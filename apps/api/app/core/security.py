"""Password hashing (Argon2) and JWT access-token helpers."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import HTTPException, status

from app.core.config import get_settings

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    """Hash a password with Argon2id."""

    return _hasher.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time password verification; returns False on any mismatch."""

    try:
        return _hasher.verify(hashed, plain)
    except VerifyMismatchError:
        return False
    except Exception:
        # Malformed hash in DB etc. — treat as failed login, never crash.
        return False


def create_access_token(user_id: uuid.UUID, role: str) -> tuple[str, datetime]:
    """Return (token, expires_at) for a short-lived access token."""

    settings = get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=settings.access_token_minutes)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "type": "access",
    }
    token = pyjwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    return token, expires


def decode_access_token(token: str) -> dict[str, Any]:
    """Decode and validate an access token; raises pyjwt errors on failure."""

    settings = get_settings()
    payload = pyjwt.decode(
        token,
        settings.jwt_secret,
        algorithms=[settings.jwt_algorithm],
    )
    if payload.get("type") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Wrong token type",
        )
    return payload
