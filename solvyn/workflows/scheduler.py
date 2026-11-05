"""Poll PostgreSQL for due workflow executions and hand them to leased workers."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import func, select, update

from solvyn.db.models import ExecutionGraph, Session, VerificationRun
from solvyn.db.session import async_session_factory
from solvyn.workflows.executor import workflow_executor

logger = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 5
_BATCH_SIZE = 20
_VERIFICATION_STALE_AFTER = timedelta(minutes=10)


async def run_scheduled_workflows() -> None:
    """Execute due schedules; the executor's conditional lease makes polling multi-worker safe."""
    while True:
        try:
            async with async_session_factory() as db:
                await db.execute(
                    update(VerificationRun)
                    .where(
                        VerificationRun.status == "running",
                        VerificationRun.started_at < func.now() - _VERIFICATION_STALE_AFTER,
                    )
                    .values(
                        status="interrupted",
                        error="Worker stopped before persisting the verification result",
                        completed_at=func.now(),
                    )
                )
                await db.commit()
                due = (
                    await db.execute(
                        select(ExecutionGraph.id, Session.tenant_id)
                        .join(Session, Session.id == ExecutionGraph.session_id)
                        .where(
                            ExecutionGraph.status == "scheduled",
                            ExecutionGraph.scheduled_at <= func.now(),
                        )
                        .order_by(ExecutionGraph.scheduled_at, ExecutionGraph.created_at)
                        .limit(_BATCH_SIZE)
                    )
                ).all()
        except Exception:
            logger.exception("Unable to fetch scheduled workflow executions")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            continue

        for execution_id, tenant_id in due:
            try:
                await workflow_executor.resume(execution_id, str(tenant_id))
            except RuntimeError as exc:
                # Another scheduler claimed it, or the schedule was cancelled.
                logger.info("Scheduled workflow %s was not claimed: %s", execution_id, exc)
            except Exception:
                logger.exception("Scheduled workflow %s failed", execution_id)

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
