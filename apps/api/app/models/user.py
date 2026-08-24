"""User account model with role-based access."""

import enum

from sqlalchemy import Boolean, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserRole(enum.StrEnum):
    """Application roles. RBAC decisions are centralized around this enum."""

    ADMIN = "admin"
    USER = "user"
    READONLY = "readonly"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Application user. Admin-created; no public signup in Phase 1-3."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    # Argon2 hash stored here from Phase 3 onward.
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role", native_enum=False),
        default=UserRole.USER,
        nullable=False,
        index=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)
        if self.role is None:
            self.role = UserRole.USER
        if self.is_active is None:
            self.is_active = True

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User {self.username} role={self.role.value}>"

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    @property
    def can_download(self) -> bool:
        return self.role in (UserRole.ADMIN, UserRole.USER)
