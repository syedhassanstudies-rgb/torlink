"""Application-level download record.

FastAPI owns this metadata (ownership, labels, audit) while the torlink
Node daemon remains responsible for actual torrent execution. The mapping
between a record here and a torlink queue item is the info_hash plus the
daemon-assigned torrent id captured at creation time.
"""

import enum
import uuid

from sqlalchemy import BigInteger, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DownloadStatus(enum.StrEnum):
    """Application-side lifecycle, kept coarse; fine-grained progress comes
    live from the torlink daemon via the realtime layer."""

    PENDING = "pending"      # accepted by FastAPI, not yet handed to daemon
    ACTIVE = "active"        # handed to daemon and running/queued there
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    REMOVED = "removed"      # soft-deleted record


class DownloadRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One user-requested download."""

    __tablename__ = "download_records"
    __table_args__ = (
        # Hot query paths: per-user lists filtered by status, newest first.
        Index("ix_download_records_user_status_created", "user_id", "status", "created_at"),
        Index("ix_download_records_info_hash", "info_hash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    info_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Torrent name as reported by the source/daemon at add time.
    name: Mapped[str | None] = mapped_column(String(512))
    status: Mapped[DownloadStatus] = mapped_column(
        Enum(DownloadStatus, name="download_status", native_enum=False),
        default=DownloadStatus.PENDING,
        nullable=False,
    )
    # Torlink daemon torrent id, when the daemon has accepted it.
    torlink_torrent_id: Mapped[str | None] = mapped_column(String(128))
    magnet_uri: Mapped[str | None] = mapped_column(String(2048))
    category: Mapped[str | None] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<DownloadRecord {self.info_hash} {self.status.value}>"
