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
    Tenant,
    VerificationRun,
    WorkflowWorkerHeartbeat,
)
from solvyn.db.session import async_session_factory, bind_tenant_scope
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
_MAINTENANCE_TENANT_OFFSET = 0


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
    """Queue a bounded batch of due maintenance reminders per tenant scope."""
    global _MAINTENANCE_TENANT_OFFSET
    tenant_ids = list((
        await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.id))
    ).scalars().all())
    if tenant_ids:
        start = _MAINTENANCE_TENANT_OFFSET % len(tenant_ids)
        tenant_ids = tenant_ids[start:] + tenant_ids[:start]
        _MAINTENANCE_TENANT_OFFSET = (start + 1) % len(tenant_ids)
    queued = 0
    for tenant_id in tenant_ids:
        remaining = _BATCH_SIZE - queued
        if remaining <= 0:
            break
        await bind_tenant_scope(db, tenant_id)
        due = (
            await db.execute(
                select(MaintenanceSchedule)
                .join(Project, Project.id == MaintenanceSchedule.project_id)
                .join(Organization, Organization.id == MaintenanceSchedule.organization_id)
                .where(
                    MaintenanceSchedule.enabled.is_(True),
                    MaintenanceSchedule.next_run_at <= func.now(),
                    MaintenanceSchedule.organization_id == Organization.id,
                    Organization.tenant_id == tenant_id,
                    Project.organization_id == MaintenanceSchedule.organization_id,
                    Project.tenant_id == tenant_id,
                    Project.is_active.is_(True),
                    Organization.is_active.is_(True),
                )
                .order_by(MaintenanceSchedule.next_run_at, MaintenanceSchedule.created_at)
                .with_for_update(skip_locked=True)
                .limit(remaining)
            )
        ).scalars().all()
        if not due:
            await db.commit()
            continue

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
        queued += len(due)
    return queued


async def _fetch_due_scheduled_workflows(db: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Read due executions one tenant at a time under forced Session RLS."""
    tenant_ids = (
        await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.id))
    ).scalars().all()
    due_rows: list[tuple[uuid.UUID, uuid.UUID, datetime, datetime]] = []
    for tenant_id in tenant_ids:
        await bind_tenant_scope(db, tenant_id)
        rows = (
            await db.execute(
                select(ExecutionGraph.id, Session.tenant_id, ExecutionGraph.scheduled_at, ExecutionGraph.created_at)
                .join(Session, Session.id == ExecutionGraph.session_id)
                .where(
                    Session.tenant_id == tenant_id,
                    ExecutionGraph.status == "scheduled",
                    ExecutionGraph.scheduled_at <= func.now(),
                )
                .order_by(ExecutionGraph.scheduled_at, ExecutionGraph.created_at, ExecutionGraph.id)
                .limit(_BATCH_SIZE)
            )
        ).all()
        for row in rows:
            if row.scheduled_at is None or row.created_at is None:
                continue
            due_rows.append((row.id, row.tenant_id, row.scheduled_at, row.created_at))
        await db.commit()

    due_rows.sort(key=lambda row: (row[2], row[3], row[0]))
    return [(execution_id, tenant_id) for execution_id, tenant_id, _, _ in due_rows[:_BATCH_SIZE]]


async def _interrupt_stale_verification_runs() -> None:
    """Mark interrupted runs for each tenant under that tenant's RLS scope."""
    async with async_session_factory() as db:
        tenant_ids = (await db.execute(select(Tenant.id))).scalars().all()
        for tenant_id in tenant_ids:
            await bind_tenant_scope(db, tenant_id)
            await db.execute(
                update(VerificationRun)
                .where(
                    VerificationRun.tenant_id == tenant_id,
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


async def run_scheduled_workflows() -> None:
    """Execute due schedules; the executor's conditional lease makes polling multi-worker safe."""
    heartbeat_task = asyncio.create_task(_worker_heartbeat_loop(), name="workflow-worker-heartbeat")
    try:
        while True:
            try:
                await _interrupt_stale_verification_runs()
                async with async_session_factory() as db:
                    await db.commit()
                    await _queue_due_maintenance_events(db)
                    due = await _fetch_due_scheduled_workflows(db)
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
