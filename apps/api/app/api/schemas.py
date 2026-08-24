"""Pydantic schemas for auth and user management."""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.user import UserRole


class UserCreate(BaseModel):
    """Admin-supplied payload when creating a user."""

    email: EmailStr
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.USER
    display_name: str | None = Field(default=None, max_length=128)


class UserOut(BaseModel):
    """Public shape of a user — never includes the password hash."""

    id: uuid.UUID
    email: str
    username: str
    role: UserRole
    is_active: bool
    display_name: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LoginRequest(BaseModel):
    username_or_email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_at: datetime


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)
