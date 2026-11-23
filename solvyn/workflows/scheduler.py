"""Poll PostgreSQL for due workflow executions and hand them to leased workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import socket
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from opentelemetry.trace import Status, StatusCode
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import (
    Approval,
    ExecutionGraph,
    MaintenanceSchedule,
    Organization,
    ProactiveEvent,
    Project,
    Repository,
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
from solvyn.observability.opentelemetry import get_tracer
from solvyn.proactive_dependabot import dependabot_alert_signal
from solvyn.proactive_dependabot_state import polling_delivery_id, record_dependabot_alert
from solvyn.repositories.credentials import CredentialEncryptionUnavailableError, GitHubCredentialNotFoundError
from solvyn.repositories.github import GitHubRepositoryError
from solvyn.repositories.providers import get_authorized_repository_provider
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
_MAX_DEPENDABOT_REPOSITORIES = 10


async def _trace_execution(
    span_name: str,
    execution_id: uuid.UUID,
    execution_kind: str,
    operation: Callable[[], Awaitable[str]],
) -> str:
    """Trace a worker operation with identifiers and bounded outcome labels only."""
    with get_tracer("solvyn.scheduler").start_as_current_span(
        span_name,
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        span.set_attribute("solvyn.execution.id", str(execution_id))
        span.set_attribute("solvyn.execution.kind", execution_kind)
        try:
            outcome = await operation()
        except asyncio.CancelledError:
            span.set_attribute("solvyn.execution.outcome", "cancelled")
            raise
        except Exception:
            span.set_attribute("solvyn.execution.outcome", "failed")
            span.set_status(Status(StatusCode.ERROR))
            raise
        span.set_attribute("solvyn.execution.outcome", outcome)
        if outcome == "failed":
            span.set_status(Status(StatusCode.ERROR))
        return outcome


def _dependabot_error_outcome(exc: Exception) -> tuple[str, str, str]:
    """Map provider failures to safe, explicit triage outcomes."""
    if isinstance(exc, GitHubRepositoryError):
        if exc.status_code == 403:
            return "blocked", "permission_required", "blocked"
        if exc.status_code == 401:
            return "blocked", "credential_invalid", "blocked"
        if exc.status_code == 404:
            return "blocked", "alerts_unavailable_or_not_enabled", "blocked"
        if exc.status_code == 429:
            return "failed", "rate_limited", "failed"
        return "failed", "provider_unavailable", "failed"
    if isinstance(exc, GitHubCredentialNotFoundError | CredentialEncryptionUnavailableError):
        return "blocked", "github_credential_unavailable", "blocked"
    return "failed", "provider_unavailable", "failed"


async def _reconcile_dependabot_schedule(db: AsyncSession, schedule: MaintenanceSchedule) -> None:
    """Queue a bounded snapshot of open Dependabot alerts for this project."""
    repositories = list((
        await db.execute(
            select(Repository)
            .where(
                Repository.project_id == schedule.project_id,
                Repository.provider == "github",
                Repository.is_active.is_(True),
            )
            .order_by(Repository.full_name)
            .limit(_MAX_DEPENDABOT_REPOSITORIES + 1)
        )
    ).scalars().all())
    exceeded_repository_limit = len(repositories) > _MAX_DEPENDABOT_REPOSITORIES
    if exceeded_repository_limit:
        # A partial repository set cannot establish an organization-wide zero.
        repositories = []
    signals: dict[str, tuple[uuid.UUID, dict[str, Any]]] = {}
    incomplete_reason = "repository_limit_reached" if exceeded_repository_limit else ""
    incomplete_action = "incomplete" if incomplete_reason else ""
    incomplete_status = "blocked" if incomplete_reason else ""

    for repository in repositories:
        try:
            provider = await get_authorized_repository_provider(
                repository.provider,
                db,
                repository,
                schedule.organization_id,
                permissions={"metadata": "read", "vulnerability_alerts": "read"},
            )
            async with provider:
                alerts, complete = await provider.list_dependabot_alerts(
                    repository.full_name,
                    states=("auto_dismissed", "dismissed", "fixed", "open"),
                )
        except Exception as exc:
            logger.warning(
                "Dependabot reconciliation unavailable for repository %s (schedule %s): %s",
                repository.full_name[:255],
                schedule.id,
                type(exc).__name__,
            )
            if not incomplete_reason:
                incomplete_action, incomplete_reason, incomplete_status = _dependabot_error_outcome(exc)
            continue

        if not complete and not incomplete_reason:
            incomplete_reason = "alert_page_limit_reached"
            incomplete_action, incomplete_status = "incomplete", "blocked"
        for alert in alerts:
            signal = dependabot_alert_signal(repository.full_name, alert, action="created")
            if signal is not None:
                signals[signal["source_event_id"]] = (repository.id, signal)
            elif not incomplete_reason:
                incomplete_reason = "invalid_alert_data"
                incomplete_action, incomplete_status = "incomplete", "failed"

    for repository_id, signal in signals.values():
        await record_dependabot_alert(
            db,
            organization_id=schedule.organization_id,
            project_id=schedule.project_id,
            repository_id=repository_id,
            signal=signal,
            delivery_id=polling_delivery_id(signal["source_event_id"], signal["provider_updated_at"]),
            source="poll",
        )

    scheduled_for = schedule.next_run_at.isoformat()
    if incomplete_reason:
        action, conclusion, status = incomplete_action or "incomplete", incomplete_reason, incomplete_status or "blocked"
    elif not repositories:
        action, conclusion, status = "completed", "no_connected_repositories", "pending"
    else:
        open_alert_count = sum(1 for _, signal in signals.values() if signal["notify"])
        action, conclusion, status = "completed", f"open_alerts:{open_alert_count}", "pending"
    reconciliation_id = f"dependabot-reconciliation:{schedule.id}:{scheduled_for}"[:320]
    db.add(
        ProactiveEvent(
            organization_id=schedule.organization_id,
            project_id=schedule.project_id,
            delivery_id="dependabot-run:" + hashlib.sha256(reconciliation_id.encode("utf-8")).hexdigest(),
            source_event_id=reconciliation_id,
            event_type="dependency_review",
            action=action,
            repository_name="",
            workflow_name=f"Dependabot review: {schedule.title}"[:255],
            conclusion=conclusion[:50],
            branch="",
            details_url="",
            status=status,
        )
    )


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
            if schedule.maintenance_type == "dependency_review":
                await _reconcile_dependabot_schedule(db, schedule)
                # Skip missed intervals rather than flooding the inbox after downtime.
                schedule.next_run_at = now + timedelta(days=schedule.interval_days)
                continue
            if schedule.maintenance_type in ("code_quality", "documentation_review") and schedule.task_id is not None:
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
                    from solvyn.api.v1.verification import VerificationKind, _workspace_manager
                    from solvyn.config import settings
                    from solvyn.sandbox.dependencies import DependencyBlockedError
                    from solvyn.sandbox.profiles import command_for_profile
                    from solvyn.sandbox.workspaces import WorkspaceError

                    # The API validator and database constraint bound this persisted value.
                    verification_kind = cast(VerificationKind, schedule.verification_kind)
                    manager = _workspace_manager()
                    try:
                        workspace = manager.get(workspace_record.id)
                        source_digest = manager.fingerprint(workspace)
                        command = command_for_profile(workspace.path, verification_kind)
                    except (WorkspaceError, DependencyBlockedError, OSError, ValueError):
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
                            kind=verification_kind,
                            command=json.dumps(command),
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
                                summary=f"Scheduled {verification_kind} check: {schedule.title}",
                                payload=json.dumps(
                                    {
                                        "execution_id": str(task.execution_id) if task.execution_id else None,
                                        "task_id": str(task.id),
                                        "verification_run_id": str(run.id),
                                        "workspace_id": str(workspace_record.id),
                                        "maintenance_schedule_id": str(schedule.id),
                                        "scheduled_for": scheduled_for,
                                        "kind": verification_kind,
                                        "command": command,
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


async def _fetch_due_cancellation_workflows(db: AsyncSession) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Discover tenant-scoped cancellations whose worker lease can no longer finish them."""
    tenant_ids = (
        await db.execute(select(Tenant.id).where(Tenant.is_active.is_(True)).order_by(Tenant.id))
    ).scalars().all()
    due_rows: list[tuple[uuid.UUID, uuid.UUID, datetime]] = []
    for tenant_id in tenant_ids:
        await bind_tenant_scope(db, tenant_id)
        rows = (
            await db.execute(
                select(ExecutionGraph.id, Session.tenant_id, ExecutionGraph.created_at)
                .join(Session, Session.id == ExecutionGraph.session_id)
                .where(
                    Session.tenant_id == tenant_id,
                    ExecutionGraph.status == "cancelling",
                    or_(
                        ExecutionGraph.lease_owner.is_(None),
                        ExecutionGraph.lease_expires_at.is_(None),
                        ExecutionGraph.lease_expires_at <= func.now(),
                    ),
                )
                .order_by(ExecutionGraph.created_at, ExecutionGraph.id)
                .limit(_BATCH_SIZE)
            )
        ).all()
        due_rows.extend((row.id, row.tenant_id, row.created_at) for row in rows if row.created_at is not None)
        await db.commit()

    due_rows.sort(key=lambda row: (row[2], row[0]))
    return [(execution_id, tenant_id) for execution_id, tenant_id, _ in due_rows[:_BATCH_SIZE]]


async def _resume_scheduled_workflow(execution_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Resume one due workflow while recording a privacy-safe execution span."""
    async def resume() -> str:
        try:
            await workflow_executor.resume(execution_id, str(tenant_id))
        except RuntimeError:
            # Another scheduler claimed it, or the schedule was cancelled.
            logger.info("Scheduled workflow %s was not claimed", execution_id)
            return "not_claimed"
        except Exception:
            logger.exception("Scheduled workflow %s failed", execution_id)
            return "failed"
        return "completed"

    await _trace_execution(
        "solvyn.scheduler.workflow.resume",
        execution_id,
        "workflow",
        resume,
    )


async def _finalize_stale_workflow_cancellation(execution_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
    """Finalize a stale cancellation with a privacy-safe execution span."""
    async def finalize() -> str:
        try:
            await workflow_executor.cancel(execution_id, str(tenant_id))
        except RuntimeError:
            logger.info("Stale workflow cancellation %s was not finalized", execution_id)
            return "not_claimed"
        return "completed"

    await _trace_execution(
        "solvyn.scheduler.workflow.cancel",
        execution_id,
        "workflow_cancellation",
        finalize,
    )


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
    """Claim one approved verification and record its bounded execution outcome."""
    from fastapi import HTTPException

    from solvyn.api.v1.verification import _execute_approved_verification

    async def execute() -> str:
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
                return "not_found"
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
                return "missing_context"
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
                        return "not_claimed"
                    raise
        return "completed"

    await _trace_execution(
        "solvyn.scheduler.verification.execute",
        run_id,
        "verification",
        execute,
    )


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
                    due_cancellations = await _fetch_due_cancellation_workflows(db)
                    due_verifications = await _fetch_due_verification_runs(db)
            except Exception:
                logger.exception("Unable to fetch scheduled workflow executions")
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
                continue

            for execution_id, tenant_id in due:
                await _resume_scheduled_workflow(execution_id, tenant_id)

            for execution_id, tenant_id in due_cancellations:
                try:
                    await _finalize_stale_workflow_cancellation(execution_id, tenant_id)
                except Exception:
                    logger.exception("Stale workflow cancellation %s failed", execution_id)

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
