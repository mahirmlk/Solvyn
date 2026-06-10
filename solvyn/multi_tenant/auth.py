from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from solvyn.db.models import Tenant, TenantRole
from solvyn.db.session import async_session_factory
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.multi_tenant.rate_limiter import redis_rate_limiter

logger = logging.getLogger(__name__)


PUBLIC_PATHS = ("/api/v1/health", "/metrics", "/docs", "/openapi.json")


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


async def _validate_tenant_api_key(api_key: str) -> Tenant | None:
    """Validate API key against tenant-specific api_key_hash in the database.

    Returns the authenticated Tenant if valid, or None if no matching
    active tenant is found.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    async with async_session_factory() as session:
        result = await session.execute(
            select(Tenant).where(Tenant.is_active == True)  # noqa: E712
        )
        tenants = result.scalars().all()

        for tenant in tenants:
            stored_hash = (tenant.api_key_hash or "").lower()
            if hmac.compare_digest(key_hash, stored_hash):
                return tenant

    return None


# ---------------------------------------------------------------------------
# Role-based access control
# ---------------------------------------------------------------------------

class RequireRole:
    """Dependency class for enforcing role-based access on FastAPI routes.

    Usage in route definitions::

        @app.post("/admin/thing", dependencies=[Depends(RequireRole(TenantRole.ADMIN))])
    """

    def __init__(self, *allowed_roles: str) -> None:
        self.allowed_roles = allowed_roles

    async def __call__(self, request: Request) -> None:
        if tenant_context.tenant_id is None:
            raise HTTPException(status_code=403, detail="No tenant context")

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

    Flow:
    1. Skip authentication for public paths.
    2. Extract the ``X-API-Key`` header.
    3. If ``X-Tenant-Slug`` is present, validate against the tenant's
       ``api_key_hash`` in the database (per-tenant mode).
    4. Otherwise, fall back to shared-secret validation against
       ``settings.api_key_secret`` (backward-compatible mode).
    5. Set tenant context with the authenticated tenant's role (per-tenant)
       or default role (shared-secret mode).
    6. Forward the request.
    """
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/v1/health"):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return JSONResponse(status_code=401, content={"error": "Missing X-API-Key header"})

    tenant_slug = request.headers.get("X-Tenant-Slug")

    # Per-tenant mode: validate against tenant's api_key_hash
    if tenant_slug:
        tenant = await _validate_tenant_api_key(api_key)
        if tenant is None:
            return JSONResponse(status_code=401, content={"error": "Invalid X-API-Key"})
        tenant_context.set(tenant.id, tenant.slug, tenant.role or TenantRole.USER)
    else:
        # Shared-secret mode (backward compatible)
        from solvyn.config import settings

        if not verify_api_key(api_key, settings.api_key_secret):
            return JSONResponse(status_code=401, content={"error": "Invalid X-API-Key"})

    try:
        response = await call_next(request)
        return response
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Request failed", extra={"error": str(e), "path": request.url.path})
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
