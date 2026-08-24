"""File records for completed downloads.

Files are exposed to users only through these records — never raw
filesystem paths. The stored path is server-internal; public identifiers
are the UUIDs. Streaming/deletion authorization resolves ownership via
the parent download record.
"""

import uuid

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class FileRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One file belonging to one download record."""

    __tablename__ = "file_records"
    __table_args__ = (
        UniqueConstraint(
            "download_record_id", "relative_path", name="uq_file_records_path"
        ),
        Index("ix_file_records_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    download_record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("download_records.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # Path relative to the download's directory. Server-internal absolute
    # paths are resolved from this at request time, never sent to clients.
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(128))
