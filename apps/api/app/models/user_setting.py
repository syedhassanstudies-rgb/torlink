"""Per-user application settings."""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Flexible per-user key/value preferences.

    A single row per user with a JSONB payload keeps the schema stable
    while settings evolve; promote individual hot keys to columns later
    if query patterns demand it.
    """

    __tablename__ = "user_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
