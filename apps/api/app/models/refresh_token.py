"""Refresh-token / session model for login session management."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A refresh token granting continued access after an access token expires.

    Tokens are stored hashed (Phase 3 fills in the hashing scheme); raw
    tokens are never persisted. Revocation is explicit so stolen sessions
    can be killed server-side.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime())
    # Free-form client label ("Chrome on laptop") for user-facing session lists.
    user_agent: Mapped[str | None] = mapped_column(String(256))
