"""Per-user search history."""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SearchHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per executed search query (stored only when the user allows it).

    Opt-out lives in user_settings.settings['save_search_history'] = False;
    default is to store. Kept small: query text, hit count, no results blob.
    """

    __tablename__ = "search_history"
    __table_args__ = (
        Index("ix_search_history_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
