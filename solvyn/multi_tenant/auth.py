from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from solvyn.db.models import ApiKey, Organization, OrganizationMembership, Tenant, TenantRole
from solvyn.db.session import async_session_factory
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.multi_tenant.rate_limiter import redis_rate_limiter

logger = logging.getLogger(__name__)


PUBLIC_PATHS = (
    "/api/v1/health",
    "/metrics",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
)


# ---------------------------------------------------------------------------
# API key verification helpers
# ---------------------------------------------------------------------------

def verify_api_key(api_key: str, expected_hash_or_key: str) -> bool:
    """Verify API key using constant-time hash comparison.

    ``expected_hash_or_key`` should be a SHA256 hex digest (64-char) or a
    plaintext value which will be hashed before comparison.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    expected = expected_hash_or_key.lower()
    if len(expected) == 64 and all(c in "0123456789abcdef" for c in expected):
        return hmac.compare_digest(key_hash, expected)
    expected_hash = hashlib.sha256(expected_hash_or_key.encode()).hexdigest()
    return hmac.compare_digest(key_hash, expected_hash)


async def _validate_tenant_api_key(api_key: str, expected_tenant_id: uuid.UUID | None = None) -> Tenant | None:
    """Validate an API key with an indexed lookup (never a full-table scan).

    P0-003 fix: when ``expected_tenant_id`` is supplied (resolved from the
    X-Tenant-Slug header), the key must belong to *that* tenant. A valid key
    from a different tenant is rejected so one tenant cannot impersonate
    another by sending a foreign slug.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    if expected_tenant_id is not None:
        async with async_session_factory() as session:
            tenant_result = await session.execute(select(Tenant).where(Tenant.id == expected_tenant_id, Tenant.is_active == True))  # noqa: E712
            tenant = tenant_result.scalar_one_or_none()
            if tenant is None:
                return None
            stored_hash = (tenant.api_key_hash or "").lower()
            if stored_hash and hmac.compare_digest(key_hash, stored_hash):
                return tenant
            # Fall through to per-key table (tenant may have rotated keys)
            api_key_result = await session.execute(
                select(ApiKey)
                .where(ApiKey.tenant_id == expected_tenant_id)
                .where(ApiKey.key_hash == key_hash)
                .where(ApiKey.revoked_at.is_(None))
            )
            return tenant if api_key_result.scalar_one_or_none() is not None else None

    # No slug supplied: resolve the tenant by key hash using the unique index.
    async with async_session_factory() as session:
        tenant_result = await session.execute(
            select(Tenant).where(Tenant.api_key_hash == key_hash, Tenant.is_active == True)  # noqa: E712
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant is not None:
            return tenant
        api_key_result = await session.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked_at.is_(None))
        )
        api_key_row = api_key_result.scalar_one_or_none()
        if api_key_row is None:
            return None
        bound_result = await session.execute(
            select(Tenant).where(Tenant.id == api_key_row.tenant_id, Tenant.is_active == True)  # noqa: E712
        )
        return bound_result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# JWT ↔ tenant binding
# ---------------------------------------------------------------------------

_ORG_ROLE_TO_TENANT_ROLE = {
    # Organization ownership is scoped to that organization. Global admin
    # endpoints use TenantRole.ADMIN and must only be reachable via an
    # explicitly provisioned tenant API key, never an ordinary user JWT.
    "owner": TenantRole.USER,
    "admin": TenantRole.USER,
    "member": TenantRole.USER,
    "viewer": TenantRole.VIEWER,
}

# Sentinel for backward-compatible single-tenant shared-secret deployments:
# requests authenticated with the shared secret are granted admin scope on
# never-allocated nil UUID so tenant-scoped queries stay empty and safe.
SHARED_SECRET_TENANT_ID = uuid.UUID(int=0)


async def _membership_for_tenant(user_id: uuid.UUID, tenant_id: uuid.UUID) -> tuple[str, str] | None:
    """Return (organization slug, role) when the user is a member of the tenant."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Organization.slug, OrganizationMembership.role)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(OrganizationMembership.user_id == user_id, Organization.tenant_id == tenant_id, Organization.is_active == True)  # noqa: E712
            .order_by(OrganizationMembership.created_at)
            .limit(1)
        )
        row = result.first()
        return (row[0], row[1]) if row is not None else None


async def _first_membership(user_id: uuid.UUID) -> tuple[uuid.UUID, str, str] | None:
    """Return (tenant_id, organization slug, role) for the user's first org (deterministic)."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(Organization.tenant_id, Organization.slug, OrganizationMembership.role)
            .join(OrganizationMembership, OrganizationMembership.organization_id == Organization.id)
            .where(OrganizationMembership.user_id == user_id, Organization.is_active == True)  # noqa: E712
            .order_by(OrganizationMembership.created_at, Organization.created_at)
            .limit(1)
        )
        row = result.first()
        return (row[0], row[1], row[2]) if row else None


# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------

class RequireRole:
    """Dependency class for enforcing role-based access on FastAPI routes.

    Usage in route definitions::

        @app.post("/admin/thing", dependencies=[Depends(RequireRole(TenantRole.ADMIN))])

    The check requires an *authenticated* tenant (proved by credential), not
    merely a tenant slug named in a header (P0-003).
    """

    def __init__(self, *allowed_roles: str) -> None:
        self.allowed_roles = allowed_roles

    async def __call__(self, request: Request) -> None:
        if not tenant_context.is_authenticated:
            raise HTTPException(status_code=401, detail="Authentication required")
        if tenant_context.tenant_id is None:
            raise HTTPException(status_code=401, detail="No tenant context")
        if tenant_context.authenticated_tenant_id != tenant_context.tenant_id:
            raise HTTPException(status_code=401, detail="Tenant context does not match the credential")
        if tenant_context.tenant_role not in self.allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role: {', '.join(self.allowed_roles)}",
            )


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

async def auth_middleware(request: Request, call_next: Any) -> Any:
    """Authenticate requests using per-tenant or shared-secret API key validation.

    Tenant context is already resolved by ``tenant_middleware`` which runs
    before this middleware. This middleware only validates the API key.

    Flow:
    1. Skip authentication for public paths.
    2. Extract the ``X-API-Key`` header.
    3. Validate the key (per-tenant or shared-secret mode).
    4. Forward the request.
    """
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/v1/health"):
        return await call_next(request)

    # JWT bearer (user identity) takes precedence when present.
    authorization = request.headers.get("Authorization")
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(None, 1)[1].strip()
        from solvyn.identity.security import decode_access_token

        try:
            payload = decode_access_token(token)
            subject = payload.get("sub")
            user_uuid = uuid.UUID(str(subject))
            from solvyn.db.models import User

            async with async_session_factory() as db_session:
                user = await db_session.get(User, user_uuid)
            if user is None or not user.is_active:
                return JSONResponse(status_code=401, content={"error": "Invalid or expired token"})

            header_tenant_id = tenant_context.tenant_id
            header_slug = tenant_context.tenant_slug
            if header_tenant_id is not None:
                membership = await _membership_for_tenant(user_uuid, header_tenant_id)
                if membership is None:
                    tenant_context.set_authenticated(None, user_id=user_uuid)
                    return JSONResponse(status_code=403, content={"error": "Not a member of the requested tenant"})
                role = _ORG_ROLE_TO_TENANT_ROLE.get(membership[1], TenantRole.VIEWER)
                tenant_context.authenticate(header_tenant_id, header_slug or "", role, user_id=user_uuid)
            else:
                first = await _first_membership(user_uuid)
                if first is not None:
                    tenant_id, slug, org_role = first
                    role = _ORG_ROLE_TO_TENANT_ROLE.get(org_role, TenantRole.VIEWER)
                    tenant_context.authenticate(tenant_id, slug, role, user_id=user_uuid)
                else:
                    # authenticated user with no tenant scope: allows /auth/* only.
                    tenant_context.set_authenticated(None, user_id=user_uuid)
        except Exception as exc:
            logger.exception("JWT authorization failed: %s", exc)
            return JSONResponse(status_code=401, content={"error": "Invalid or expired token"})
        try:
            return await call_next(request)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Request failed: %s", e)
            return JSONResponse(status_code=500, content={"error": "Internal server error"})

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return JSONResponse(status_code=401, content={"error": "Missing X-API-Key header"})

    # Per-tenant mode: the key must belong to the tenant named in X-Tenant-Slug.
    # The slug is an untrusted hint; the credential is the source of truth.
    if tenant_context.tenant_id is not None:
        tenant = await _validate_tenant_api_key(api_key, expected_tenant_id=tenant_context.tenant_id)
        if tenant is None:
            return JSONResponse(status_code=401, content={"error": "Invalid X-API-Key for the requested tenant"})
        tenant_context.authenticate(tenant.id, tenant.slug, tenant.role or TenantRole.USER, user_id=None)
    else:
        # Shared-secret mode (backward compatible single-tenant deployments).
        from solvyn.config import settings

        if not settings.api_key_secret:
            return JSONResponse(status_code=500, content={"error": "Server not configured: API_KEY_SECRET is empty"})
        if not verify_api_key(api_key, settings.api_key_secret):
            return JSONResponse(status_code=401, content={"error": "Invalid X-API-Key"})
        # Explicit, fail-closed tenant scope for deployments without per-tenant contexts.
        tenant_context.authenticate(SHARED_SECRET_TENANT_ID, "shared", TenantRole.ADMIN, user_id=None)

    try:
        response = await call_next(request)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Request failed: %s", e)
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


class RateLimitMiddleware:
    """Redis-backed rate limiter with in-memory fallback for multi-worker deployments."""

    def __init__(self) -> None:
        from solvyn.config import settings

        redis_rate_limiter.configure(
            ip_limit=settings.rate_limit_per_ip,
            tenant_limit=settings.rate_limit_per_tenant,
            window_seconds=settings.rate_limit_window,
        )

    async def __call__(self, request: Request, call_next: Any) -> Any:
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/v1/health"):
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        tenant_id = str(tenant_context.tenant_id) if tenant_context.tenant_id else "unknown"

        ip_allowed, ip_retry = await redis_rate_limiter.check_ip(client_ip)
        if not ip_allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded", "retry_after": ip_retry, "scope": "ip"},
            )

        tenant_allowed, tenant_retry = await redis_rate_limiter.check_tenant(tenant_id)
        if not tenant_allowed:
            return JSONResponse(
                status_code=429,
                content={"error": "Tenant rate limit exceeded", "retry_after": tenant_retry, "scope": "tenant"},
            )

        return await call_next(request)
