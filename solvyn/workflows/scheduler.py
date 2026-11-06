"""Poll PostgreSQL for due workflow executions and hand them to leased workers."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import (
    ExecutionGraph,
    MaintenanceSchedule,
    Organization,
    ProactiveEvent,
    Project,
    Session,
    VerificationRun,
    WorkflowWorkerHeartbeat,
)
from solvyn.db.session import async_session_factory
from solvyn.workflows.executor import workflow_executor

logger = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 5
_BATCH_SIZE = 20
_VERIFICATION_STALE_AFTER = timedelta(minutes=10)
_WORKER_ID = uuid.uuid4()
_WORKER_STARTED_AT = datetime.now(UTC)
_WORKER_NAME = "workflow-scheduler"
_WORKER_HEARTBEAT_INTERVAL_SECONDS = 5
_WORKER_HEARTBEAT_RETENTION = timedelta(days=30)


async def _write_worker_heartbeat(status: str) -> None:
    async with async_session_factory() as db:
        heartbeat = await db.get(WorkflowWorkerHeartbeat, _WORKER_ID)
        now = datetime.now(UTC)
        if heartbeat is None:
            heartbeat = WorkflowWorkerHeartbeat(
                worker_id=_WORKER_ID,
                worker_name=_WORKER_NAME,
                host_name=socket.gethostname()[:255],
                status=status,
                started_at=_WORKER_STARTED_AT,
                last_seen_at=now,
            )
            db.add(heartbeat)
        else:
            heartbeat.status = status
            heartbeat.last_seen_at = now
        await db.execute(delete(WorkflowWorkerHeartbeat).where(WorkflowWorkerHeartbeat.last_seen_at < now - _WORKER_HEARTBEAT_RETENTION))
        await db.commit()


async def _worker_heartbeat_loop() -> None:
    while True:
        try:
            await _write_worker_heartbeat("running")
        except Exception:
            logger.exception("Unable to persist workflow worker heartbeat")
        await asyncio.sleep(_WORKER_HEARTBEAT_INTERVAL_SECONDS)


async def _queue_due_maintenance_events(db: AsyncSession) -> int:
    """Atomically queue a bounded batch of due maintenance reminders."""
    due = (
        await db.execute(
            select(MaintenanceSchedule)
            .join(Project, Project.id == MaintenanceSchedule.project_id)
            .join(Organization, Organization.id == MaintenanceSchedule.organization_id)
            .where(
                MaintenanceSchedule.enabled == True,  # noqa: E712
                MaintenanceSchedule.next_run_at <= func.now(),
                Project.organization_id == MaintenanceSchedule.organization_id,
                Project.is_active == True,  # noqa: E712
                Organization.is_active == True,  # noqa: E712
            )
            .order_by(MaintenanceSchedule.next_run_at, MaintenanceSchedule.created_at)
            .with_for_update(skip_locked=True)
            .limit(_BATCH_SIZE)
        )
    ).scalars().all()
    if not due:
        return 0

    now = datetime.now(UTC)
    for schedule in due:
        scheduled_for = schedule.next_run_at.isoformat()
        db.add(
            ProactiveEvent(
                organization_id=schedule.organization_id,
                project_id=schedule.project_id,
                delivery_id=str(uuid.uuid4()),
                source_event_id=f"maintenance:{schedule.id}:{scheduled_for}"[:320],
                event_type="scheduled_maintenance",
                action="due",
                repository_name="",
                workflow_name=f"{schedule.maintenance_type.replace('_', ' ').title()}: {schedule.title}"[:255],
                conclusion="scheduled",
                branch="",
                details_url="",
                status="pending",
            )
        )
        # Skip missed intervals rather than flooding the inbox after downtime.
        schedule.next_run_at = now + timedelta(days=schedule.interval_days)
    await db.commit()
    return len(due)


async def run_scheduled_workflows() -> None:
    """Execute due schedules; the executor's conditional lease makes polling multi-worker safe."""
    heartbeat_task = asyncio.create_task(_worker_heartbeat_loop(), name="workflow-worker-heartbeat")
    try:
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
                    await _queue_due_maintenance_events(db)
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
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        try:
            await _write_worker_heartbeat("stopped")
        except Exception:
            logger.exception("Unable to mark workflow worker as stopped")
