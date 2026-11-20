"""Standalone durable workflow scheduler process."""

from __future__ import annotations

import asyncio
import logging

from solvyn.config import enforce_prod_secrets, settings
from solvyn.logging_config import setup_logging
from solvyn.message_bus.redis import message_bus
from solvyn.observability.opentelemetry import init_opentelemetry, shutdown_opentelemetry
from solvyn.workflows.scheduler import run_scheduled_workflows


async def run_worker() -> None:
    enforce_prod_secrets()
    await message_bus.connect()
    logger = logging.getLogger(__name__)
    logger.info("Solvyn workflow worker started")
    try:
        await run_scheduled_workflows()
    finally:
        await message_bus.disconnect()
        from solvyn.llm.router import llm_router

        await llm_router.close()
        from solvyn.db.session import engine

        await engine.dispose()
        logger.info("Solvyn workflow worker stopped")


def main() -> None:
    setup_logging(settings.log_level, settings.json_logging)
    init_opentelemetry(
        service_name="solvyn-worker",
        otlp_endpoint=settings.otlp_endpoint,
        enabled=settings.otel_enabled,
    )
    try:
        asyncio.run(run_worker())
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_opentelemetry()


if __name__ == "__main__":
    main()
