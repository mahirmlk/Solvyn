import logging
from typing import Any

from fastapi import APIRouter
from sqlalchemy import text as sa_text

from solvyn.db.session import async_session_factory
from solvyn.message_bus.redis import message_bus

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health")
async def health_check() -> dict[str, Any]:
    return {"status": "ok", "service": "solvyn"}


@router.get("/health/deep")
async def deep_health_check() -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    # Database check
    try:
        async with async_session_factory() as session:
            await session.execute(sa_text("SELECT 1"))
        checks["database"] = {"status": "ok"}
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        checks["database"] = {"status": "error", "detail": str(e)}

    # Redis check
    try:
        if message_bus.redis:
            await message_bus.redis.ping()  # type: ignore[misc]
            checks["redis"] = {"status": "ok"}
        else:
            checks["redis"] = {"status": "disconnected"}
    except Exception as e:
        logger.error(f"Redis health check failed: {e}")
        checks["redis"] = {"status": "error", "detail": str(e)}

    all_ok = all(c.get("status") == "ok" for c in checks.values())
    return {"status": "ok" if all_ok else "degraded", "checks": checks}
