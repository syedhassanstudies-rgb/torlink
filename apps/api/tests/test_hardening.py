"""Hardening tests: rate limits, body-size cap, CORS, production checks."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient


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


import asyncio  # noqa: E402
from collections.abc import Iterator  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.ratelimit import login_limiter  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.engine import dispose_engine  # noqa: E402
from app.models.user import User, UserRole  # noqa: E402


def test_rate_limiter_blocks_after_budget() -> None:
    """5 logins/min per IP: the 6th must be a 429."""

    def seed():
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        async def _run():
            engine = create_async_engine(str(get_settings().database_url))
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as s:
                s.add(User(
                    email="rl@example.com",
                    username="rl",
                    password_hash=hash_password("correct-horse-battery"),
                    role=UserRole.USER,
                ))
                await s.commit()
            await engine.dispose()

        asyncio.run(_run())

    seed()

    from app.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        codes = []
        for _ in range(6):
            r = tc.post("/api/auth/login", json={
                "username_or_email": "rl", "password": "correct-horse-battery",
            })
            codes.append(r.status_code)
        assert codes[:5] == [200] * 5
        assert codes[5] == 429
    login_limiter.reset()


def test_body_size_limit_returns_413() -> None:
    from app.main import create_app

    app = create_app()
    with TestClient(app) as tc:
        big = "x" * (get_settings().max_body_bytes + 10)
        r = tc.post("/api/auth/login", json={
            "username_or_email": big, "password": "y",
        })
        assert r.status_code == 413


def test_cors_off_by_default_and_on_when_configured(monkeypatch) -> None:
    from app.main import create_app

    # Default: no CORS headers at all.
    app = create_app()
    with TestClient(app) as tc:
        r = tc.get("/api/health", headers={"Origin": "https://evil.example"})
        assert r.headers.get("access-control-allow-origin") is None

    # Configured: only the allowed origin gets the header.
    old_origins = get_settings().__dict__.get("cors_origins")
    get_settings().__dict__["cors_origins"] = ["https://app.example"]
    app2 = create_app()
    with TestClient(app2) as tc2:
        r = tc2.get("/api/health", headers={"Origin": "https://evil.example"})
        assert r.headers.get("access-control-allow-origin") is None
        r = tc2.get("/api/health", headers={"Origin": "https://app.example"})
        assert r.headers.get("access-control-allow-origin") == "https://app.example"
    if old_origins is None:
        get_settings().__dict__.pop("cors_origins", None)
        # pydantic needs a value; restore to the model default (empty list).
        get_settings().__dict__["cors_origins"] = []


def test_production_refuses_default_jwt_secret(monkeypatch) -> None:
    s = get_settings()
    old_env, old_secret = s.environment, s.jwt_secret
    get_settings().__dict__["environment"] = "production"
    get_settings().__dict__["jwt_secret"] = (
        "dev-secret-change-me-0123456789abcdef0123456789abcdef"
    )
    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            import app.main  # noqa: F401 - module-level create_app() may raise

            app.main.create_app()
    finally:
        get_settings().__dict__["environment"] = old_env
        get_settings().__dict__["jwt_secret"] = old_secret


def test_token_scrub_filter_redacts_query_tokens() -> None:
    from app.core.hardening import TokenScrubFilter

    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg='GET /ws/downloads?token=abc.def.ghi HTTP/1.1" 101', args=(),
        exc_info=None,
    )
    TokenScrubFilter().filter(record)
    assert "abc.def.ghi" not in record.getMessage()
    assert "token=REDACTED" in record.getMessage()
