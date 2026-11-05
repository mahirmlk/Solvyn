"""User authentication endpoints.

POST /api/v1/auth/register  — create a user (bring-your-own org isolation)
POST /api/v1/auth/login     — email + password → access + refresh tokens
POST /api/v1/auth/refresh   — rotate refresh token → new token pair
POST /api/v1/auth/logout    — revoke the refresh token
GET  /api/v1/auth/me        — identity + organization memberships
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Organization, OrganizationMembership, RefreshToken, Tenant, User
from solvyn.db.session import get_session
from solvyn.identity.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    refresh_expiry,
    verify_password,
)
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# Constant-time defense against email enumeration on the login path.
_DUMMY_PASSWORD_HASH = hash_password("dummy-password-for-constant-time-checks")


class RegisterRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=255)


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    is_active: bool
    is_superuser: bool
    created_at: str


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _load_user_by_email(db: AsyncSession, email: str) -> User | None:
    normalized = email.strip().lower()
    result = await db.execute(select(User).where(User.email == normalized))
    return result.scalar_one_or_none()


def _create_refresh_token(user: User) -> tuple[RefreshToken, str]:
    raw_refresh = generate_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        expires_at=refresh_expiry(),
    )
    return row, raw_refresh


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.post("/register", response_model=UserOut, status_code=201, summary="Register a new user")
async def register(payload: RegisterRequest, request: Request, db: AsyncSession = Depends(get_session)) -> UserOut:
    from solvyn.config import settings

    if not settings.allow_public_registration:
        token = request.headers.get("X-Bootstrap-Token")
        if not settings.bootstrap_token or token != settings.bootstrap_token:
            raise HTTPException(status_code=403, detail="Public registration is disabled")

    email = payload.email.strip().lower()
    existing = await _load_user_by_email(db, email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    user = User(email=email, password_hash=password_hash, display_name=payload.display_name)
    db.add(user)
    await db.flush()  # user.id is needed for the slug/tid in the same transaction

    # Every registrant gets an isolated organization bound to a fresh tenant —
    # never a pre-existing organization and never implicit superuser rights.
    tenant = Tenant(
        name=(payload.display_name or email)[:255],
        slug=f"user-{user.id.hex}",
        api_key_hash="",
        role="user",
    )
    db.add(tenant)
    await db.flush()

    org = Organization(name=(payload.display_name or email)[:255], slug=f"org-{user.id.hex}", tenant_id=tenant.id)
    db.add(org)
    await db.flush()

    db.add(OrganizationMembership(user_id=user.id, organization_id=org.id, role="owner"))
    await db.commit()

    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name or "",
        is_active=bool(user.is_active),
        is_superuser=bool(user.is_superuser),
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


# ---------------------------------------------------------------------------
# Login / refresh / logout
# ---------------------------------------------------------------------------


@router.post("/login", response_model=TokenPair, summary="Log in with email + password")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_session)) -> TokenPair:
    from solvyn.config import settings

    user = await _load_user_by_email(db, payload.email)
    if user is None or not user.is_active:
        # Verify against a dummy hash so response time does not reveal whether the email exists.
        verify_password(payload.password, _DUMMY_PASSWORD_HASH)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Single transaction: last_login_at + refresh token rotation pair commit together.
    user.last_login_at = datetime.now(UTC)
    db.add(user)

    access = create_access_token(subject=str(user.id))
    row, raw_refresh = _create_refresh_token(user)
    db.add(row)
    await db.commit()
    return TokenPair(access_token=access, refresh_token=raw_refresh, expires_in_minutes=settings.access_token_ttl_minutes)


@router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_session)) -> TokenPair:
    from solvyn.config import settings

    token_hash = hash_token(payload.refresh_token)
    now = datetime.now(UTC)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if row.revoked_at is not None:
        # Reuse detection: revoking the token family limits blast radius of a stolen token.
        await db.execute(
            update(RefreshToken).where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None)).values(revoked_at=now)
        )
        await db.commit()
        logger.warning("Refresh token reuse detected for user %s — family revoked", row.user_id)
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    if _aware(row.expires_at) <= now:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    row.revoked_at = now
    access = create_access_token(subject=str(user.id))
    new_row, raw_refresh = _create_refresh_token(user)
    row.replaced_by_id = new_row.id
    db.add_all([row, new_row])
    await db.commit()
    return TokenPair(access_token=access, refresh_token=raw_refresh, expires_in_minutes=settings.access_token_ttl_minutes)


@router.post("/logout", summary="Revoke a refresh token")
async def logout(payload: RefreshRequest, db: AsyncSession = Depends(get_session)) -> dict[str, bool]:
    token_hash = hash_token(payload.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    row = result.scalar_one_or_none()
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        db.add(row)
        await db.commit()
    return {"ok": True}


async def get_current_user(db: AsyncSession = Depends(get_session)) -> User:
    """Resolve the JWT subject into a User, or raise 401."""
    user_id = tenant_context.user_id
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


@router.get("/me", response_model=UserOut, summary="Current user identity")
async def me(db: AsyncSession = Depends(get_session)) -> UserOut:
    user = await get_current_user(db)
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name or "",
        is_active=bool(user.is_active),
        is_superuser=bool(user.is_superuser),
        created_at=user.created_at.isoformat() if user.created_at else "",
    )
