"""Audit log for security-relevant and destructive actions."""

import enum
import uuid

from sqlalchemy import Enum, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AuditAction(enum.StrEnum):
    """Coarse action categories worth auditing."""

    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DELETED = "user.deleted"
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    DOWNLOAD_ADDED = "download.added"
    DOWNLOAD_PAUSED = "download.paused"
    DOWNLOAD_RESUMED = "download.resumed"
    DOWNLOAD_SEEDING_STARTED = "download.seeding_started"
    DOWNLOAD_SEEDING_STOPPED = "download.seeding_stopped"
    DOWNLOAD_REMOVED = "download.removed"
    DOWNLOAD_DELETED = "download.deleted"  # data deletion: always audit
    FILE_DELETED = "file.deleted"
    API_KEY_CREATED = "apikey.created"
    API_KEY_REVOKED = "apikey.revoked"
    SETTINGS_UPDATED = "settings.updated"


class AuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only trail of who did what. Never updated or deleted by app code."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
    )

    # Null for anonymous events such as failed logins with no valid user.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action", native_enum=False),
        nullable=False,
    )
    # Client-reported IP / user agent at event time.
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(256))
    detail: Mapped[dict | None] = mapped_column(JSONB)
