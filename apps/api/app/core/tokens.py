"""Refresh-token lifecycle: issue, rotate, revoke.

Raw tokens are shown once at login; only SHA-256 hashes are stored.
Rotation on every refresh limits the blast radius of a stolen token.
"""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.refresh_token import RefreshToken


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def issue_refresh_token(
    session: AsyncSession, user_id: uuid.UUID, user_agent: str | None = None
) -> tuple[str, datetime]:
    """Create a new refresh token row; return (raw_token, expires_at)."""

    settings = get_settings()
    raw = secrets.token_urlsafe(48)
    expires = datetime.now(UTC).replace(tzinfo=None) + timedelta(
        days=settings.refresh_token_days
    )
    session.add(
        RefreshToken(
            user_id=user_id,
            token_hash=_hash(raw),
            expires_at=expires,
            user_agent=user_agent,
        )
    )
    return raw, expires


async def rotate_refresh_token(
    session: AsyncSession, raw: str, user_agent: str | None = None
) -> tuple[uuid.UUID, str, datetime] | None:
    """Validate a presented refresh token and replace it with a new one.

    Returns (user_id, new_raw_token, expires_at), or None when invalid,
    expired or already revoked. Revoked-at detection also catches reuse of
    a rotated token — a sign of theft worth auditing later.
    """

    stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash(raw))
    stored = (await session.execute(stmt)).scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        return None
    now = datetime.now(UTC).replace(tzinfo=None)
    if stored.expires_at <= now:
        return None

    stored.revoked_at = now  # single use: old token dies immediately
    new_raw, expires = await issue_refresh_token(session, stored.user_id, user_agent)
    await session.commit()
    return stored.user_id, new_raw, expires


async def revoke_refresh_token(session: AsyncSession, raw: str) -> bool:
    """Revoke one token (logout). Returns True when a live token was found."""

    stmt = select(RefreshToken).where(RefreshToken.token_hash == _hash(raw))
    stored = (await session.execute(stmt)).scalar_one_or_none()
    if stored is None or stored.revoked_at is not None:
        return False
    stored.revoked_at = datetime.now(UTC).replace(tzinfo=None)
    await session.commit()
    return True


async def revoke_all_user_tokens(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Revoke every live refresh token for a user (password reset, ban)."""

    stmt = select(RefreshToken).where(
        RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)
    )
    live = (await session.execute(stmt)).scalars().all()
    now = datetime.now(UTC).replace(tzinfo=None)
    for token in live:
        token.revoked_at = now
    await session.commit()
    return len(live)
