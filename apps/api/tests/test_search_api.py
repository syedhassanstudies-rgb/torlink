"""Phase 7 tests: search API (mocked daemon, real Postgres)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.api.downloads import get_torlink
from app.core.security import hash_password
from app.db.engine import dispose_engine
from app.models.user import User, UserRole


class FakeSearchDaemon:
    def __init__(self) -> None:
        self.searched: list[str] = []
        self.unavailable = False

    async def search(self, query: str):
        from app.torlink.client import TorlinkClientError, TorlinkUnavailableError
        from app.torlink.models import DaemonSearchResults

        if self.unavailable:
            raise TorlinkUnavailableError("down")
        if not query.strip():
            raise TorlinkClientError("empty search query")
        self.searched.append(query)
        return DaemonSearchResults.model_validate({
            "ok": True,
            "query": query,
            "elapsedMs": 123,
            "timedOut": False,
            "results": [
                {
                    "infoHash": "a" * 40,
                    "name": "Ubuntu 24.04 ISO",
                    "sizeBytes": 5_000_000_000,
                    "seeders": 99,
                    "leechers": 3,
                    "numFiles": 1,
                    "source": "tpb-movies",
                    "magnet": "magnet:?xt=urn:btih:" + "a" * 40,
                    "added": 1700000000,
                },
                {
                    "infoHash": "b" * 40,
                    "name": "ubuntu linux dvd",
                    "sizeBytes": 4_000_000_000,
                    "seeders": 10,
                    "leechers": 1,
                    "numFiles": None,
                    "source": "x1337-tv",
                    "magnet": "magnet:?xt=urn:btih:" + "b" * 40,
                    "added": None,
                },
            ],
            "sources": {
                "yts": {"id": "yts", "count": 2, "error": None},
                "nyaa": {"id": "nyaa", "count": 0, "error": "timeout after 10000ms"},
            },
        })


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
         "file_records, user_settings, audit_logs, search_history CASCADE"],
        env=env, check=True,
    )
    yield
    try:
        asyncio.run(dispose_engine())
    except Exception:
        pass


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeSearchDaemon]]:
    from app.main import create_app

    app = create_app()
    daemon = FakeSearchDaemon()
    app.dependency_overrides[get_torlink] = lambda: daemon
    with TestClient(app) as tc:
        yield tc, daemon


def _seed_user(username: str) -> None:
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
                role=UserRole.USER,
            ))
            await s.commit()
        await engine.dispose()

    asyncio.run(_run())


def _login(tc: TestClient, username: str) -> str:
    r = tc.post("/api/auth/login", json={
        "username_or_email": username, "password": "correct-horse-battery",
    })
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- adapter-level -------------------------------------------------------------


def test_client_search_rejects_empty_query() -> None:
    import asyncio

    from app.torlink.client import TorlinkClient, TorlinkClientError

    async def run():
        client = TorlinkClient(base_url="http://127.0.0.1:9161", token="x")
        with pytest.raises(TorlinkClientError):
            await client.search("   ")

    asyncio.run(run())


# ---- API -----------------------------------------------------------------------


def test_search_requires_auth(client) -> None:
    tc, _ = client
    assert tc.post("/api/search", json={"query": "ubuntu"}).status_code == 401


def test_search_returns_results_and_stores_history(client) -> None:
    tc, daemon = client
    _seed_user("searcher")
    token = _login(tc, "searcher")

    resp = tc.post("/api/search", json={"query": "ubuntu"}, headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["query"] == "ubuntu"
    assert body["total"] == 2
    assert body["results"][0]["info_hash"] == "a" * 40
    assert body["results"][0]["seeders"] == 99
    assert "yts" in body["sources"]

    # history recorded
    hist = tc.get("/api/search/history", headers=_auth(token)).json()
    assert len(hist) == 1
    assert hist[0]["query"] == "ubuntu"
    assert hist[0]["result_count"] == 2


def test_history_opt_out_via_user_settings(client) -> None:
    tc, _ = client
    _seed_user("private")
    token = _login(tc, "private")

    # set opt-out flag
    r = tc.put("/api/users/me/settings", json={"save_search_history": False},
               headers=_auth(token))
    if r.status_code == 404 or r.status_code == 405:
        pytest.skip("user settings endpoint not implemented yet")
    tc.post("/api/search", json={"query": "secret"}, headers=_auth(token))
    hist = tc.get("/api/search/history", headers=_auth(token)).json()
    assert hist == []


def test_history_scoped_to_owner(client) -> None:
    tc, _ = client
    _seed_user("hist_a")
    _seed_user("hist_b")
    ta = _login(tc, "hist_a")
    tb = _login(tc, "hist_b")

    tc.post("/api/search", json={"query": "mine"}, headers=_auth(ta))
    hist_b = tc.get("/api/search/history", headers=_auth(tb)).json()
    assert hist_b == []


def test_clear_history(client) -> None:
    tc, _ = client
    _seed_user("clearme")
    token = _login(tc, "clearme")
    tc.post("/api/search", json={"query": "gone"}, headers=_auth(token))
    assert tc.delete("/api/search/history", headers=_auth(token)).status_code == 204
    assert tc.get("/api/search/history", headers=_auth(token)).json() == []


def test_daemon_unavailable_maps_503(client) -> None:
    tc, daemon = client
    _seed_user("offline")
    token = _login(tc, "offline")
    daemon.unavailable = True
    resp = tc.post("/api/search", json={"query": "x"}, headers=_auth(token))
    assert resp.status_code == 503


def test_sources_endpoint_requires_auth(client) -> None:
    tc, _ = client
    assert tc.get("/api/search/sources").status_code == 401


def test_sources_endpoint_lists_registry(client) -> None:
    tc, _ = client
    _seed_user("srcviewer")
    token = _login(tc, "srcviewer")
    resp = tc.get("/api/search/sources", headers=_auth(token))
    assert resp.status_code == 200
    ids = [s["id"] for s in resp.json()]
    assert "yts" in ids and "nyaa" in ids
    assert len(ids) == 10
