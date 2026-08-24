"""End-to-end auth tests: login, tokens, refresh rotation, RBAC, admin users.

Runs against the real local PostgreSQL `torlink` database. Tables are
TRUNCATED before each test (fast and deterministic); seeded rows are real
commits, which also matches production semantics better than savepoints.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401
from app.core.security import hash_password
from app.db.engine import dispose_engine
from app.models.user import User, UserRole


@pytest.fixture(autouse=True)
def _clean_db() -> Iterator[None]:
    """Truncate all tables before each test via psql (loop-independent)."""

    import os
    import subprocess

    from app.core.config import get_settings

    url = str(get_settings().database_url)  # postgresql+asyncpg://user:pw@host/db
    rest = url.split("://", 1)[1]
    userinfo, hostport_db = rest.rsplit("@", 1)
    user, password = userinfo.split(":", 1)
    host, port_db = hostport_db.split(":", 1)
    port, dbname = port_db.split("/", 1)

    env = os.environ.copy()
    env["PGPASSWORD"] = password
    subprocess.run(
        [
            "psql",
            "-U", user,
            "-h", host,
            "-p", port,
            "-d", dbname,
            "-c",
            "TRUNCATE users, refresh_tokens, api_keys, download_records, "
            "file_records, user_settings, audit_logs CASCADE",
        ],
        env=env,
        check=True,
    )
    yield
    # Engine disposal: the global engine may hold connections from the
    # previous test's loop; drop it so the next test builds fresh ones.
    import asyncio

    try:
        asyncio.run(dispose_engine())
    except Exception:
        pass


@pytest.fixture
def client() -> Iterator[tuple[TestClient, None]]:
    from app.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        yield tc, None


# Helpers ----------------------------------------------------------------------


def _mk_user(
    username: str,
    role: UserRole = UserRole.USER,
    password: str = "correct-horse-battery",
) -> None:
    """Seed a user with its own short-lived engine/loop (TestClient-safe)."""

    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core.config import get_settings

    async def _seed() -> None:
        engine = create_async_engine(str(get_settings().database_url))
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s:
            s.add(
                User(
                    email=f"{username}@example.com",
                    username=username,
                    password_hash=hash_password(password),
                    role=role,
                )
            )
            await s.commit()
        await engine.dispose()

    asyncio.run(_seed())


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _login(tc: TestClient, username: str, password: str = "correct-horse-battery"):
    return tc.post(
        "/api/auth/login", json={"username_or_email": username, "password": password}
    )


# ---- password hashing --------------------------------------------------------


def test_argon2_hash_roundtrip() -> None:
    from app.core.security import verify_password

    hashed = hash_password("s3cret-password")
    assert hashed.startswith("$argon2")
    assert verify_password("s3cret-password", hashed)
    assert not verify_password("wrong", hashed)


# ---- login -------------------------------------------------------------------


def test_login_success_returns_token_pair(client) -> None:
    tc, _ = client
    _mk_user("alice")

    resp = _login(tc, "alice")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert len(body["access_token"]) > 20
    assert len(body["refresh_token"]) > 20


def test_login_wrong_password_is_401_generic(client) -> None:
    tc, _ = client
    _mk_user("bob")

    for creds in (
        {"username_or_email": "bob", "password": "nope"},
        {"username_or_email": "ghost-user", "password": "whatever"},
    ):
        resp = tc.post("/api/auth/login", json=creds)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"


def test_me_requires_token(client) -> None:
    tc, _ = client
    assert tc.get("/api/auth/me").status_code == 401


def test_me_with_valid_token(client) -> None:
    tc, _ = client
    _mk_user("carol")

    access = _login(tc, "carol").json()["access_token"]
    resp = tc.get("/api/auth/me", headers=_auth(access))
    assert resp.status_code == 200
    data = resp.json()
    assert data["username"] == "carol"
    assert "password_hash" not in data


# ---- refresh rotation ---------------------------------------------------------


def test_refresh_rotates_and_old_token_dies(client) -> None:
    tc, _ = client
    _mk_user("dave")

    old_refresh = _login(tc, "dave").json()["refresh_token"]

    r1 = tc.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert r1.status_code == 200
    new_pair = r1.json()
    assert new_pair["refresh_token"] != old_refresh

    r2 = tc.post("/api/auth/refresh", json={"refresh_token": old_refresh})
    assert r2.status_code == 401

    r3 = tc.post("/api/auth/refresh", json={"refresh_token": new_pair["refresh_token"]})
    assert r3.status_code == 200


def test_logout_revokes_refresh(client) -> None:
    tc, _ = client
    _mk_user("erin")

    refresh_raw = _login(tc, "erin").json()["refresh_token"]

    assert tc.post("/api/auth/logout", json={"refresh_token": refresh_raw}).status_code == 204
    assert tc.post("/api/auth/refresh", json={"refresh_token": refresh_raw}).status_code == 401


# ---- RBAC + admin user management ---------------------------------------------


def _admin_headers(tc: TestClient) -> dict[str, str]:
    _mk_user("boss", role=UserRole.ADMIN)
    return _auth(_login(tc, "boss").json()["access_token"])


def test_admin_creates_user_and_regular_cannot(client) -> None:
    tc, _ = client
    admin_h = _admin_headers(tc)
    _mk_user("plainuser")
    user_h = _auth(_login(tc, "plainuser").json()["access_token"])

    payload = {
        "email": "newkid@example.com",
        "username": "newkid",
        "password": "strong-pass-123",
        "role": "user",
    }
    ok = tc.post("/api/users", json=payload, headers=admin_h)
    assert ok.status_code == 201, ok.text
    assert ok.json()["username"] == "newkid"

    denied = tc.post("/api/users", json={**payload, "username": "other"}, headers=user_h)
    assert denied.status_code == 403

    anon = tc.post("/api/users", json=payload)
    assert anon.status_code == 401

    dup = tc.post("/api/users", json=payload, headers=admin_h)
    assert dup.status_code == 409


def test_role_change_and_deactivation_flow(client) -> None:
    tc, _ = client
    admin_h = _admin_headers(tc)

    created = tc.post(
        "/api/users",
        json={
            "email": "temp@example.com",
            "username": "temp",
            "password": "temp-pass-123",
        },
        headers=admin_h,
    )
    uid = created.json()["id"]

    t_access = _login(tc, "temp", "temp-pass-123").json()["access_token"]

    role_resp = tc.patch(f"/api/users/{uid}/role?role=admin", headers=admin_h)
    assert role_resp.status_code == 200
    assert role_resp.json()["role"] == "admin"

    deact = tc.patch(f"/api/users/{uid}/deactivate", headers=admin_h)
    assert deact.status_code == 200

    me_after = tc.get("/api/auth/me", headers=_auth(t_access))
    assert me_after.status_code == 401

    blocked = _login(tc, "temp", "temp-pass-123")
    assert blocked.status_code == 401


def test_list_users_paginated(client) -> None:
    tc, _ = client
    admin_h = _admin_headers(tc)

    for i in range(5):
        _mk_user(f"bulk{i}")

    page = tc.get("/api/users?offset=0&limit=3", headers=admin_h)
    assert page.status_code == 200
    assert len(page.json()) == 3

    page2 = tc.get("/api/users?offset=3&limit=3", headers=admin_h)
    assert page2.status_code == 200
    assert len(page2.json()) >= 2


def test_garbage_tokens_rejected(client) -> None:
    tc, _ = client
    assert tc.get("/api/auth/me", headers=_auth("not.a.jwt")).status_code == 401
