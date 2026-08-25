"""Authentication and user-management routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenPair,
    UserCreate,
    UserOut,
)
from app.core.auth_deps import get_current_user, lookup_user_by_login, require_admin
from app.core.config import get_settings
from app.core.ratelimit import login_limiter
from app.core.security import create_access_token, hash_password, verify_password
from app.core.tokens import (
    issue_refresh_token,
    revoke_all_user_tokens,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.db.engine import get_db_session
from app.models.audit_log import AuditAction, AuditLog
from app.models.user import User, UserRole

router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])


def _client_meta(request: Request) -> dict:
    return {
        "ip": request.client.host if request.client else None,
        "ua": request.headers.get("user-agent", "")[:256],
    }


async def _audit(
    session: AsyncSession,
    action: AuditAction,
    user_id: uuid.UUID | None = None,
    detail: dict | None = None,
    ip: str | None = None,
    ua: str | None = None,
) -> None:
    session.add(
        AuditLog(action=action, user_id=user_id, detail=detail, ip_address=ip, user_agent=ua)
    )
    await session.commit()


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TokenPair:
    if get_settings().rate_limit_enabled:
        login_limiter.check(request)
    meta = _client_meta(request)
    user = await lookup_user_by_login(session, payload.username_or_email)

    # Same generic message for unknown user and wrong password: no user enumeration.
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password, user.password_hash)
    ):
        await _audit(
            session, AuditAction.LOGIN_FAILED, user.id if user else None,
            {"login": payload.username_or_email[:64]}, meta["ip"], meta["ua"],
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    access, expires = create_access_token(user.id, user.role.value)
    refresh_raw, _ = await issue_refresh_token(session, user.id, meta["ua"])
    await session.commit()
    await _audit(session, AuditAction.LOGIN_SUCCEEDED, user.id, None, meta["ip"], meta["ua"])
    return TokenPair(access_token=access, refresh_token=refresh_raw, expires_at=expires)


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest, request: Request, session: AsyncSession = Depends(get_db_session)
) -> TokenPair:
    meta = _client_meta(request)
    result = await rotate_refresh_token(session, payload.refresh_token, meta["ua"])
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        )
    user_id, new_raw, new_expires = result

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User inactive")

    access, expires = create_access_token(user.id, user.role.value)
    return TokenPair(access_token=access, refresh_token=new_raw, expires_at=expires)


@router.post("/logout", status_code=204)
async def logout(
    payload: RefreshRequest, session: AsyncSession = Depends(get_db_session)
) -> None:
    await revoke_refresh_token(session, payload.refresh_token)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


# ---- Admin: user management -------------------------------------------------


@users_router.post("", response_model=UserOut, status_code=201)
async def create_user(
    payload: UserCreate,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    dup = await lookup_user_by_login(session, payload.email) or await lookup_user_by_login(
        session, payload.username
    )
    if dup is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already exists")

    user = User(
        email=payload.email.lower(),
        username=payload.username,
        password_hash=hash_password(payload.password),
        role=payload.role,
        display_name=payload.display_name,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    await _audit(session, AuditAction.USER_CREATED, admin.id, {"new_user": str(user.id)})
    return user


@users_router.get("", response_model=list[UserOut])
async def list_users(
    offset: int = 0,
    limit: int = 50,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> list[User]:
    limit = min(limit, 200)
    stmt = select(User).order_by(User.created_at).offset(offset).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


@users_router.patch("/{user_id}/role", response_model=UserOut)
async def change_role(
    user_id: uuid.UUID,
    role: UserRole,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id and role is not UserRole.ADMIN:
        raise HTTPException(status_code=400, detail="Admins cannot demote themselves")
    old = user.role
    user.role = role
    await session.commit()
    await session.refresh(user)
    await _audit(
        session, AuditAction.USER_UPDATED, admin.id,
        {"target": str(user.id), "old_role": old.value, "new_role": role.value},
    )
    return user


@users_router.patch("/{user_id}/deactivate", response_model=UserOut)
async def deactivate_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot deactivate themselves")
    user.is_active = False
    await session.commit()
    n_revoked = await revoke_all_user_tokens(session, user.id)
    await _audit(
        session, AuditAction.USER_UPDATED, admin.id,
        {"target": str(user.id), "action": "deactivate", "tokens_revoked": n_revoked},
    )
    await session.refresh(user)
    return user


@users_router.delete("/{user_id}", status_code=204)
async def delete_user(
    user_id: uuid.UUID,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Admins cannot delete themselves")
    await session.delete(user)
    await session.commit()
    await _audit(session, AuditAction.USER_DELETED, admin.id, {"target": str(user_id)})


# keep settings import referenced for future token TTL config exposure
_ = get_settings
