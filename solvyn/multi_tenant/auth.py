import hashlib
import hmac
import logging
import time
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from solvyn.multi_tenant.middleware import tenant_context

logger = logging.getLogger(__name__)


PUBLIC_PATHS = ("/api/v1/health", "/metrics", "/docs", "/openapi.json")


def verify_api_key(api_key: str, expected_hash_or_key: str) -> bool:
    """Verify API key using constant-time hash comparison.

    ``api_key_secret`` should be set to a SHA256 hash (64-char hex)
    or a plaintext value which will be hashed before comparison.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    expected = expected_hash_or_key.lower()
    # If expected is already a SHA256 hex digest (64 chars), compare directly
    if len(expected) == 64 and all(c in "0123456789abcdef" for c in expected):
        return hmac.compare_digest(key_hash, expected)
    # Otherwise hash the stored secret and compare
    expected_hash = hashlib.sha256(expected_hash_or_key.encode()).hexdigest()
    return hmac.compare_digest(key_hash, expected_hash)


async def auth_middleware(request: Request, call_next: Any) -> Any:
    if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/v1/health"):
        return await call_next(request)

    from solvyn.config import settings
    if settings.api_key_secret == "change-me-to-a-random-secret":
        logger.warning("INSECURE: api_key_secret is using the default value. Set a custom secret before production deployment.")

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return JSONResponse(status_code=401, content={"error": "Missing X-API-Key header"})

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
    def __init__(self) -> None:
        self._ip_requests: dict[str, list[float]] = {}
        self._tenant_requests: dict[str, list[float]] = {}
        self.max_per_ip = 100
        self.max_per_tenant = 500
        self.window_seconds = 60

    async def __call__(self, request: Request, call_next: Any) -> Any:
        if request.url.path in PUBLIC_PATHS or request.url.path.startswith("/api/v1/health"):
            return await call_next(request)

        now = time.time()
        client_ip = request.client.host if request.client else "unknown"
        tenant_id = str(tenant_context.tenant_id) if tenant_context.tenant_id else "unknown"

        # Periodic cleanup of stale entries
        for key in list(self._ip_requests.keys()):
            self._ip_requests[key] = [t for t in self._ip_requests[key] if now - t < self.window_seconds]
            if not self._ip_requests[key]:
                del self._ip_requests[key]
        for key in list(self._tenant_requests.keys()):
            self._tenant_requests[key] = [t for t in self._tenant_requests[key] if now - t < self.window_seconds]
            if not self._tenant_requests[key]:
                del self._tenant_requests[key]

        # Per-IP throttle
        ip_window = self._ip_requests.get(client_ip, [])
        ip_window = [t for t in ip_window if now - t < self.window_seconds]
        if len(ip_window) >= self.max_per_ip:
            return JSONResponse(status_code=429, content={"error": "Rate limit exceeded", "retry_after": self.window_seconds, "scope": "ip"})
        ip_window.append(now)
        self._ip_requests[client_ip] = ip_window

        # Per-tenant throttle
        tenant_window = self._tenant_requests.get(tenant_id, [])
        tenant_window = [t for t in tenant_window if now - t < self.window_seconds]
        if len(tenant_window) >= self.max_per_tenant:
            return JSONResponse(status_code=429, content={"error": "Tenant rate limit exceeded", "retry_after": self.window_seconds, "scope": "tenant"})
        tenant_window.append(now)
        self._tenant_requests[tenant_id] = tenant_window

        return await call_next(request)
