import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from solvyn.api.v1 import health, sessions, workflows
from solvyn.config import settings
from solvyn.logging_config import setup_logging
from solvyn.message_bus.redis import message_bus
from solvyn.multi_tenant.auth import RateLimitMiddleware, auth_middleware
from solvyn.multi_tenant.middleware import tenant_middleware
from solvyn.observability.analytics_api import router as observability_router
from solvyn.observability.metrics import MetricsMiddleware

setup_logging(settings.log_level, settings.json_logging)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Startup
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
    app = FastAPI(
        title="Solvyn",
        description="AI Operating Center for Engineering Teams",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.middleware("http")(auth_middleware)
    app.middleware("http")(RateLimitMiddleware())
    app.middleware("http")(tenant_middleware)
    app.middleware("http")(MetricsMiddleware())

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(sessions.router, prefix="/api/v1")
    app.include_router(workflows.router, prefix="/api/v1")
    app.include_router(observability_router, prefix="/api/v1")

    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    return app


app = create_app()
