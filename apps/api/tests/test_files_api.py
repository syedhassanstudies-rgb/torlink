"""Phase 8 tests: file browser API (real Postgres, local temp download dir).

The stream endpoint is tested with a stubbed proxy so no Node files server
is required; live verification happens separately.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.models  # noqa: F401
from app.core.security import hash_password
from app.db.engine import dispose_engine
from app.models.download_record import DownloadRecord, DownloadStatus
from app.models.user import User, UserRole

IH_A = "a" * 40


@pytest.fixture(autouse=True)
def _clean_db(tmp_path: Path) -> Iterator[Path]:
    import os
    import subprocess

    from app.core.config import get_settings

    # Temp download dir with one torrent folder + files.
    tdir = tmp_path / "downloads"
    torrent_dir = tdir / IH_A
    torrent_dir.mkdir(parents=True)
    (torrent_dir / "movie.mp4").write_bytes(b"0123456789abcdef")  # 16 bytes
    sub = torrent_dir / "subs"
    sub.mkdir()
    (sub / "en.srt").write_text("hello subs")

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
    yield tdir
    try:
        asyncio.run(dispose_engine())
    except Exception:
        pass


@pytest.fixture
def client(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    from app.main import create_app

    app = create_app()
    # Point settings at the temp dir for this test process.
    from app.core.config import get_settings

    get_settings().__dict__["download_dir"] = str(tmp_path / "downloads")

    with TestClient(app) as tc:
        yield tc, tmp_path / "downloads"


def _seed_user(username: str, role: UserRole = UserRole.USER):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _run():
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            u = User(
                email=f"{username}@example.com",
                username=username,
                password_hash=hash_password("correct-horse-battery"),
                role=role,
            )
            s.add(u)
            await s.commit()
            await s.refresh(u)
            uid = u.id
        await engine.dispose()
        return uid

    return asyncio.run(_run())


def _seed_download(user_id, info_hash: str, **kw) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def _run():
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            s.add(DownloadRecord(
                user_id=user_id,
                info_hash=info_hash,
                status=kw.pop("status", DownloadStatus.COMPLETED),
                **kw,
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


# ---- unit: safe_join ---------------------------------------------------------------


def test_safe_join_rejects_traversal_and_absolute() -> None:
    import tempfile

    from app.api.files_service import SafePathError, safe_join

    with tempfile.TemporaryDirectory() as td:
        root = td
        assert safe_join(root, "a/b.txt").is_absolute()
        for bad in ("../escape", "..\\escape", "/etc/passwd", "C:/Windows", "a/../../out"):
            with pytest.raises(SafePathError):
                safe_join(root, bad)


# ---- API --------------------------------------------------------------------------


def test_files_require_auth(client) -> None:
    tc, _root = client
    assert tc.get("/api/files").status_code == 401


def test_tree_syncs_and_lists_files(client) -> None:
    tc, _root = client
    uid = _seed_user("fowner")
    _seed_download(uid, IH_A)
    token = _login(tc, "fowner")

    resp = tc.get(f"/api/files/tree?download_id={_dl_id(token, tc)}", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rels = {e["relative_path"]: e for e in body["entries"]}
    assert f"{IH_A}/movie.mp4" in rels
    assert rels[f"{IH_A}/movie.mp4"]["size_bytes"] == 16

    # sync registered the file records
    listed = tc.get("/api/files", headers=_auth(token)).json()
    names = {f["display_name"] for f in listed}
    assert {"movie.mp4", "en.srt"} <= names


def _dl_id(token: str, tc: TestClient, info_hash: str | None = None) -> str:
    data = tc.get("/api/downloads", headers=_auth(token)).json()
    if info_hash:
        return next(d["id"] for d in data if d["info_hash"] == info_hash)
    return data[0]["id"]


def test_tree_requires_ownership(client) -> None:
    tc, _root = client
    owner_id = _seed_user("treeowner")
    _seed_user("intruder")
    _seed_download(owner_id, IH_A)

    towner = _login(tc, "treeowner")
    tintr = _login(tc, "intruder")
    dl = _dl_id(towner, tc)
    assert tc.get(f"/api/files/tree?download_id={dl}", headers=_auth(tintr)).status_code == 404
    assert tc.get(f"/api/files/tree?download_id={dl}", headers=_auth(towner)).status_code == 200


def test_stream_requires_ownership_and_supports_ranges(client) -> None:
    tc, _root = client
    owner_id = _seed_user("streamowner")
    _seed_user("sneak")
    _seed_download(owner_id, IH_A)

    towner = _login(tc, "streamowner")
    tsneak = _login(tc, "sneak")

    # sync (tree) registers the torrent's files as records
    assert tc.get(
        f"/api/files/tree?download_id={_dl_id(towner, tc)}", headers=_auth(towner)
    ).status_code == 200

    # register + fetch a file id via metadata endpoint after sync
    listed = tc.get("/api/files", headers=_auth(towner)).json()
    target = next(f for f in listed if f["display_name"] == "movie.mp4")
    fid = target["id"]

    # foreign user cannot see or stream it
    assert tc.get(f"/api/files/{fid}", headers=_auth(tsneak)).status_code == 404
    assert tc.get(f"/api/files/{fid}/stream", headers=_auth(tsneak)).status_code == 404

    # full GET returns whole content (proxy stubbed below via monkeypatch of
    # httpx in files_service? simpler: read directly since files server absent ->
    # we instead validate range path by patching proxy target to local disk).
    # For unit coverage here, patch proxy_stream's upstream with a local file URL.
    from unittest.mock import patch

    from fastapi.responses import StreamingResponse

    def fake_proxy(relative_path: str, range_header: str | None):
        import urllib.parse

        from app.api.files_service import safe_join
        from app.core.config import get_settings

        rel = urllib.parse.unquote(relative_path)
        full = safe_join(get_settings().download_dir, rel)
        size = full.stat().st_size
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):]
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else max(0, size - int(end_s))
            end = int(end_s) if end_s else size - 1
            payload = full.read_bytes()[start:end + 1]
            return StreamingResponse(
                iter([payload]), status_code=206,
                headers={"content-range": f"bytes {start}-{end}/{size}",
                         "accept-ranges": "bytes"},
            )
        return StreamingResponse(iter([full.read_bytes()]), status_code=200,
                                 headers={"accept-ranges": "bytes"})

    with patch("app.api.files.proxy_stream", side_effect=fake_proxy):
        full_resp = tc.get(f"/api/files/{fid}/stream", headers=_auth(towner))
        assert full_resp.status_code == 200
        assert full_resp.content == b"0123456789abcdef"

        ranged = tc.get(f"/api/files/{fid}/stream", headers={**_auth(towner),
                                                          "Range": "bytes=0-3"})
        assert ranged.status_code == 206
        assert ranged.content == b"0123"
        assert ranged.headers["content-range"] == "bytes 0-3/16"


def test_delete_rules(client, tmp_path: Path) -> None:
    tc, root = client
    owner_id = _seed_user("delowner")
    _seed_download(owner_id, IH_A, is_deletion_allowed=True)
    token = _login(tc, "delowner")

    tc.get(f"/api/files/tree?download_id={_dl_id(token, tc)}", headers=_auth(token))
    listed = tc.get("/api/files", headers=_auth(token)).json()
    srt = next(f for f in listed if f["display_name"] == "en.srt")

    resp = tc.delete(f"/api/files/{srt['id']}", headers=_auth(token))
    assert resp.status_code == 204
    assert not (root / IH_A / "subs" / "en.srt").exists()

    # second delete -> record gone -> 404
    assert tc.delete(f"/api/files/{srt['id']}", headers=_auth(token)).status_code == 404

    # deletion-forbidden download blocks file delete
    _seed_download(owner_id, "c" * 40, is_deletion_allowed=False)
    (root / ("c" * 40)).mkdir()
    (root / ("c" * 40) / "x.bin").write_bytes(b"x")
    token2 = token
    tc.get(f"/api/files/tree?download_id={_dl_id(token2, tc)}", headers=_auth(token2))
    listed = tc.get("/api/files", headers=_auth(token)).json()
    xbin = next(f for f in listed if f["display_name"] == "x.bin")
    assert tc.delete(f"/api/files/{xbin['id']}", headers=_auth(token)).status_code == 403


def test_admin_sees_all_files(client) -> None:
    tc, root = client

    uid = _seed_user("adminfiles", role=UserRole.ADMIN)
    _seed_user("plainuser")
    _seed_download(uid, IH_A)
    other = _user_id_by_name("plainuser")
    _seed_download(other, "b" * 40)

    bdir = root / ("b" * 40)
    bdir.mkdir(parents=True, exist_ok=True)
    (bdir / "other.txt").write_text("x")

    tadmin = _login(tc, "adminfiles")
    tuser = _login(tc, "plainuser")

    # user syncs own download; admin syncs theirs
    tc.get(f"/api/files/tree?download_id={_dl_id(tuser, tc, 'b' * 40)}", headers=_auth(tuser))
    tc.get(f"/api/files/tree?download_id={_dl_id(tadmin, tc, IH_A)}", headers=_auth(tadmin))

    seen_by_user = {f["display_name"] for f in tc.get("/api/files", headers=_auth(tuser)).json()}
    assert "other.txt" in seen_by_user          # owns b*40
    assert "movie.mp4" not in seen_by_user      # admin's file hidden

    seen_by_admin = {f["display_name"] for f in tc.get("/api/files", headers=_auth(tadmin)).json()}
    assert "movie.mp4" in seen_by_admin         # owns a*40
    assert "other.txt" in seen_by_admin         # admins see all users' files


def _root():
    from app.core.config import get_settings

    return get_settings().download_dir


def _user_id_by_name(name: str):
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import get_settings

    async def run():
        e = create_async_engine(str(get_settings().database_url))
        async with async_sessionmaker(e)() as s:
            uid = (await s.execute(select(User.id).where(User.username == name))).scalar_one()
        await e.dispose()
        return uid

    return asyncio.run(run())
