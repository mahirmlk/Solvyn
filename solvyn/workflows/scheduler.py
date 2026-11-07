"""Poll PostgreSQL for due workflow executions and hand them to leased workers."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import (
    Approval,
    ExecutionGraph,
    MaintenanceSchedule,
    Organization,
    ProactiveEvent,
    Project,
    Session,
    Task,
    TaskHandoff,
    TaskWorkspace,
    TeammateProfile,
    TeammateProjectAssignment,
    Tenant,
    VerificationQueueControl,
    VerificationRun,
    WorkflowWorkerHeartbeat,
)
from solvyn.db.session import async_session_factory, bind_tenant_scope
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.workflows.executor import workflow_executor

logger = logging.getLogger(__name__)
_POLL_INTERVAL_SECONDS = 5
_BATCH_SIZE = 20
_VERIFICATION_BATCH_SIZE = 2
_VERIFICATION_STALE_AFTER = timedelta(minutes=10)
_VERIFICATION_MAX_ATTEMPTS = 3
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
            run: VerificationRun | None = None
            outcome = "scheduled"
            status = "pending"
            action = "due"
            if schedule.maintenance_type == "code_quality" and schedule.task_id is not None:
                target = (
                    await db.execute(
                        select(Task, TaskWorkspace, TaskHandoff, TeammateProfile)
                        .join(
                            TaskWorkspace,
                            (TaskWorkspace.task_id == Task.id)
                            & (TaskWorkspace.tenant_id == Task.tenant_id),
                        )
                        .join(
                            TaskHandoff,
                            (TaskHandoff.child_task_id == Task.id)
                            & (TaskHandoff.tenant_id == Task.tenant_id)
                            & (TaskHandoff.project_id == schedule.project_id),
                        )
                        .join(
                            TeammateProfile,
                            (TeammateProfile.id == TaskHandoff.teammate_id)
                            & (TeammateProfile.tenant_id == TaskHandoff.tenant_id)
                            & TeammateProfile.is_enabled.is_(True),
                        )
                        .join(
                            TeammateProjectAssignment,
                            (TeammateProjectAssignment.teammate_id == TeammateProfile.id)
                            & (TeammateProjectAssignment.project_id == schedule.project_id),
                        )
                        .where(
                            Task.id == schedule.task_id,
                            Task.tenant_id == tenant_id,
                            TaskWorkspace.task_id == schedule.task_id,
                            TaskWorkspace.tenant_id == tenant_id,
                            TaskHandoff.status.in_(("pending", "running", "failed", "completed")),
                            TaskHandoff.tenant_id == tenant_id,
                            TeammateProfile.organization_id == schedule.organization_id,
                        )
                    )
                ).first()
                if target is None:
                    outcome = "target_unavailable"
                    status = "blocked"
                    action = "blocked"
                else:
                    task, workspace_record, handoff, teammate = target
                    from solvyn.api.v1.verification import _DEFAULT_VERIFICATION_COMMANDS, _workspace_manager
                    from solvyn.config import settings
                    from solvyn.sandbox.workspaces import WorkspaceError

                    manager = _workspace_manager()
                    try:
                        workspace = manager.get(workspace_record.id)
                        source_digest = manager.fingerprint(workspace)
                    except (WorkspaceError, OSError, ValueError):
                        outcome = "workspace_unavailable"
                        status = "blocked"
                        action = "blocked"
                    else:
                        run = VerificationRun(
                            tenant_id=tenant_id,
                            task_id=task.id,
                            execution_id=task.execution_id,
                            workspace_id=workspace_record.id,
                            maintenance_schedule_id=schedule.id,
                            maintenance_scheduled_for=schedule.next_run_at,
                            kind="lint",
                            command=json.dumps(_DEFAULT_VERIFICATION_COMMANDS["lint"]),
                            status="pending_approval",
                            runner_version="sandbox-service/1" if settings.sandbox_runtime_url else "docker-sandbox/1",
                            source_digest=source_digest,
                        )
                        run.id = uuid.uuid4()
                        db.add(run)
                        await db.flush()
                        db.add(
                            Approval(
                                organization_id=schedule.organization_id,
                                project_id=schedule.project_id,
                                teammate_id=teammate.id,
                                verification_run_id=run.id,
                                kind="verification_run",
                                status="pending",
                                summary=f"Scheduled code quality check: {schedule.title}",
                                payload=json.dumps(
                                    {
                                        "execution_id": str(task.execution_id) if task.execution_id else None,
                                        "task_id": str(task.id),
                                        "verification_run_id": str(run.id),
                                        "workspace_id": str(workspace_record.id),
                                        "maintenance_schedule_id": str(schedule.id),
                                        "scheduled_for": scheduled_for,
                                        "kind": run.kind,
                                        "command": _DEFAULT_VERIFICATION_COMMANDS["lint"],
                                        "source_digest": source_digest,
                                    }
                                ),
                                requested_by=f"maintenance:{schedule.id}",
                            )
                        )
                        outcome = "awaiting_approval"
                        action = "approval_requested"

            db.add(
                ProactiveEvent(
                    organization_id=schedule.organization_id,
                    project_id=schedule.project_id,
                    delivery_id=str(uuid.uuid4()),
                    source_event_id=f"maintenance:{schedule.id}:{scheduled_for}"[:320],
                    event_type="scheduled_maintenance",
                    action=action,
                    repository_name="",
                    workflow_name=f"{schedule.maintenance_type.replace('_', ' ').title()}: {schedule.title}"[:255],
                    conclusion=outcome,
                    branch="",
                    details_url="",
                    status=status,
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
                select(
                    ExecutionGraph.id,
                    Session.tenant_id,
                    func.coalesce(ExecutionGraph.scheduled_at, ExecutionGraph.created_at).label("due_at"),
                    ExecutionGraph.created_at,
                )
                .join(Session, Session.id == ExecutionGraph.session_id)
                .where(
                    Session.tenant_id == tenant_id,
                    or_(
                        and_(
                            ExecutionGraph.status == "scheduled",
                            ExecutionGraph.scheduled_at <= func.now(),
                        ),
                        and_(
                            ExecutionGraph.status.in_(["running", "resuming"]),
                            or_(
                                ExecutionGraph.lease_expires_at.is_(None),
                                ExecutionGraph.lease_expires_at < func.now(),
                            ),
                        ),
                    ),
                )
                .order_by(ExecutionGraph.scheduled_at, ExecutionGraph.created_at, ExecutionGraph.id)
                .limit(_BATCH_SIZE)
            )
        ).all()
        for row in rows:
            if row.due_at is None or row.created_at is None:
                continue
            due_rows.append((row.id, row.tenant_id, row.due_at, row.created_at))
        await db.commit()

    due_rows.sort(key=lambda row: (row[2], row[3], row[0]))
    return [(execution_id, tenant_id) for execution_id, tenant_id, _, _ in due_rows[:_BATCH_SIZE]]


async def _fetch_due_verification_runs(db: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Discover approved work and interrupted attempts under each tenant scope."""
    tenant_ids = (
        await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.id))
    ).scalars().all()
    due_rows: list[tuple[uuid.UUID, uuid.UUID, datetime]] = []
    for tenant_id in tenant_ids:
        await bind_tenant_scope(db, tenant_id)
        paused_organization_ids = (
            await db.execute(
                select(VerificationQueueControl.organization_id).where(
                    VerificationQueueControl.tenant_id == tenant_id,
                    VerificationQueueControl.paused.is_(True),
                )
            )
        ).scalars().all()
        query = (
            select(VerificationRun.id, VerificationRun.tenant_id, VerificationRun.started_at)
                .join(Task, Task.id == VerificationRun.task_id)
                .join(Approval, Approval.verification_run_id == VerificationRun.id)
                .where(
                    VerificationRun.tenant_id == tenant_id,
                    VerificationRun.status.in_(["approved", "interrupted"]),
                    or_(
                        VerificationRun.status != "interrupted",
                        VerificationRun.lease_expires_at.is_(None),
                        VerificationRun.lease_expires_at <= func.now(),
                    ),
                    VerificationRun.attempt_count < _VERIFICATION_MAX_ATTEMPTS,
                    Approval.status.in_(["approved", "consumed"]),
                    Task.tenant_id == tenant_id,
                )
                .order_by(VerificationRun.started_at, VerificationRun.id)
                .limit(_VERIFICATION_BATCH_SIZE)
        )
        if paused_organization_ids:
            query = query.where(Approval.organization_id.not_in(paused_organization_ids))
        rows = (await db.execute(query)).all()
        due_rows.extend((row.id, row.tenant_id, row.started_at) for row in rows)
        await db.commit()

    due_rows.sort(key=lambda row: (row[2], row[0]))
    return [(run_id, tenant_id) for run_id, tenant_id, _ in due_rows[:_VERIFICATION_BATCH_SIZE]]


async def _execute_due_verification(run_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Claim and execute one persisted, already-approved verification."""
    from fastapi import HTTPException

    from solvyn.api.v1.verification import _execute_approved_verification

    async with async_session_factory() as db:
        await bind_tenant_scope(db, tenant_id)
        run = (
            await db.execute(
                select(VerificationRun).where(
                    VerificationRun.id == run_id,
                    VerificationRun.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if run is None:
            return
        task = (
            await db.execute(
                select(Task).where(Task.id == run.task_id, Task.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        approval = (
            await db.execute(
                select(Approval).where(Approval.verification_run_id == run.id)
            )
        ).scalar_one_or_none()
        if task is None or approval is None:
            logger.error("Approved verification %s has no tenant-owned task or approval", run_id)
            return
        with tenant_context.scoped_tenant(tenant_id):
            try:
                await _execute_approved_verification(
                    db,
                    task,
                    run,
                    approval,
                    recover_interrupted=True,
                )
            except HTTPException as exc:
                if exc.status_code == 409:
                    logger.info("Verification %s was claimed elsewhere or is not retryable", run_id)
                else:
                    raise


async def _interrupt_stale_verification_runs() -> None:
    """Release expired claims, and dead-letter runs that exhausted retries."""
    async with async_session_factory() as db:
        tenant_ids = (await db.execute(select(Tenant.id))).scalars().all()
        for tenant_id in tenant_ids:
            await bind_tenant_scope(db, tenant_id)
            stale = and_(
                VerificationRun.tenant_id == tenant_id,
                VerificationRun.status == "running",
                func.coalesce(VerificationRun.attempt_started_at, VerificationRun.started_at)
                < func.now() - _VERIFICATION_STALE_AFTER,
                or_(
                    VerificationRun.lease_expires_at.is_(None),
                    VerificationRun.lease_expires_at < func.now(),
                ),
            )
            await db.execute(
                update(VerificationRun)
                .where(
                    stale,
                    VerificationRun.attempt_count < _VERIFICATION_MAX_ATTEMPTS,
                )
                .values(
                    status="interrupted",
                    error="Worker stopped before persisting the verification result",
                    completed_at=func.now(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
            )
            await db.execute(
                update(VerificationRun)
                .where(
                    stale,
                    VerificationRun.attempt_count >= _VERIFICATION_MAX_ATTEMPTS,
                )
                .values(
                    status="dead_letter",
                    error="Verification exceeded the maximum number of recovery attempts",
                    completed_at=func.now(),
                    lease_owner=None,
                    lease_expires_at=None,
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
                    due_verifications = await _fetch_due_verification_runs(db)
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

            outcomes = await asyncio.gather(
                *(_execute_due_verification(run_id, tenant_id) for run_id, tenant_id in due_verifications),
                return_exceptions=True,
            )
            for (run_id, _), outcome in zip(due_verifications, outcomes, strict=True):
                if isinstance(outcome, Exception):
                    logger.exception(
                        "Approved verification %s failed in the recovery worker",
                        run_id,
                        exc_info=outcome,
                    )

            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
        try:
            await _write_worker_heartbeat("stopped")
        except Exception:
            logger.exception("Unable to mark workflow worker as stopped")
