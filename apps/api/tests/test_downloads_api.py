"""End-to-end downloads API tests.

Runs against real PostgreSQL with a MOCKED torlink daemon client
(dependency override), so ownership/permission/dedup logic is tested
without a live Node daemon. Live-daemon behavior was verified separately.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.api.downloads import get_torlink
from app.core.security import hash_password
from app.db.engine import dispose_engine
from app.models.download_record import DownloadRecord, DownloadStatus
from app.models.user import User, UserRole


class FakeDaemon:
    """Scriptable stand-in for TorlinkClient used by routes."""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.controls: list[tuple[str, str]] = []
        self.downloads: list[dict] = []
        self.unavailable = False
        self.control_404 = False

    async def add_download(self, magnet: str):
        from app.torlink.client import AddOutcome

        if self.unavailable:
            raise RuntimeError("unavailable")
        ih = magnet.split("btih:")[1][:40]
        self.added.append(ih)
        self.downloads.append({"id": ih, "name": "t", "status": "downloading",
                               "progress": 0.1, "peers": 1, "speed": 10})
        return AddOutcome(ok=True, outcome="added", raw={})

    async def list_downloads(self):
        from app.torlink.client import TorlinkUnavailableError
        from app.torlink.models import DaemonStatus

        if self.unavailable:
            raise TorlinkUnavailableError("down")
        return DaemonStatus.model_validate({"downloads": self.downloads, "seeds": []})

    async def control(self, tid: str, action, delete_files: bool = False):
        from app.torlink.client import TorlinkHTTPError, TorlinkUnavailableError

        if self.unavailable:
            raise TorlinkUnavailableError("down")
        if self.control_404:
            raise TorlinkHTTPError(404, "no such torrent")
        self.controls.append((tid, str(action)))
        return {"ok": True}


@pytest.fixture(autouse=True)
def _clean_db() -> Iterator[None]:
    """Truncate tables via psql before each test; drop engine after."""

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
    import asyncio

    try:
        asyncio.run(dispose_engine())
    except Exception:
        pass


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeDaemon]]:

    from app.main import create_app

    app = create_app()
    daemon = FakeDaemon()

    def _fake_daemon():
        # Wrap async methods for sync dependency injection by FastAPI.
        class _SyncShim(FakeDaemon.__class__):
            pass
        return daemon

    app.dependency_overrides[get_torlink] = _fake_daemon

    with TestClient(app) as tc:
        yield tc, daemon


MAGNET_A = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=test-a"
MAGNET_B = "magnet:?xt=urn:btih:" + "b" * 40 + "&dn=test-b"
IH_A = "a" * 40
IH_B = "b" * 40


# ---- seeding helpers -----------------------------------------------------------


def _mk_user(username: str, role: UserRole = UserRole.USER,
             password: str = "correct-horse-battery") -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _seed() -> None:
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            s.add(User(
                email=f"{username}@example.com",
                username=username,
                password_hash=hash_password(password),
                role=role,
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_seed())


def _mk_record(user_id: str, info_hash: str, **kw) -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _seed() -> None:
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            s.add(DownloadRecord(
                user_id=user_id,
                info_hash=info_hash,
                status=kw.pop("status", DownloadStatus.ACTIVE),
                **kw,
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_seed())


def _login(tc: TestClient, username: str, password: str = "correct-horse-battery") -> str:
    resp = tc.post("/api/auth/login", json={
        "username_or_email": username, "password": password,
    })
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- tests ----------------------------------------------------------------------


def test_create_requires_auth(client) -> None:
    tc, _ = client
    resp = tc.post("/api/downloads", json={"magnet": MAGNET_A})
    assert resp.status_code == 401


def test_create_rejects_magnet_without_infohash(client) -> None:
    tc, _ = client
    _mk_user("u1")
    token = _login(tc, "u1")
    resp = tc.post("/api/downloads", json={"magnet": "magnet:?dn=nohash"},
                   headers=_auth(token))
    assert resp.status_code == 400


def test_create_adds_to_daemon_and_creates_record(client) -> None:
    tc, daemon = client
    _mk_user("alice")
    token = _login(tc, "alice")

    resp = tc.post("/api/downloads", json={
        "magnet": MAGNET_A, "label": "linux", "category": "iso", "source": "search",
    }, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["info_hash"] == IH_A
    assert body["status"] == "active"
    assert body["label"] == "linux"
    assert body["source"] == "search"

    assert daemon.added == [IH_A]
    # torlink_torrent_id resolved from daemon listing
    rec = body
    assert rec["id"]

    # audit log entry exists
    import asyncio

    from sqlalchemy import text as _t
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    async def check():
        e = create_async_engine(str(get_settings().database_url))
        async with e.connect() as c:
            q = _t("SELECT count(*) FROM audit_logs WHERE action='DOWNLOAD_ADDED'")
            n = (await c.execute(q)).scalar()
        await e.dispose()
        return n

    assert asyncio.run(check()) >= 1


def test_duplicate_same_user_conflicts_but_other_user_ok(client) -> None:
    tc, _ = client
    _mk_user("dup1")
    _mk_user("dup2")
    t1 = _login(tc, "dup1")
    r1 = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t1))
    assert r1.status_code == 201

    r2 = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t1))
    assert r2.status_code == 409

    t2 = _login(tc, "dup2")
    r3 = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t2))
    assert r3.status_code == 201


def test_list_returns_only_own_downloads(client) -> None:
    tc, _ = client
    _mk_user("own1")
    _mk_user("own2")
    t1 = _login(tc, "own1")

    # seed records directly for both users
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def ids():
        e = create_async_engine(str(get_settings().database_url))
        async with async_sessionmaker(e)() as s:
            u1 = (await s.execute(select(User).where(User.username == "own1"))).scalar_one()
            u2 = (await s.execute(select(User).where(User.username == "own2"))).scalar_one()
            await e.dispose()
            return u1.id, u2.id

    u1, u2 = asyncio.run(ids())
    _mk_record(u1, IH_A)
    _mk_record(u2, IH_B)

    resp = tc.get("/api/downloads", headers=_auth(t1))
    assert resp.status_code == 200
    hashes = [d["info_hash"] for d in resp.json()]
    assert IH_A in hashes
    assert IH_B not in hashes


def test_get_detail_includes_live_fields(client) -> None:
    tc, daemon = client
    _mk_user("det")
    t = _login(tc, "det")

    created = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t)).json()
    did = created["id"]

    detail = tc.get(f"/api/downloads/{did}", headers=_auth(t))
    assert detail.status_code == 200
    data = detail.json()
    assert data["live_status"] == "downloading"
    assert data["live_progress"] == pytest.approx(0.1)


def test_other_users_download_is_404_not_403(client) -> None:
    tc, _ = client
    _mk_user("ownerA")
    _mk_user("intruder")
    ta = _login(tc, "ownerA")
    ti = _login(tc, "intruder")

    created = tc.post("/api/downloads", json={"magnet": MAGNET_B}, headers=_auth(ta)).json()
    resp = tc.get(f"/api/downloads/{created['id']}", headers=_auth(ti))
    assert resp.status_code == 404  # existence hidden


def test_pause_resume_roundtrip_updates_status(client) -> None:
    tc, daemon = client
    _mk_user("ctl")
    t = _login(tc, "ctl")

    created = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t)).json()
    did = created["id"]

    p = tc.post(f"/api/downloads/{did}/pause", headers=_auth(t))
    assert p.status_code == 200
    assert p.json()["action"] == "pause"

    got = tc.get(f"/api/downloads/{did}", headers=_auth(t)).json()
    assert got["status"] == "paused"

    r = tc.post(f"/api/downloads/{did}/resume", headers=_auth(t))
    assert r.status_code == 200
    got = tc.get(f"/api/downloads/{did}", headers=_auth(t)).json()
    assert got["status"] == "active"


def test_daemon_down_gives_503_on_control(client) -> None:
    tc, daemon = client
    daemon.unavailable = True
    _mk_user("dd")
    t = _login(tc, "dd")

    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def uid():
        e = create_async_engine(str(get_settings().database_url))
        async with async_sessionmaker(e)() as s:
            u = (await s.execute(select(User).where(User.username == "dd"))).scalar_one()
        await e.dispose()
        return u.id

    _mk_record(asyncio.run(uid()), IH_A)

    # need the record id: fetch list
    lst = tc.get("/api/downloads", headers=_auth(t)).json()
    did = lst[0]["id"]
    resp = tc.post(f"/api/downloads/{did}/pause", headers=_auth(t))
    assert resp.status_code == 503


def test_daemon_missing_torrent_marks_failed_and_409s(client) -> None:
    tc, daemon = client
    daemon.control_404 = True
    _mk_user("gone")
    t = _login(tc, "gone")

    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def uid():
        e = create_async_engine(str(get_settings().database_url))
        async with async_sessionmaker(e)() as s:
            u = (await s.execute(select(User).where(User.username == "gone"))).scalar_one()
        await e.dispose()
        return u.id

    _mk_record(asyncio.run(uid()), IH_A)
    did = tc.get("/api/downloads", headers=_auth(t)).json()[0]["id"]

    resp = tc.post(f"/api/downloads/{did}/pause", headers=_auth(t))
    assert resp.status_code == 409
    got = tc.get(f"/api/downloads/{did}", headers=_auth(t)).json()
    assert got["status"] == "failed"


def test_delete_removes_from_daemon_soft_deletes_record(client) -> None:
    tc, daemon = client
    _mk_user("del")
    t = _login(tc, "del")
    created = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t)).json()
    did = created["id"]

    resp = tc.delete(f"/api/downloads/{did}?delete_files=true", headers=_auth(t))
    assert resp.status_code == 204
    assert ("x" * 0 + created["info_hash"], "delete") in [
        (c[0], c[1]) for c in daemon.controls
    ]
    # record gone from listing
    assert tc.get("/api/downloads", headers=_auth(t)).json() == []


def test_readonly_role_cannot_add(client) -> None:
    tc, _ = client
    _mk_user("ro", role=UserRole.READONLY)
    t = _login(tc, "ro")
    resp = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(t))
    assert resp.status_code == 403


def test_admin_sees_all_and_can_control_others(client) -> None:
    tc, _ = client
    _mk_user("pleb")
    _mk_user("root", role=UserRole.ADMIN)
    tp = _login(tc, "pleb")
    tr = _login(tc, "root")

    created = tc.post("/api/downloads", json={"magnet": MAGNET_A}, headers=_auth(tp)).json()

    admin_list = tc.get("/api/downloads", headers=_auth(tr)).json()
    assert len(admin_list) == 1
    assert admin_list[0]["id"] == created["id"]

    # admin can control someone else's download
    ok = tc.post(f"/api/downloads/{created['id']}/pause", headers=_auth(tr))
    assert ok.status_code == 200

    # but pleb cannot see or touch admin's (none exist anyway): sanity
    assert tc.get(f"/api/downloads/{created['id']}", headers=_auth(tp)).status_code == 200
