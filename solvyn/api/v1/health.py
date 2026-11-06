"""Liveness and readiness probes.

These endpoints are intentionally public (no ``X-API-Key`` required) so that
Kubernetes, load balancers, and uptime monitors can reach them without
exposing credentials. They are registered with the ``health`` tag so they
show up under a dedicated group in the generated OpenAPI schema.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy import text as sa_text

from solvyn.db.models import WorkflowWorkerHeartbeat
from solvyn.db.session import async_session_factory
from solvyn.message_bus.redis import message_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])
_WORKER_STALE_AFTER = timedelta(seconds=30)

# Health probes are public, so we do not document the standard 401/403/422
# responses. We do declare 503 because the deep probe may return it.
HEALTH_RESPONSES: dict[int | str, dict[str, Any]] = {
    503: {
        "description": "One or more critical dependencies (database, Redis) are unhealthy.",
        "content": {
            "application/json": {
                "example": {
                    "status": "degraded",
                    "checks": {
                        "database": {"status": "ok"},
                        "redis": {"status": "error", "detail": "Redis connection failed"},
                    },
                }
            }
        },
    },
}


@router.get(
    "/health",
    summary="Liveness probe",
    description=(
        "Lightweight liveness probe suitable for high-frequency polling by "
        "Kubernetes, load balancers, and uptime monitors. Returns ``ok`` as "
        "long as the process is running and the event loop is responsive. "
        "This endpoint does **not** verify downstream dependencies — use "
        "`/health/deep` for readiness."
    ),
    response_description="Service is alive.",
    responses=HEALTH_RESPONSES,
)
async def health_check() -> dict[str, Any]:
    """Return a static OK payload indicating the process is alive."""
    return {"status": "ok", "service": "solvyn"}


@router.get(
    "/health/deep",
    summary="Readiness probe",
    description=(
        "Deep readiness probe that exercises every critical dependency — "
        "PostgreSQL (`SELECT 1`) and Redis (`PING`). Returns ``200`` when all "
        "checks pass and ``503`` otherwise, making it suitable as a Kubernetes "
        "readiness probe that will pull a degraded pod out of the Service's "
        "endpoint set until the underlying issue is resolved."
    ),
    response_description="All dependencies are reachable.",
    responses=HEALTH_RESPONSES,
)
async def deep_health_check() -> JSONResponse:
    """Verify that all critical dependencies are reachable.

    Each dependency is checked independently so a partial outage is reported
    with a clear breakdown. The aggregate response uses HTTP ``503`` whenever
    any check is not in the ``ok`` state, which Kubernetes interprets as
    "not ready" and stops routing traffic to the pod.
    """
    checks: dict[str, dict[str, Any]] = {}

    # Database check
    try:
        async with async_session_factory() as session:
            await session.execute(sa_text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        checks["database"] = {"status": "error", "detail": "Database connection failed"}

    # Redis check
    try:
        if message_bus.redis is not None:
            await message_bus.redis.ping()
            checks["redis"] = {"status": "ok"}
        else:
            checks["redis"] = {"status": "disconnected"}
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        checks["redis"] = {"status": "error", "detail": "Redis connection failed"}

    all_ok = all(c.get("status") == "ok" for c in checks.values())
    status_code = 200 if all_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ok" if all_ok else "degraded", "checks": checks},
    )


@router.get(
    "/health/workers",
    summary="Inspect workflow worker heartbeats",
    description=(
        "Returns persisted workflow scheduler worker liveness. A worker is "
        "healthy when its latest heartbeat is less than 30 seconds old. "
        "This operator signal does not imply that queued work is being recovered."
    ),
)
async def worker_health_check() -> dict[str, Any]:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        rows = (await session.execute(select(WorkflowWorkerHeartbeat).order_by(WorkflowWorkerHeartbeat.last_seen_at.desc()).limit(100))).scalars().all()

    workers = []
    for heartbeat in rows:
        age_seconds = max(0, int((now - heartbeat.last_seen_at).total_seconds()))
        healthy = heartbeat.status == "running" and age_seconds < _WORKER_STALE_AFTER.total_seconds()
        workers.append(
            {
                "worker_name": heartbeat.worker_name,
                "status": heartbeat.status if healthy else ("stale" if heartbeat.status == "running" else heartbeat.status),
                "healthy": healthy,
                "age_seconds": age_seconds,
                "last_seen_at": heartbeat.last_seen_at.isoformat(),
            }
        )
    return {
        "status": "healthy" if any(worker["healthy"] for worker in workers) else "unhealthy",
        "healthy_count": sum(1 for worker in workers if worker["healthy"]),
        "worker_count": len(workers),
        "workers": workers,
    }
