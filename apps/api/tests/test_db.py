"""End-to-end database tests against the real local PostgreSQL.

These require the `torlink` database to exist and migrations applied
(`alembic upgrade head`). Each test runs in its own transaction which is
rolled back, so no data persists between tests.
"""

from collections.abc import AsyncIterator

import pytest
import sqlalchemy.exc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401
from app.db.engine import dispose_engine, get_engine
from app.models import (
    ApiKey,
    AuditAction,
    AuditLog,
    DownloadRecord,
    DownloadStatus,
    FileRecord,
    RefreshToken,
    User,
    UserRole,
    UserSetting,
)


@pytest.fixture(autouse=True)
async def _fresh_engine() -> AsyncIterator[None]:
    """Give each test its own engine: asyncpg conns bind to one event loop."""

    yield
    await dispose_engine()


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """Session bound to a transaction that is rolled back after the test."""

    engine = get_engine()
    async with engine.connect() as conn:
        trans = await conn.begin()
        factory = async_sessionmaker(conn, expire_on_commit=False)
        async with factory() as session:
            try:
                yield session
            finally:
                await trans.rollback()


async def test_all_tables_created() -> None:
    engine = get_engine()
    async with engine.connect() as conn:
        tables = await conn.run_sync(
            lambda sync_conn: set(
                sync_conn.dialect.get_table_names(sync_conn)
            )
        )
    expected = {
        "users",
        "refresh_tokens",
        "api_keys",
        "download_records",
        "file_records",
        "user_settings",
        "audit_logs",
    }
    assert expected.issubset(tables)


async def test_create_user_with_role(db_session: AsyncSession) -> None:
    user = User(
        email="admin@example.com",
        username="admin",
        password_hash="placeholder-argon2-hash",
        role=UserRole.ADMIN,
    )
    db_session.add(user)
    await db_session.flush()

    result = await db_session.execute(
        select(User).where(User.username == "admin")
    )
    loaded = result.scalar_one()

    assert loaded.id is not None
    assert loaded.role is UserRole.ADMIN
    assert loaded.is_admin
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_unique_email_enforced(db_session: AsyncSession) -> None:
    db_session.add(
        User(email="dup@example.com", username="a", password_hash="x")
    )
    db_session.add(
        User(email="dup@example.com", username="b", password_hash="x")
    )
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.flush()


async def test_user_with_full_relationship_graph(db_session: AsyncSession) -> None:
    user = User(
        email="owner@example.com", username="owner", password_hash="x"
    )
    db_session.add(user)
    await db_session.flush()

    download = DownloadRecord(
        user_id=user.id,
        info_hash="abcdef0123456789" * 4,
        name="Test torrent",
        status=DownloadStatus.ACTIVE,
    )
    db_session.add(download)
    await db_session.flush()

    db_session.add(
        FileRecord(
            user_id=user.id,
            download_record_id=download.id,
            relative_path="dir/file.iso",
            display_name="file.iso",
            size_bytes=1024,
            mime_type="application/octet-stream",
        )
    )
    db_session.add(
        RefreshToken(
            user_id=user.id,
            token_hash="tok-hash",
            expires_at=__import__("datetime").datetime(2030, 1, 1),
        )
    )
    db_session.add(ApiKey(user_id=user.id, name="ci-key", token_hash="key-hash"))
    db_session.add(UserSetting(user_id=user.id, settings={"theme": "dark"}))
    db_session.add(
        AuditLog(
            user_id=user.id,
            action=AuditAction.DOWNLOAD_ADDED,
            detail={"info_hash": download.info_hash},
        )
    )
    await db_session.flush()

    files = (await db_session.execute(
        select(FileRecord).where(FileRecord.user_id == user.id)
    )).scalars().all()
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.user_id == user.id)
    )).scalars().all()

    assert len(files) == 1
    assert files[0].download_record_id == download.id
    assert len(logs) == 1
    # Readonly role helpers
    ro = User(
        email="ro@example.com", username="ro", password_hash="x", role=UserRole.READONLY
    )
    assert not ro.can_download
    regular = User(email="u@example.com", username="u", password_hash="x")
    assert regular.role is UserRole.USER  # default applied at flush-time by ORM default
    db_session.add(regular)
    await db_session.flush()
    assert regular.can_download


async def test_download_record_defaults(db_session: AsyncSession) -> None:
    user = User(email="d@example.com", username="dl", password_hash="x")
    db_session.add(user)
    await db_session.flush()
    record = DownloadRecord(user_id=user.id, info_hash="ff" * 32)
    db_session.add(record)
    await db_session.flush()
    assert record.status is DownloadStatus.PENDING
