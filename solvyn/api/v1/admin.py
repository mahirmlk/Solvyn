"""Tenant administration and API key management endpoints.

All endpoints in this module are guarded by the ``admin`` role via
``RequireRole(TenantRole.ADMIN)``. They are intended for operators who need
to provision tenants, rotate API keys, and inspect cross-tenant usage.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, TypedDict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func as sqlfunc
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import ApiKey, Tenant, TenantRole, TokenUsage
from solvyn.db.models import Session as SessionModel
from solvyn.db.session import get_session
from solvyn.multi_tenant.auth import RequireRole

router = APIRouter(prefix="/admin", tags=["admin"])

# Number of leading characters kept in plaintext for display/identification.
KEY_PREFIX_LENGTH = 12
# Length of the random secret portion of a generated API key (bytes).
KEY_RANDOM_BYTES = 32

# Admin responses keep the admin-only surface well documented.
ADMIN_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid `X-API-Key` header."},
    403: {"description": "Caller does not have the `admin` role."},
}


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class TenantCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    role: str = Field(default=TenantRole.USER)
    is_active: bool = Field(default=True)
    monthly_token_quota: int | None = Field(default=None, ge=0)
    monthly_cost_quota: float | None = Field(default=None, ge=0)


class TenantUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = None
    is_active: bool | None = None
    monthly_token_quota: int | None = Field(default=None, ge=0)
    monthly_cost_quota: float | None = Field(default=None, ge=0)


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    role: str
    is_active: bool
    monthly_token_quota: int | None
    monthly_cost_quota: float | None
    created_at: str
    updated_at: str
    # Aggregate usage stats (joined in from the same query path).
    total_tokens: int
    total_cost: float
    sessions_count: int
    api_keys_count: int


class TenantUsage(BaseModel):
    tenant_id: str
    total_tokens: int
    total_cost: float
    prompt_tokens: int
    completion_tokens: int
    sessions_count: int


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class ApiKeyOut(BaseModel):
    id: str
    tenant_id: str
    name: str
    key_prefix: str
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    is_active: bool


class ApiKeyCreated(ApiKeyOut):
    """Returned exactly once when a key is created — includes the plaintext."""

    plaintext_key: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _generate_api_key() -> tuple[str, str, str]:
    """Return (plaintext_key, key_hash, key_prefix) for a freshly minted key."""
    raw = secrets.token_urlsafe(KEY_RANDOM_BYTES)
    plaintext = f"sk_{raw}"
    key_hash = hashlib.sha256(plaintext.encode()).hexdigest()
    key_prefix = plaintext[:KEY_PREFIX_LENGTH]
    return plaintext, key_hash, key_prefix


def _mask_key(prefix: str) -> str:
    """Return a masked representation of an API key for list views."""
    return f"{prefix}{'*' * 24}"


def _tenant_to_dict(
    tenant: Tenant,
    *,
    total_tokens: int = 0,
    total_cost: float = 0.0,
    sessions_count: int = 0,
    api_keys_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "role": tenant.role or TenantRole.USER,
        "is_active": bool(tenant.is_active),
        "monthly_token_quota": getattr(tenant, "monthly_token_quota", None),
        "monthly_cost_quota": getattr(tenant, "monthly_cost_quota", None),
        "created_at": tenant.created_at.isoformat() if tenant.created_at else "",
        "updated_at": tenant.updated_at.isoformat() if tenant.updated_at else "",
        "total_tokens": int(total_tokens or 0),
        "total_cost": float(total_cost or 0.0),
        "sessions_count": int(sessions_count or 0),
        "api_keys_count": int(api_keys_count or 0),
    }


def _apikey_to_dict(key: ApiKey) -> dict[str, Any]:
    revoked = key.revoked_at is not None
    return {
        "id": str(key.id),
        "tenant_id": str(key.tenant_id),
        "name": key.name,
        "key_prefix": key.key_prefix,
        "created_at": key.created_at.isoformat() if key.created_at else "",
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "is_active": not revoked,
    }


async def _load_tenant_or_404(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail=f"Tenant {tenant_id} not found")
    return tenant


async def _tenant_aggregates(db: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int | float]:
    """Return aggregate token/session/api-key counts for a tenant in one round trip."""
    token_row = (
        await db.execute(
            select(
                sqlfunc.coalesce(sqlfunc.sum(TokenUsage.total_tokens), 0).label("total_tokens"),
                sqlfunc.coalesce(sqlfunc.sum(TokenUsage.cost), 0.0).label("total_cost"),
                sqlfunc.coalesce(sqlfunc.sum(TokenUsage.prompt_tokens), 0).label("prompt_tokens"),
                sqlfunc.coalesce(sqlfunc.sum(TokenUsage.completion_tokens), 0).label("completion_tokens"),
            ).where(TokenUsage.tenant_id == tenant_id)
        )
    ).one()

    sessions_count = (
        await db.execute(select(sqlfunc.count(SessionModel.id)).where(SessionModel.tenant_id == tenant_id))
    ).scalar_one()

    api_keys_count = (
        await db.execute(
            select(sqlfunc.count(ApiKey.id)).where(ApiKey.tenant_id == tenant_id, ApiKey.revoked_at.is_(None))
        )
    ).scalar_one()

    return {
        "total_tokens": int(token_row.total_tokens or 0),
        "total_cost": float(token_row.total_cost or 0.0),
        "prompt_tokens": int(token_row.prompt_tokens or 0),
        "completion_tokens": int(token_row.completion_tokens or 0),
        "sessions_count": int(sessions_count or 0),
        "api_keys_count": int(api_keys_count or 0),
    }


class _TenantAggregates(TypedDict, total=False):
    total_tokens: int
    total_cost: float
    prompt_tokens: int
    completion_tokens: int
    sessions_count: int
    api_keys_count: int


async def _unique_slug_exists(db: AsyncSession, slug: str) -> bool:
    result = await db.execute(select(Tenant.id).where(Tenant.slug == slug))
    return result.scalar_one_or_none() is not None


def _validate_role(role: str) -> str:
    allowed = {TenantRole.ADMIN, TenantRole.USER, TenantRole.VIEWER}
    if role not in allowed:
        raise HTTPException(status_code=400, detail=f"role must be one of: {', '.join(sorted(allowed))}")
    return role


# ---------------------------------------------------------------------------
# Tenant endpoints
# ---------------------------------------------------------------------------

@router.get(
    "/tenants",
    summary="List all tenants (admin)",
    description="Return every tenant in the system along with aggregate usage "
    "statistics. Restricted to operators with the `admin` role.",
    response_description="List of tenants with usage stats.",
    responses=ADMIN_RESPONSES,
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def list_tenants(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """List every tenant with aggregate usage statistics."""
    tenants_result = await db.execute(select(Tenant).order_by(Tenant.created_at.desc()))
    tenants = tenants_result.scalars().all()

    if not tenants:
        return {"tenants": [], "count": 0}

    tenant_ids = [t.id for t in tenants]

    # Aggregate token usage per tenant in a single query.
    tokens_rows = (
        await db.execute(
            select(
                TokenUsage.tenant_id,
                sqlfunc.coalesce(sqlfunc.sum(TokenUsage.total_tokens), 0).label("total_tokens"),
                sqlfunc.coalesce(sqlfunc.sum(TokenUsage.cost), 0.0).label("total_cost"),
            )
            .where(TokenUsage.tenant_id.in_(tenant_ids))
            .group_by(TokenUsage.tenant_id)
        )
    ).all()
    tokens_by_tenant: dict[uuid.UUID, tuple[int, float]] = {
        row.tenant_id: (int(row.total_tokens or 0), float(row.total_cost or 0.0)) for row in tokens_rows
    }

    sessions_rows = (
        await db.execute(
            select(SessionModel.tenant_id, sqlfunc.count(SessionModel.id))
            .where(SessionModel.tenant_id.in_(tenant_ids))
            .group_by(SessionModel.tenant_id)
        )
    ).all()
    sessions_by_tenant = {row[0]: int(row[1] or 0) for row in sessions_rows}

    api_keys_rows = (
        await db.execute(
            select(ApiKey.tenant_id, sqlfunc.count(ApiKey.id))
            .where(ApiKey.tenant_id.in_(tenant_ids), ApiKey.revoked_at.is_(None))
            .group_by(ApiKey.tenant_id)
        )
    ).all()
    api_keys_by_tenant = {row[0]: int(row[1] or 0) for row in api_keys_rows}

    items = [
        _tenant_to_dict(
            t,
            total_tokens=tokens_by_tenant.get(t.id, (0, 0.0))[0],
            total_cost=tokens_by_tenant.get(t.id, (0, 0.0))[1],
            sessions_count=sessions_by_tenant.get(t.id, 0),
            api_keys_count=api_keys_by_tenant.get(t.id, 0),
        )
        for t in tenants
    ]
    return {"tenants": items, "count": len(items)}


@router.post(
    "/tenants",
    summary="Create a new tenant (admin)",
    description="Provision a new tenant. A fresh API key is automatically generated and "
    "its plaintext is returned exactly once — store it securely.",
    response_description="The created tenant plus a freshly generated API key.",
    status_code=201,
    responses={**ADMIN_RESPONSES, 400: {"description": "Invalid slug or role."}, 409: {"description": "Slug already exists."}},
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def create_tenant(payload: TenantCreate, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Create a new tenant and mint its first API key."""
    if await _unique_slug_exists(db, payload.slug):
        raise HTTPException(status_code=409, detail=f"Tenant with slug '{payload.slug}' already exists")

    role = _validate_role(payload.role)

    plaintext, key_hash, key_prefix = _generate_api_key()

    tenant = Tenant(
        name=payload.name,
        slug=payload.slug,
        api_key_hash=key_hash,
        role=role,
        is_active=payload.is_active,
    )
    # Optional quota fields. These are guarded with ``setattr`` so the model
    # remains usable even if a deployment skipped the schema migration.
    if payload.monthly_token_quota is not None:
        setattr(tenant, "monthly_token_quota", payload.monthly_token_quota)
    if payload.monthly_cost_quota is not None:
        setattr(tenant, "monthly_cost_quota", payload.monthly_cost_quota)

    db.add(tenant)
    await db.flush()  # populate tenant.id before creating the ApiKey row

    api_key = ApiKey(
        tenant_id=tenant.id,
        name="default",
        key_hash=key_hash,
        key_prefix=key_prefix,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(tenant)

    out = _tenant_to_dict(tenant)
    out["initial_api_key"] = {
        **ApiKeyCreated(
            id=str(api_key.id),
            tenant_id=str(api_key.tenant_id),
            name=api_key.name,
            key_prefix=api_key.key_prefix,
            created_at=api_key.created_at.isoformat() if api_key.created_at else "",
            last_used_at=None,
            revoked_at=None,
            is_active=True,
            plaintext_key=plaintext,
        ).model_dump(),
        "masked": _mask_key(api_key.key_prefix),
    }
    return out


@router.patch(
    "/tenants/{tenant_id}",
    summary="Update a tenant (admin)",
    description="Partially update a tenant. All fields are optional; only those present in "
    "the request body are modified. The API key is never returned and cannot be "
    "rotated through this endpoint — use the dedicated `/api-keys` endpoints.",
    response_description="The updated tenant record.",
    responses={**ADMIN_RESPONSES, 404: {"description": "Tenant not found."}},
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def update_tenant(tenant_id: uuid.UUID, payload: TenantUpdate, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Update a tenant's mutable fields."""
    tenant = await _load_tenant_or_404(db, tenant_id)

    update_data = payload.model_dump(exclude_unset=True)
    if "role" in update_data and update_data["role"] is not None:
        update_data["role"] = _validate_role(update_data["role"])

    for field, value in update_data.items():
        if value is None:
            continue
        setattr(tenant, field, value)

    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)

    aggregates = await _tenant_aggregates(db, tenant.id)
    return _tenant_to_dict(
        tenant,
        total_tokens=int(aggregates["total_tokens"]),
        total_cost=float(aggregates["total_cost"]),
        sessions_count=int(aggregates["sessions_count"]),
        api_keys_count=int(aggregates["api_keys_count"]),
    )


@router.delete(
    "/tenants/{tenant_id}",
    summary="Soft-delete a tenant (admin)",
    description="Mark a tenant as inactive. The row is preserved so historical sessions and "
    "audit entries remain referentially intact. Use this as a reversible off-boarding.",
    status_code=200,
    responses={**ADMIN_RESPONSES, 404: {"description": "Tenant not found."}},
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def delete_tenant(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Soft-delete (deactivate) a tenant."""
    tenant = await _load_tenant_or_404(db, tenant_id)
    tenant.is_active = False
    db.add(tenant)
    await db.commit()
    return {"deleted": True, "tenant_id": str(tenant_id), "is_active": False}


@router.get(
    "/tenants/{tenant_id}/usage",
    summary="Tenant usage details (admin)",
    description="Return the per-tenant token usage, cost, prompt/completion breakdown and "
    "session count.",
    response_description="Aggregated usage numbers for the tenant.",
    responses={**ADMIN_RESPONSES, 404: {"description": "Tenant not found."}},
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def get_tenant_usage(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Return aggregate token usage, cost and session count for a tenant."""
    tenant = await _load_tenant_or_404(db, tenant_id)
    aggregates = await _tenant_aggregates(db, tenant.id)
    return {
        "tenant_id": str(tenant.id),
        "total_tokens": aggregates["total_tokens"],
        "total_cost": aggregates["total_cost"],
        "prompt_tokens": aggregates["prompt_tokens"],
        "completion_tokens": aggregates["completion_tokens"],
        "sessions_count": aggregates["sessions_count"],
    }


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/tenants/{tenant_id}/api-keys",
    summary="Create an API key (admin)",
    description="Mint a new API key for the given tenant. The plaintext key is returned in "
    "the response — it is the only time the server ever holds the plaintext, so the "
    "operator must store it immediately.",
    status_code=201,
    responses={
        **ADMIN_RESPONSES,
        404: {"description": "Tenant not found."},
    },
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def create_api_key(tenant_id: uuid.UUID, payload: ApiKeyCreate, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Create a new API key for a tenant and return its plaintext once."""
    tenant = await _load_tenant_or_404(db, tenant_id)
    plaintext, key_hash, key_prefix = _generate_api_key()

    api_key = ApiKey(
        tenant_id=tenant.id,
        name=payload.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return {
        **_apikey_to_dict(api_key),
        "plaintext_key": plaintext,
        "masked": _mask_key(api_key.key_prefix),
    }


@router.get(
    "/tenants/{tenant_id}/api-keys",
    summary="List API keys for a tenant (admin)",
    description="Return every API key issued for the tenant. Only the key prefix and "
    "metadata are returned — the plaintext is never available after creation.",
    response_description="List of masked API keys.",
    responses={**ADMIN_RESPONSES, 404: {"description": "Tenant not found."}},
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def list_api_keys(tenant_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """List (masked) API keys for a tenant."""
    tenant = await _load_tenant_or_404(db, tenant_id)
    result = await db.execute(
        select(ApiKey).where(ApiKey.tenant_id == tenant.id).order_by(ApiKey.created_at.desc())
    )
    keys = result.scalars().all()
    items = [{**_apikey_to_dict(k), "masked": _mask_key(k.key_prefix)} for k in keys]
    return {"api_keys": items, "count": len(items)}


@router.delete(
    "/api-keys/{key_id}",
    summary="Revoke an API key (admin)",
    description="Soft-revoke an API key. The key remains in the database for audit purposes "
    "but will no longer authenticate requests (matching logic should treat "
    "`revoked_at != None` as invalid).",
    status_code=200,
    responses={**ADMIN_RESPONSES, 404: {"description": "API key not found."}},
    dependencies=[Depends(RequireRole(TenantRole.ADMIN))],
)
async def revoke_api_key(key_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Revoke an API key by setting its `revoked_at` timestamp."""
    api_key = await db.get(ApiKey, key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail=f"API key {key_id} not found")

    if api_key.revoked_at is None:
        api_key.revoked_at = datetime.now(UTC)
        db.add(api_key)
        await db.commit()
        await db.refresh(api_key)

    return {
        "revoked": True,
        "key_id": str(api_key.id),
        "tenant_id": str(api_key.tenant_id),
        "revoked_at": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
    }


# ---------------------------------------------------------------------------
# Sanity check helper (used by tests)
# ---------------------------------------------------------------------------

async def authenticate_admin_request(api_key: str) -> bool:
    """Verify the supplied API key belongs to an active admin tenant.

    This is a stand-alone helper that does not depend on request state, useful
    for callers that need to validate an API key out-of-band (e.g. CLI tools).
    """
    if not api_key:
        return False
    from solvyn.multi_tenant.auth import _validate_tenant_api_key

    tenant = await _validate_tenant_api_key(api_key)
    return tenant is not None and str(tenant.role) == TenantRole.ADMIN


# Re-export status for the convenience of downstream tests.
__all__ = [
    "router",
    "authenticate_admin_request",
    "TenantCreate",
    "TenantUpdate",
    "TenantOut",
    "TenantUsage",
    "ApiKeyCreate",
    "ApiKeyOut",
    "ApiKeyCreated",
]
