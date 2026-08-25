"""WS /ws/downloads — live download progress for authenticated clients.

Auth: browsers cannot set custom headers on WebSocket handshakes, so the
access token is passed as a query parameter (`?token=<jwt>`). It is only
read at connect time; the connection is rejected with close code 4401
before any data flows if the token is missing/invalid/expired.

Scoping: each tick, records are filtered by ownership (daemon torrent ids
are matched to the caller's download_records via info hash). Admins see
every daemon torrent; users and readonly users see only their own.
"""

from __future__ import annotations

import asyncio
import uuid

import jwt as pyjwt
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.core.security import decode_access_token
from app.db.engine import get_session_factory
from app.models.download_record import DownloadRecord
from app.models.user import User, UserRole


async def _user_from_token(token: str | None) -> User | None:
    """Resolve a User from a raw access-token string (query-param auth)."""

    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(str(payload.get("sub")))
    except (pyjwt.PyJWTError, ValueError):
        return None
    session_factory = get_session_factory()
    async with session_factory() as session:
        user = await session.get(User, user_id)
        if user is None or not user.is_active:
            return None
        # Detach so the instance stays usable after the session closes.
        session.expunge(user)
        return user


async def _visible_info_hashes(user: User) -> set[str]:
    """Info hashes this user may see. Admins get an empty set => see all."""

    if user.role is UserRole.ADMIN:
        return set()
    session_factory = get_session_factory()
    async with session_factory() as session:
        result = await session.execute(
            select(DownloadRecord.info_hash).where(
                DownloadRecord.user_id == user.id,
                DownloadRecord.status != "removed",
            )
        )
        return {row[0].lower() for row in result}


def _filter_snapshot(message: dict, visible_hashes: set[str]) -> dict:
    """Filter a broadcast snapshot down to the client's own torrents."""

    if not visible_hashes:
        return message  # admin: everything
    clone = dict(message)
    clone["downloads"] = [
        d for d in message.get("downloads", []) if str(d.get("id", "")).lower() in visible_hashes
    ]
    clone["seeds"] = [
        s for s in message.get("seeds", []) if str(s.get("id", "")).lower() in visible_hashes
    ]
    return clone


def register_ws_routes(app: FastAPI) -> None:
    @app.websocket("/ws/downloads")
    async def ws_downloads(websocket: WebSocket, token: str | None = Query(default=None)) -> None:
        user = await _user_from_token(token)
        if user is None:
            await websocket.close(code=4401)
            return

        poller = getattr(app.state, "download_poller", None)
        cache = getattr(app.state, "status_cache", None)
        if poller is None or cache is None:
            await websocket.close(code=1011)
            return

        await websocket.accept()

        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
        cache.clients.add(queue)
        try:
            # Immediate first frame from the existing cache (if any), then
            # make sure a fresh poll happens soon even if the loop is idle.
            visible = await _visible_info_hashes(user)
            if cache.snapshot is not None and queue.empty():
                queue.put_nowait(_filter_snapshot(_last_message(cache), visible))
            poller.kick()

            while True:
                message = await queue.get()
                await websocket.send_json(_filter_snapshot(message, visible))
        except WebSocketDisconnect:
            pass
        except Exception:
            # Dead socket / send failure — drop the client quietly.
            pass
        finally:
            cache.clients.discard(queue)


def _last_message(cache) -> dict:
    """Rebuild a broadcast-shaped message from the cached snapshot."""

    status = cache.snapshot
    message: dict = {
        "type": "snapshot",
        "downloads": [
            {
                "id": d.id,
                "name": d.name,
                "status": d.status,
                "progress": d.progress,
                "peers": d.peers,
                "speed": d.speed,
            }
            for d in status.downloads
        ],
        "seeds": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "peers": s.peers,
                "uploaded": s.uploaded,
            }
            for s in status.seeds
        ],
    }
    return message
