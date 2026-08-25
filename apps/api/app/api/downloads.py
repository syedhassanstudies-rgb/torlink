"""Public downloads API: application records + torlink daemon execution.

Ownership rules enforced here:
- users see/control only their own download records
- admins can see all; control of someone else's download is admin-only
- deletion additionally requires is_deletion_allowed on the record
"""

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _audit
from app.api.download_schemas import (
    ActionOk,
    DownloadCreate,
    DownloadDetail,
    DownloadOut,
)
from app.core.auth_deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.ratelimit import add_download_limiter
from app.db.engine import get_db_session
from app.models.audit_log import AuditAction, AuditLog
from app.models.download_record import DownloadRecord, DownloadStatus
from app.models.user import User, UserRole
from app.torlink.client import (
    TorlinkClient,
    TorlinkClientError,
    TorlinkHTTPError,
    TorlinkUnavailableError,
)

router = APIRouter(prefix="/downloads", tags=["downloads"])

INFO_HASH_RE = re.compile(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")


def get_torlink(settings: Settings = Depends(get_settings)) -> TorlinkClient:
    return TorlinkClient(
        base_url=str(settings.torlink_base_url),
        token=settings.torlink_token,
        timeout_seconds=settings.torlink_timeout_seconds,
    )


async def _daemon_status_or_503(client: TorlinkClient):
    try:
        return await client.list_downloads()
    except TorlinkUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="torlink daemon is unavailable",
        ) from exc
    except TorlinkClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


def _extract_info_hash(magnet: str) -> str | None:
    m = INFO_HASH_RE.search(magnet)
    return m.group(1).lower() if m else None


def _can_touch(user: User, record: DownloadRecord) -> bool:
    """Read/control permission: owner or admin."""
    return user.id == record.user_id or user.role is UserRole.ADMIN


async def _get_owned_record(
    download_id: uuid.UUID, user: User, session: AsyncSession
) -> DownloadRecord:
    record = await session.get(DownloadRecord, download_id)
    if record is None or record.status is DownloadStatus.REMOVED:
        raise HTTPException(status_code=404, detail="Download not found")
    if not _can_touch(user, record):
        # 404, not 403: don't confirm existence of others' resources (IDOR).
        raise HTTPException(status_code=404, detail="Download not found")
    return record


@router.get("", response_model=list[DownloadOut])
async def list_downloads(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[DownloadRecord]:
    stmt = select(DownloadRecord)
    if user.role is not UserRole.ADMIN:
        stmt = stmt.where(DownloadRecord.user_id == user.id)
    stmt = (
        stmt.where(DownloadRecord.status != DownloadStatus.REMOVED)
        .order_by(DownloadRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


@router.post("", response_model=DownloadOut, status_code=201)
async def create_download(
    payload: DownloadCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> DownloadRecord:
    if get_settings().rate_limit_enabled:
        add_download_limiter.check(request)
    if user.role not in (UserRole.ADMIN, UserRole.USER):
        raise HTTPException(status_code=403, detail="Readonly users cannot add downloads")

    info_hash = _extract_info_hash(payload.magnet)
    if info_hash is None:
        raise HTTPException(status_code=400, detail="magnet must contain a btih info hash")

    # App-level dedupe first (cheaper and gives a precise error).
    existing = (
        await session.execute(
            select(DownloadRecord).where(
                DownloadRecord.user_id == user.id,
                DownloadRecord.info_hash == info_hash,
                DownloadRecord.status != DownloadStatus.REMOVED,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="You already added this torrent")

    # Hand off to the daemon.
    try:
        outcome = await client.add_download(payload.magnet)
    except TorlinkUnavailableError as exc:
        raise HTTPException(status_code=503, detail="torlink daemon is unavailable") from exc
    except (TorlinkHTTPError, TorlinkClientError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # Resolve the daemon-assigned torrent id by matching info hash.
    live = await _daemon_status_or_503(client)
    torlink_id = next((d.id for d in live.downloads if d.id.lower() == info_hash), None)

    record = DownloadRecord(
        user_id=user.id,
        info_hash=info_hash,
        name=None,
        status=DownloadStatus.ACTIVE,
        magnet_uri=payload.magnet[:2048],
        category=payload.category,
        label=payload.label,
        source=payload.source,
        torlink_torrent_id=torlink_id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)

    session.add(
        AuditLog(
            action=AuditAction.DOWNLOAD_ADDED,
            user_id=user.id,
            detail={
                "download_id": str(record.id),
                "info_hash": info_hash,
                "source": payload.source,
                "daemon_outcome": outcome.outcome,
            },
        )
    )
    await session.commit()
    return record


@router.get("/{download_id}", response_model=DownloadDetail)
async def get_download(
    download_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> dict:
    record = await _get_owned_record(download_id, user, session)
    detail: dict = {
        c: getattr(record, c)
        for c in (
            "id",
            "info_hash",
            "name",
            "status",
            "label",
            "category",
            "source",
            "is_deletion_allowed",
            "created_at",
        )
    }
    # Best-effort live enrichment; a dead daemon doesn't kill the metadata view.
    try:
        live = await client.list_downloads()
    except TorlinkClientError:
        return detail

    items = list(live.downloads) + list(live.seeds)
    match = next((d for d in items if d.id.lower() == record.info_hash), None)
    if match is not None:
        detail.update(
            live_status=match.status,
            live_progress=float(getattr(match, "progress", 0.0) or 0.0),
            live_peers=int(getattr(match, "peers", 0) or 0),
            live_speed=float(getattr(match, "speed", 0.0) or 0.0),
            seeding=any(s.id.lower() == record.info_hash for s in live.seeds),
        )
    return detail


async def _control_action(
    download_id: uuid.UUID,
    action: str,
    user: User,
    session: AsyncSession,
    client: TorlinkClient,
    audit_action: AuditAction | None = None,
) -> dict:
    record = await _get_owned_record(download_id, user, session)
    tid = record.torlink_torrent_id or record.info_hash
    try:
        body = await client.control(tid, action)
    except TorlinkUnavailableError as exc:
        raise HTTPException(status_code=503, detail="torlink daemon is unavailable") from exc
    except TorlinkHTTPError as exc:
        if exc.status_code == 404:
            # Daemon lost it (restart etc.) — reflect reality in the record.
            record.status = DownloadStatus.FAILED
            await session.commit()
            raise HTTPException(
                status_code=409, detail="Torrent no longer exists in daemon"
            ) from exc
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TorlinkClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if action == "pause":
        record.status = DownloadStatus.PAUSED
    elif action == "resume":
        record.status = DownloadStatus.ACTIVE
    else:
        record.status = DownloadStatus.COMPLETED

    if audit_action is not None:
        await _audit(session, audit_action, user.id, {"download_id": str(record.id)})
    await session.commit()
    return {"ok": True, "id": str(download_id), "action": action, "daemon": body}


@router.post("/{download_id}/pause", response_model=ActionOk)
async def pause_download(
    download_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> dict:
    return await _control_action(
        download_id, "pause", user, session, client, AuditAction.DOWNLOAD_PAUSED
    )


@router.post("/{download_id}/resume", response_model=ActionOk)
async def resume_download(
    download_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> dict:
    return await _control_action(
        download_id, "resume", user, session, client, AuditAction.DOWNLOAD_RESUMED
    )


@router.post("/{download_id}/start-seeding", response_model=ActionOk)
async def start_seeding(
    download_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> dict:
    return await _control_action(
        download_id,
        "start-seed",
        user,
        session,
        client,
        AuditAction.DOWNLOAD_SEEDING_STARTED,
    )


@router.post("/{download_id}/stop-seeding", response_model=ActionOk)
async def stop_seeding(
    download_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> dict:
    return await _control_action(
        download_id,
        "stop-seed",
        user,
        session,
        client,
        AuditAction.DOWNLOAD_SEEDING_STOPPED,
    )


@router.delete("/{download_id}", status_code=204)
async def delete_download(
    download_id: uuid.UUID,
    delete_files: bool = Query(default=False),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    client: TorlinkClient = Depends(get_torlink),
) -> None:
    record = await _get_owned_record(download_id, user, session)
    if not record.is_deletion_allowed:
        raise HTTPException(status_code=403, detail="Deletion is not allowed for this download")

    tid = record.torlink_torrent_id or record.info_hash
    action = "delete" if delete_files else "remove"
    try:
        await client.control(tid, action)
    except TorlinkUnavailableError as exc:
        raise HTTPException(status_code=503, detail="torlink daemon is unavailable") from exc
    except TorlinkHTTPError as exc:
        if exc.status_code != 404:  # already gone from daemon is fine
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TorlinkClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    record.status = DownloadStatus.REMOVED
    await session.commit()
    await _audit(
        session,
        AuditAction.DOWNLOAD_DELETED,
        user.id,
        {"download_id": str(record.id), "delete_files": delete_files},
    )
