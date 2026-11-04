"""User authentication endpoints.

POST /api/v1/auth/register  — create a user (and, on first registration, the
                              default organization + workspace)
POST /api/v1/auth/login     — email + password → access + refresh tokens
POST /api/v1/auth/refresh   — rotate refresh token → new token pair
POST /api/v1/auth/logout    — revoke the refresh token
GET  /api/v1/auth/me        — identity + organization memberships
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Organization, OrganizationMembership, RefreshToken, Tenant, User
from solvyn.db.session import get_session
from solvyn.identity.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    refresh_expiry,
    verify_password,
)
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(prefix="/auth", tags=["auth"])


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


async def _load_user_by_email(db: AsyncSession, email: str) -> User | None:
    normalized = email.strip().lower()
    result = await db.execute(select(User).where(User.email == normalized))
    return result.scalar_one_or_none()


async def _issue_tokens(db: AsyncSession, user: User) -> TokenPair:
    from solvyn.config import settings

    access = create_access_token(subject=str(user.id))
    raw_refresh = generate_refresh_token()
    row = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        expires_at=refresh_expiry(),
    )
    db.add(row)
    await db.commit()
    return TokenPair(
        access_token=access,
        refresh_token=raw_refresh,
        expires_in_minutes=settings.access_token_ttl_minutes,
    )


async def _ensure_default_organization(db: AsyncSession, user: User) -> Organization:
    """First-registered user becomes owner of a default organization.

    The organization is bound to the caller's tenant when one is resolved
    from X-Tenant-Slug; otherwise a fresh tenant row is created so the
    organization always has a isolation boundary.
    """
    result = await db.execute(select(Organization).limit(1))
    org = result.scalar_one_or_none()
    if org is not None:
        return org

    tenant_id = tenant_context.tenant_id
    if tenant_id is None:
        tenant = Tenant(name=f"{user.display_name or user.email}'s tenant", slug=f"user-{user.id.hex[:8]}", api_key_hash="", role="user")
        db.add(tenant)
        await db.flush()
        tenant_id = tenant.id

    org = Organization(name=f"{user.display_name or user.email}'s organization", slug=f"org-{user.id.hex[:8]}", tenant_id=tenant_id)
    db.add(org)
    await db.flush()
    membership = OrganizationMembership(user_id=user.id, organization_id=org.id, role="owner")
    db.add(membership)
    await db.commit()
    return org


@router.post("/register", response_model=UserOut, status_code=201, summary="Register a new user")
async def register(payload: RegisterRequest, db: AsyncSession = Depends(get_session)) -> UserOut:
    email = payload.email.strip().lower()
    existing = await _load_user_by_email(db, email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    try:
        password_hash = hash_password(payload.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    first = (await db.execute(select(User.id).limit(1))).scalar_one_or_none() is None
    user = User(email=email, password_hash=password_hash, display_name=payload.display_name, is_superuser=first)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await _ensure_default_organization(db, user)
    return UserOut(
        id=str(user.id),
        email=user.email,
        display_name=user.display_name or "",
        is_active=bool(user.is_active),
        is_superuser=bool(user.is_superuser),
        created_at=user.created_at.isoformat() if user.created_at else "",
    )


@router.post("/login", response_model=TokenPair, summary="Log in with email + password")
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_session)) -> TokenPair:
    user = await _load_user_by_email(db, payload.email)
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user.last_login_at = datetime.now(UTC)
    db.add(user)
    tokens = await _issue_tokens(db, user)
    return tokens


@router.post("/refresh", response_model=TokenPair, summary="Rotate a refresh token")
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_session)) -> TokenPair:
    token_hash = hash_token(payload.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    row = result.scalar_one_or_none()
    if row is None or row.revoked_at is not None or row.expires_at < datetime.now(UTC):
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user = await db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    row.revoked_at = datetime.now(UTC)
    tokens = await _issue_tokens(db, user)
    # link rotation chain for auditability
    new_row = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(tokens.refresh_token)))).scalar_one_or_none()
    if new_row is not None:
        row.replaced_by_id = new_row.id
    db.add(row)
    await db.commit()
    return tokens


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


def _bearer_user_id_from_header(authorization: str | None) -> uuid.UUID | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(None, 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except Exception:
        return None
    sub = payload.get("sub")
    try:
        return uuid.UUID(str(sub))
    except ValueError:
        return None


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
