"""Solvyn — your self-hosted AI engineering team.

The AI Operating Center is the internal ops/monitoring layer; the primary
product experience is an engineering-teammates workspace.

This module wires the FastAPI application together: it registers routers,
configures middleware (CORS, auth, tenant resolution, rate limiting, metrics,
security headers), initialises OpenTelemetry, and exposes the rich OpenAPI
schema used by ``/docs`` and ``/redoc``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from prometheus_client import make_asgi_app

from solvyn.api.v1 import health, sessions, workflows
from solvyn.api.v1.admin import router as admin_router
from solvyn.api.v1.approvals import router as approvals_router
from solvyn.api.v1.auth import router as auth_router
from solvyn.api.v1.organizations import router as organizations_router
from solvyn.api.v1.repositories import router as repositories_router
from solvyn.api.v1.teammates import router as teammates_router
from solvyn.api_metadata import API_DESCRIPTION, TAGS_METADATA
from solvyn.config import enforce_prod_secrets, settings
from solvyn.logging_config import setup_logging
from solvyn.message_bus.redis import message_bus
from solvyn.multi_tenant.auth import RateLimitMiddleware, auth_middleware
from solvyn.multi_tenant.middleware import tenant_middleware
from solvyn.observability.analytics_api import router as observability_router
from solvyn.observability.metrics import MetricsMiddleware
from solvyn.observability.opentelemetry import init_opentelemetry

setup_logging(settings.log_level, settings.json_logging)
logger = logging.getLogger(__name__)

init_opentelemetry(
    service_name="solvyn",
    otlp_endpoint=settings.otlp_endpoint,
    enabled=settings.otel_enabled,
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
    # P0-002: fail fast when auth secrets are absent (production).
    try:
        enforce_prod_secrets()
    except RuntimeError as exc:
        logger.critical("SECURITY: %s", exc)
        raise
    await message_bus.connect()
    logger.info("Solvyn started")
    yield
    # Shutdown
    logger.info("Shutting down — draining connections...")
    await message_bus.disconnect()
    from solvyn.llm.router import llm_router

    await llm_router.close()
    from solvyn.db.session import engine

    await engine.dispose()
    logger.info("Connections drained. Solvyn stopped.")


def create_app() -> FastAPI:
    solvyn_app = FastAPI(
        title="Solvyn API",
        description=API_DESCRIPTION,
        version="0.1.0",
        contact={
            "name": "Solvyn Engineering",
            "url": "https://solvyn.example.com",
            "email": "eng@solvyn.example.com",
        },
        license_info={
            "name": "Apache-2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0.html",
        },
        terms_of_service="https://solvyn.example.com/terms",
        openapi_tags=TAGS_METADATA,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    def _custom_openapi() -> dict[str, Any]:
        """Generate a custom OpenAPI schema with security schemes pre-wired.

        FastAPI's auto-generated schema is fine for most projects, but Solvyn
        exposes a tenant-aware security model that benefits from a richer
        description. The result is cached on the app instance to avoid
        regenerating it on every request to ``/openapi.json``.
        """
        if solvyn_app.openapi_schema:
            return solvyn_app.openapi_schema

        openapi_schema = get_openapi(
            title=solvyn_app.title,
            version=solvyn_app.version,
            description=solvyn_app.description,
            routes=solvyn_app.routes,
            tags=solvyn_app.openapi_tags,
            contact=solvyn_app.contact,
            license_info=solvyn_app.license_info,
            openapi_version=solvyn_app.openapi_version,
            terms_of_service=solvyn_app.terms_of_service,
        )

        openapi_schema.setdefault("components", {}).setdefault("securitySchemes", {})
        openapi_schema["components"]["securitySchemes"] = {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
                "description": (
                    "Per-tenant API key. Generate one in the admin console or via "
                    "`POST /admin/tenants/{slug}/api-key`. For single-tenant "
                    "deployments, the shared `API_KEY_SECRET` is accepted instead."
                ),
            },
            "TenantSlug": {
                "type": "apiKey",
                "in": "header",
                "name": "X-Tenant-Slug",
                "description": (
                    "Tenant slug. Required when running in per-tenant mode to "
                    "disambiguate the authenticated principal."
                ),
            },
        }
        openapi_schema["security"] = [{"ApiKeyAuth": []}, {"TenantSlug": []}]

        solvyn_app.openapi_schema = openapi_schema
        return solvyn_app.openapi_schema

    # Bind the closure so it captures this concrete app instance.
    solvyn_app.openapi = _custom_openapi  # type: ignore[method-assign]

    solvyn_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
        allow_headers=["Content-Type", "X-API-Key", "X-Tenant-Slug", "Authorization"],
    )

    # Middleware executes in reverse registration order for requests:
    # tenant_middleware (first on request) → auth_middleware → RateLimitMiddleware → MetricsMiddleware
    solvyn_app.middleware("http")(MetricsMiddleware())
    solvyn_app.middleware("http")(RateLimitMiddleware())
    solvyn_app.middleware("http")(auth_middleware)
    solvyn_app.middleware("http")(tenant_middleware)

    solvyn_app.include_router(health.router, prefix="/api/v1")
    solvyn_app.include_router(sessions.router, prefix="/api/v1")
    solvyn_app.include_router(workflows.router, prefix="/api/v1")
    solvyn_app.include_router(observability_router, prefix="/api/v1")
    solvyn_app.include_router(admin_router, prefix="/api/v1")
    solvyn_app.include_router(auth_router, prefix="/api/v1")
    solvyn_app.include_router(organizations_router, prefix="/api/v1")
    solvyn_app.include_router(repositories_router, prefix="/api/v1")
    solvyn_app.include_router(teammates_router, prefix="/api/v1")
    solvyn_app.include_router(approvals_router, prefix="/api/v1")

    metrics_app = make_asgi_app()
    solvyn_app.mount("/metrics", metrics_app)

    @solvyn_app.middleware("http")
    async def add_security_headers(request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # P0-005: removed `unsafe-eval` (XSS amplifier). `unsafe-inline` kept
        # only for style-src because the Next.js frontend injects inline
        # styles; script-src is locked to self.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'"
        )
        if request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    return solvyn_app


app = create_app()
