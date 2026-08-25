"""Public file-browser API (Phase 8).

Rules enforced here:
- no raw filesystem paths in requests or responses — UUID ids only
- users see only files belonging to their own download records
- admins see all; IDOR-safe 404 for foreign resources
- readonly may browse/stream but not delete
- deletion requires is_deletion_allowed on the file's download record
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import _audit
from app.api.files_service import (
    SafePathError,
    download_root,
    proxied_url,
    proxy_stream,
    safe_join,
    walk_tree,
)
from app.core.auth_deps import get_current_user
from app.db.engine import get_db_session
from app.models.audit_log import AuditAction
from app.models.download_record import DownloadRecord, DownloadStatus
from app.models.file_record import FileRecord
from app.models.user import User, UserRole

router = APIRouter(prefix="/files", tags=["files"])


def _can_touch(user: User, record: DownloadRecord) -> bool:
    return user.id == record.user_id or user.role is UserRole.ADMIN


async def _get_owned_file(file_id: uuid.UUID, user: User, session: AsyncSession):
    row = (
        await session.execute(
            select(FileRecord, DownloadRecord)
            .join(DownloadRecord, FileRecord.download_record_id == DownloadRecord.id)
            .where(FileRecord.id == file_id, DownloadRecord.status != DownloadStatus.REMOVED)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    file_rec, download_rec = row
    if not _can_touch(user, download_rec):
        # 404 not 403: hide existence of other users' files.
        raise HTTPException(status_code=404, detail="File not found")
    return file_rec, download_rec


def _file_out(rec: FileRecord, download: DownloadRecord) -> dict:
    return {
        "id": str(rec.id),
        "download_id": str(download.id),
        "download_name": download.name,
        "display_name": rec.display_name,
        "size_bytes": rec.size_bytes,
        "mime_type": rec.mime_type,
        "created_at": rec.created_at,
    }


@router.get("")
async def list_files(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    stmt = (
        select(FileRecord, DownloadRecord)
        .join(DownloadRecord, FileRecord.download_record_id == DownloadRecord.id)
        .where(DownloadRecord.status != DownloadStatus.REMOVED)
        .order_by(FileRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if user.role is not UserRole.ADMIN:
        stmt = stmt.where(FileRecord.user_id == user.id)
    rows = (await session.execute(stmt)).all()
    return [_file_out(f, d) for f, d in rows]


@router.get("/tree")
async def get_tree(
    download_id: uuid.UUID,
    dir: str = Query(default="", max_length=1024),
    sync: bool = Query(default=True),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """Directory listing for one owned download. `dir` is relative and only
    ever used read-only against the local root via safe_join."""

    rec = await session.get(DownloadRecord, download_id)
    if rec is None or rec.status is DownloadStatus.REMOVED or not _can_touch(user, rec):
        raise HTTPException(status_code=404, detail="Download not found")

    root = download_root()
    torrent_rel = str(rec.info_hash)
    torrent_dir_exists = Path(root, rec.info_hash).exists()

    # Sync file_records from disk on first browse of this download.
    if sync and torrent_dir_exists:
        disk_entries = walk_tree(root, torrent_rel)
        known_paths: set[str] = set()
        for entry in disk_entries:
            if entry["type"] != "file":
                continue
            known_paths.add(entry["relative_path"])
            exists = (
                await session.execute(
                    select(FileRecord.id).where(
                        FileRecord.download_record_id == rec.id,
                        FileRecord.relative_path == entry["relative_path"],
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(FileRecord(
                    user_id=rec.user_id,
                    download_record_id=rec.id,
                    relative_path=entry["relative_path"],
                    display_name=entry["name"],
                    size_bytes=entry.get("size_bytes"),
                ))
        if known_paths:
            await session.commit()

    if dir:
        entries = walk_tree(root, dir)
    elif torrent_dir_exists:
        entries = walk_tree(root, torrent_rel)
    else:
        entries = []

    # Mark which entries are already registered records.
    recorded = set(
        (
            await session.execute(
                select(FileRecord.relative_path).where(FileRecord.download_record_id == rec.id)
            )
        ).scalars().all()
    )
    return {
        "download_id": str(rec.id),
        "path": dir,
        "entries": [
            {**e, "file_registered": e["relative_path"] in recorded}
            for e in entries
        ],
    }


@router.get("/{file_id}")
async def get_file_meta(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    rec, download = await _get_owned_file(file_id, user, session)
    out = _file_out(rec, download)
    out["relative_path"] = rec.relative_path  # internal hint, still not absolute
    return out


@router.head("/{file_id}/stream")
async def head_stream(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _get_owned_file(file_id, user, session)
    return Response(status_code=200)


@router.get("/{file_id}/stream")
async def stream_file(
    request: Request,
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    rec, _download = await _get_owned_file(file_id, user, session)

    # Verify the file still resolves inside the root before proxying.
    root = download_root()
    try:
        full = safe_join(root, rec.relative_path)
    except SafePathError as exc:
        raise HTTPException(status_code=500, detail="stored path failed safety check") from exc
    if not full.is_file():
        raise HTTPException(status_code=404, detail="file not found on disk")

    range_header = request.headers.get("range")
    response = proxy_stream(proxied_url(rec.relative_path), range_header)
    response.headers["content-disposition"] = (
        f'inline; filename="{rec.display_name.replace(chr(34), "")}"'
    )
    return response


@router.delete("/{file_id}", status_code=204)
async def delete_file(
    file_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    rec, download = await _get_owned_file(file_id, user, session)
    if not download.is_deletion_allowed:
        raise HTTPException(status_code=403, detail="Deletion is not allowed for this download")

    root = download_root()
    try:
        full = safe_join(root, rec.relative_path)
    except SafePathError as exc:
        raise HTTPException(status_code=500, detail="stored path failed safety check") from exc

    removed_from_disk = False
    if full.is_file():
        try:
            full.unlink()
            removed_from_disk = True
        except OSError as exc:
            raise HTTPException(status_code=500, detail="could not delete file on disk") from exc

    await session.delete(rec)
    await session.flush()
    await _audit(
        session,
        AuditAction.DOWNLOAD_DELETED,
        user.id,
        {
            "kind": "single_file",
            "file_id": str(rec.id),
            "download_id": str(download.id),
            "removed_from_disk": removed_from_disk,
        },
    )
    await session.commit()
