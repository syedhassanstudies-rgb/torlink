"""Phase 6 tests: /ws/downloads realtime progress.

Covers: auth rejection, user-scoped visibility, admin sees all, broadcast
fan-out through the shared poller, and poller idle behavior with zero
clients. Runs against real PostgreSQL with a mocked daemon client.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.security import hash_password
from app.db.engine import dispose_engine
from app.models.download_record import DownloadRecord, DownloadStatus
from app.models.user import User, UserRole
from app.realtime.poller import DownloadStatusPoller, StatusCache

IH_A = "a" * 40
IH_B = "b" * 40


class FakeDaemon:
    """Scriptable stand-in for TorlinkClient (same shape as downloads tests)."""

    def __init__(self) -> None:
        self.downloads: list[dict] = [
            {"id": IH_A, "name": "alpha", "status": "downloading",
             "progress": 0.25, "peers": 3, "speed": 1024.0},
            {"id": IH_B, "name": "beta", "status": "downloading",
             "progress": 0.5, "peers": 5, "speed": 2048.0},
        ]
        self.seeds: list[dict] = []
        self.unavailable = False
        self.calls = 0

    async def list_downloads(self):
        from app.torlink.client import TorlinkUnavailableError
        from app.torlink.models import DaemonStatus

        self.calls += 1
        if self.unavailable:
            raise TorlinkUnavailableError("down")
        return DaemonStatus.model_validate({"downloads": self.downloads, "seeds": self.seeds})


@pytest.fixture(autouse=True)
def _clean_db() -> Iterator[None]:
    import os
    import subprocess

    from app.core.config import get_settings

    url = str(get_settings().database_url)
    rest = url.split("://", 1)[1]
    userinfo, hostport_db = rest.rsplit("@", 1)
    user, password = userinfo.split(":", 1)
    host, port_db = hostport_db.split(":", 1)
    port, dbname = port_db.split("/", 1)

    env = os.environ.copy()
    env["PGPASSWORD"] = password
    subprocess.run(
        ["psql", "-U", user, "-h", host, "-p", port, "-d", dbname, "-c",
         "TRUNCATE users, refresh_tokens, api_keys, download_records, "
         "file_records, user_settings, audit_logs CASCADE"],
        env=env, check=True,
    )
    yield
    try:
        asyncio.run(dispose_engine())
    except Exception:
        pass


@pytest.fixture
def ws_env() -> Iterator[tuple[TestClient, FakeDaemon, StatusCache]]:
    from app.main import create_app

    app = create_app()
    daemon = FakeDaemon()
    cache = StatusCache()
    poller = DownloadStatusPoller(daemon, cache, interval_seconds=0.05)

    original_lifespan = app.router.lifespan_context

    async def _swap(app):  # noqa: ANN001 - matches Lifespan signature
        async with original_lifespan(app):
            await poller.stop()  # stop the lifespan-built poller
            app.state.status_cache = cache
            app.state.download_poller = poller
            poller.start()
            yield

    import contextlib as _cl

    app.router.lifespan_context = _cl.asynccontextmanager(_swap)
    with TestClient(app) as tc:
        yield tc, daemon, cache
    asyncio.run(poller.stop())


# ---- sync DB helpers (short-lived engines; loop-safe) -----------------------------


def _seed_user(username: str, role: UserRole = UserRole.USER) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _run():
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            s.add(User(
                email=f"{username}@example.com",
                username=username,
                password_hash=hash_password("correct-horse-battery"),
                role=role,
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_run())


def _user_id(username: str):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _run():
        engine = create_async_engine(str(get_settings().database_url))
        async with async_sessionmaker(engine)() as s:
            uid = (await s.execute(select(User.id).where(User.username == username))).scalar_one()
        await engine.dispose()
        return uid

    return asyncio.run(_run())


def _seed_record(user_id, info_hash: str) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _run():
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            s.add(DownloadRecord(
                user_id=user_id,
                info_hash=info_hash,
                status=DownloadStatus.ACTIVE,
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_run())


def _login(tc: TestClient, username: str) -> str:
    r = tc.post("/api/auth/login", json={
        "username_or_email": username,
        "password": "correct-horse-battery",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _setup_two_users() -> None:
    """user 'wsa' owns IH_A; admin 'adminws' owns IH_B."""

    _seed_user("wsa")
    _seed_user("adminws", role=UserRole.ADMIN)
    _seed_record(_user_id("wsa"), IH_A)
    _seed_record(_user_id("adminws"), IH_B)


# ---- unit-ish poller tests --------------------------------------------------------


def test_poller_skips_daemon_when_no_clients(ws_env) -> None:
    _, daemon, cache = ws_env

    async def run():
        p = DownloadStatusPoller(daemon, cache, interval_seconds=0.01)
        ok = await p.poll_once()
        assert ok is False
        assert daemon.calls == 0

    asyncio.run(run())


def test_poller_broadcasts_to_registered_clients(ws_env) -> None:
    _, daemon, cache = ws_env

    async def run():
        q1: asyncio.Queue = asyncio.Queue()
        q2: asyncio.Queue = asyncio.Queue()
        cache.clients.update({q1, q2})
        p = DownloadStatusPoller(daemon, cache, interval_seconds=0.01)
        assert await p.poll_once() is True
        assert daemon.calls == 1

        m1 = q1.get_nowait()
        m2 = q2.get_nowait()
        for m in (m1, m2):
            assert m["type"] == "snapshot"
            ids = {d["id"] for d in m["downloads"]}
            assert IH_A in ids and IH_B in ids

        # Daemon error must not raise; last good snapshot is retained.
        daemon.unavailable = True
        assert await p.poll_once() is False
        assert cache.snapshot is not None

    asyncio.run(run())


# ---- WebSocket integration tests ---------------------------------------------------


def test_ws_rejects_missing_token(ws_env) -> None:
    tc, _, _ = ws_env
    with pytest.raises(Exception):  # noqa: B017 - starlette raises WebSocketDisconnect
        with tc.websocket_connect("/ws/downloads"):
            pass


def test_ws_rejects_invalid_token(ws_env) -> None:
    tc, _, _ = ws_env
    with pytest.raises(Exception):  # noqa: B017
        with tc.websocket_connect("/ws/downloads?token=not-a-jwt"):
            pass


def test_ws_rejects_unknown_user_token(ws_env) -> None:
    tc, _, _ = ws_env
    token = _login(tc, "ghost") if False else None
    # Simulate a valid-signature JWT for a user id not in the DB.
    import uuid

    from app.core.security import create_access_token

    token, _ = create_access_token(uuid.uuid4(), "user")
    assert token is not None
    with pytest.raises(Exception):  # noqa: B017
        with tc.websocket_connect(f"/ws/downloads?token={token}"):
            pass


def test_ws_user_sees_only_own_downloads(ws_env) -> None:
    tc, _, _ = ws_env
    _setup_two_users()
    token_a = _login(tc, "wsa")

    with tc.websocket_connect(f"/ws/downloads?token={token_a}") as ws:
        msg = json.loads(ws.receive_text())
        ids = {d["id"] for d in msg["downloads"]}
        assert IH_A in ids
        assert IH_B not in ids
        assert msg["type"] == "snapshot"


def test_ws_admin_sees_all_downloads(ws_env) -> None:
    tc, _, _ = ws_env
    _setup_two_users()
    token_admin = _login(tc, "adminws")

    with tc.websocket_connect(f"/ws/downloads?token={token_admin}") as ws:
        msg = json.loads(ws.receive_text())
        ids = {d["id"] for d in msg["downloads"]}
        assert IH_A in ids and IH_B in ids


def test_ws_receives_live_updates_from_shared_poller(ws_env) -> None:
    tc, daemon, _cache = ws_env
    _setup_two_users()
    token_a = _login(tc, "wsa")

    with tc.websocket_connect(f"/ws/downloads?token={token_a}") as ws:
        json.loads(ws.receive_text())  # first frame

        daemon.downloads[0]["progress"] = 0.9
        deadline = time.monotonic() + 5.0
        target_progress = None
        while time.monotonic() < deadline:
            msg = json.loads(ws.receive_text())
            match = next((d for d in msg["downloads"] if d["id"] == IH_A), None)
            if match is not None and match["progress"] == pytest.approx(0.9):
                target_progress = match["progress"]
                break
        assert target_progress == pytest.approx(0.9)


def test_shared_poller_single_loop_for_two_clients(ws_env) -> None:
    """Two clients connected => still one daemon call per tick, not two."""
    tc, daemon, _cache = ws_env
    _setup_two_users()
    ta = _login(tc, "wsa")
    tb = _login(tc, "adminws")

    with tc.websocket_connect(f"/ws/downloads?token={ta}") as wsa:
        with tc.websocket_connect(f"/ws/downloads?token={tb}") as wsb:
            json.loads(wsa.receive_text())
            json.loads(wsb.receive_text())
            calls_before = daemon.calls
            time.sleep(0.35)
    # Poll interval is 0.05s => ~7 ticks expected; far fewer than 2-per-tick.
    elapsed_calls = daemon.calls - calls_before
    assert elapsed_calls < 10
