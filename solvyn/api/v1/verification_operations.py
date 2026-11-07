"""Organization-admin views of durable verification queue state."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import OWNER_ADMIN, require_org_membership, require_tenant
from solvyn.db.models import (
    Approval,
    Organization,
    OrganizationMembership,
    Project,
    Task,
    TaskHandoff,
    TaskWorkspace,
    TeammateProfile,
    TeammateProjectAssignment,
    VerificationRun,
)
from solvyn.db.session import bind_tenant_scope, get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(prefix="/verification-queue", tags=["verification-operations"])

_QUEUE_STATUSES = (
    "pending_approval",
    "approved",
    "running",
    "interrupted",
    "stale",
    "completed",
    "failed",
    "dead_letter",
)
_MAX_ATTEMPTS = 3


@router.get("", summary="Inspect verification queue and dead-letter state")
async def get_verification_queue(
    status: Literal[
        "pending_approval",
        "approved",
        "running",
        "interrupted",
        "stale",
        "completed",
        "failed",
        "dead_letter",
    ]
    | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10_000),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Return bounded queue metadata for organizations the caller administers."""
    tenant_id = require_tenant()
    user_id = tenant_context.user_id
    if user_id is None:
        raise HTTPException(status_code=403, detail="Organization administrator access required")

    await bind_tenant_scope(db, tenant_id)
    organization_ids = list(
        (
            await db.execute(
                select(OrganizationMembership.organization_id)
                .join(Organization, Organization.id == OrganizationMembership.organization_id)
                .where(
                    OrganizationMembership.user_id == user_id,
                    OrganizationMembership.role.in_(OWNER_ADMIN),
                    Organization.tenant_id == tenant_id,
                    Organization.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    if not organization_ids:
        raise HTTPException(status_code=403, detail="Organization administrator access required")

    queue_filters = (
        VerificationRun.tenant_id == tenant_id,
        Task.tenant_id == tenant_id,
        Approval.organization_id.in_(organization_ids),
    )
    counts = (
        await db.execute(
            select(VerificationRun.status, func.count())
            .join(Task, Task.id == VerificationRun.task_id)
            .join(Approval, Approval.verification_run_id == VerificationRun.id)
            .where(*queue_filters)
            .group_by(VerificationRun.status)
        )
    ).all()

    queue_scope = (
        select(VerificationRun, Task.title, Approval.organization_id, Approval.status)
        .join(Task, Task.id == VerificationRun.task_id)
        .join(Approval, Approval.verification_run_id == VerificationRun.id)
        .where(*queue_filters)
    )
    total_query = (
        select(func.count())
        .select_from(VerificationRun)
        .join(Task, Task.id == VerificationRun.task_id)
        .join(Approval, Approval.verification_run_id == VerificationRun.id)
        .where(*queue_filters)
    )
    if status is not None:
        queue_scope = queue_scope.where(VerificationRun.status == status)
        total_query = total_query.where(VerificationRun.status == status)
    total = int((await db.execute(total_query)).scalar_one())
    now = datetime.now(UTC)
    rows = (
        await db.execute(
            queue_scope
            .order_by(VerificationRun.started_at.desc(), VerificationRun.id.desc())
            .offset(offset)
            .limit(limit)
        )
    ).all()
    redriven_to_by_run: dict[uuid.UUID, uuid.UUID] = {}
    if rows:
        child_links = await db.execute(
            select(VerificationRun.redriven_from_run_id, VerificationRun.id).where(
                VerificationRun.tenant_id == tenant_id,
                VerificationRun.redriven_from_run_id.in_([run.id for run, *_ in rows]),
            )
        )
        redriven_to_by_run = {
            source_id: child_id for source_id, child_id in child_links.all() if source_id is not None
        }

    return {
        "counts": {queue_status: int(count) for queue_status, count in counts if queue_status in _QUEUE_STATUSES},
        "status_filter": status,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": str(run.id),
                "redriven_from_run_id": str(run.redriven_from_run_id) if run.redriven_from_run_id else None,
                "redriven_to_run_id": str(redriven_to_by_run[run.id]) if run.id in redriven_to_by_run else None,
                "task_id": str(run.task_id),
                "execution_id": str(run.execution_id) if run.execution_id else None,
                "task_title": task_title,
                "organization_id": str(organization_id),
                "kind": run.kind,
                "status": run.status,
                "approval_status": approval_status,
                "attempt_count": run.attempt_count,
                "max_attempts": _MAX_ATTEMPTS,
                "retryable": (
                    run.status in {"approved", "interrupted"}
                    and run.attempt_count < _MAX_ATTEMPTS
                    and approval_status in {"approved", "consumed"}
                    and (
                        run.status != "interrupted"
                        or run.lease_expires_at is None
                        or run.lease_expires_at <= now
                    )
                ),
                "attempt_started_at": run.attempt_started_at.isoformat() if run.attempt_started_at else None,
                "lease_expires_at": run.lease_expires_at.isoformat() if run.lease_expires_at else None,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            }
            for run, task_title, organization_id, approval_status in rows
        ],
    }


def _redrive_response(run: VerificationRun, approval: Approval, *, already_redriven: bool) -> dict[str, Any]:
    from solvyn.api.v1.verification import _verification_dict

    return {
        "verification_run": _verification_dict(run),
        "approval_id": str(approval.id),
        "approval_status": approval.status,
        "redriven_from_run_id": str(run.redriven_from_run_id),
        "already_redriven": already_redriven,
    }


@router.post("/{verification_run_id}/redrive", status_code=202, summary="Request approval to redrive a dead-letter verification")
async def redrive_verification_run(
    verification_run_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Create one fresh, approval-gated run while preserving the dead letter."""
    tenant_id = require_tenant()
    user_id = tenant_context.user_id
    if user_id is None:
        raise HTTPException(status_code=403, detail="Organization administrator access required")
    await bind_tenant_scope(db, tenant_id)

    source = (
        await db.execute(
            select(VerificationRun)
            .where(VerificationRun.id == verification_run_id, VerificationRun.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Verification run not found")
    source_id = source.id

    source_approval = (
        await db.execute(select(Approval).where(Approval.verification_run_id == source_id))
    ).scalar_one_or_none()
    if source_approval is None:
        raise HTTPException(status_code=409, detail="Dead-letter verification has no approval record")
    await require_org_membership(db, source_approval.organization_id, roles=OWNER_ADMIN)

    existing = (
        await db.execute(
            select(VerificationRun).where(
                VerificationRun.tenant_id == tenant_id,
                VerificationRun.redriven_from_run_id == source_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_approval = (
            await db.execute(select(Approval).where(Approval.verification_run_id == existing.id))
        ).scalar_one_or_none()
        if existing_approval is None:
            raise HTTPException(status_code=409, detail="Redriven verification has no approval record")
        return _redrive_response(existing, existing_approval, already_redriven=True)
    if source.status != "dead_letter":
        raise HTTPException(status_code=409, detail="Only dead-lettered verifications can be redriven")

    task = (
        await db.execute(select(Task).where(Task.id == source.task_id, Task.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=409, detail="Verification task is no longer available")
    if task.status in {"completed", "failed", "cancelled", "canceled", "rejected"}:
        raise HTTPException(status_code=409, detail="A terminal task cannot be redriven")

    handoff = (
        await db.execute(
            select(TaskHandoff).where(
                TaskHandoff.tenant_id == tenant_id,
                TaskHandoff.child_task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    if (
        handoff is None
        or handoff.status not in {"pending", "running", "failed", "completed"}
        or handoff.project_id != source_approval.project_id
        or handoff.teammate_id != source_approval.teammate_id
    ):
        raise HTTPException(status_code=409, detail="The task's project assignment is no longer available")

    project = (
        await db.execute(
            select(Project).where(
                Project.id == handoff.project_id,
                Project.tenant_id == tenant_id,
                Project.organization_id == source_approval.organization_id,
                Project.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    teammate = (
        await db.execute(
            select(TeammateProfile).where(
                TeammateProfile.id == handoff.teammate_id,
                TeammateProfile.tenant_id == tenant_id,
                TeammateProfile.organization_id == source_approval.organization_id,
                TeammateProfile.is_enabled.is_(True),
            )
        )
    ).scalar_one_or_none()
    assignment = await db.scalar(
        select(TeammateProjectAssignment.id).where(
            TeammateProjectAssignment.teammate_id == handoff.teammate_id,
            TeammateProjectAssignment.project_id == handoff.project_id,
        )
    )
    if project is None or teammate is None or assignment is None:
        raise HTTPException(status_code=409, detail="The task's project assignment is no longer active")

    workspace_record = (
        await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.id == source.workspace_id,
                TaskWorkspace.tenant_id == tenant_id,
                TaskWorkspace.task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    if workspace_record is None:
        raise HTTPException(status_code=409, detail="Verification workspace is no longer available")
    try:
        from solvyn.api.v1.verification import _DEFAULT_VERIFICATION_COMMANDS, _workspace_manager
        from solvyn.config import settings
        from solvyn.sandbox.workspaces import WorkspaceError

        command = json.loads(source.command)
        if (
            source.kind not in _DEFAULT_VERIFICATION_COMMANDS
            or not isinstance(command, list)
            or not command
            or any(not isinstance(part, str) or not part or "\x00" in part for part in command)
            or any(len(part.encode("utf-8")) > 4096 for part in command)
            or sum(len(part.encode("utf-8")) for part in command) > 16_384
        ):
            raise ValueError("Stored command is invalid")
        manager = _workspace_manager()
        workspace = manager.get(workspace_record.id)
        source_digest = manager.fingerprint(workspace)
    except (ValueError, TypeError, UnicodeError, WorkspaceError) as exc:
        raise HTTPException(status_code=409, detail="Verification workspace or stored command is invalid") from exc

    run_id = uuid.uuid4()
    redriven_run = VerificationRun(
        id=run_id,
        tenant_id=tenant_id,
        task_id=task.id,
        execution_id=source.execution_id,
        workspace_id=workspace_record.id,
        redriven_from_run_id=source.id,
        kind=source.kind,
        command=json.dumps(command),
        status="pending_approval",
        exit_code=None,
        timed_out=False,
        output_truncated=False,
        stdout="",
        stderr="",
        error=None,
        duration_seconds=None,
        runner_version="sandbox-service/1" if settings.sandbox_runtime_url else "docker-sandbox/1",
        source_digest=source_digest,
        attempt_count=0,
        attempt_started_at=None,
        lease_owner=None,
        lease_expires_at=None,
        completed_at=None,
    )
    approval = Approval(
        organization_id=source_approval.organization_id,
        project_id=project.id,
        teammate_id=teammate.id,
        verification_run_id=run_id,
        kind="verification_run",
        summary=f"Redrive {redriven_run.kind} verification for {task.title}",
        payload=json.dumps(
            {
                "execution_id": str(redriven_run.execution_id) if redriven_run.execution_id else None,
                "task_id": str(task.id),
                "verification_run_id": str(run_id),
                "redriven_from_run_id": str(source.id),
                "workspace_id": str(workspace_record.id),
                "kind": redriven_run.kind,
                "command": command,
                "source_digest": source_digest,
            }
        ),
        requested_by=str(user_id),
    )
    db.add(redriven_run)
    await db.flush()
    db.add(approval)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await bind_tenant_scope(db, tenant_id)
        existing = (
            await db.execute(
                select(VerificationRun).where(
                    VerificationRun.tenant_id == tenant_id,
                    VerificationRun.redriven_from_run_id == source_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        existing_approval = (
            await db.execute(select(Approval).where(Approval.verification_run_id == existing.id))
        ).scalar_one_or_none()
        if existing_approval is None:
            raise HTTPException(status_code=409, detail="Redriven verification has no approval record")
        return _redrive_response(existing, existing_approval, already_redriven=True)
    await bind_tenant_scope(db, tenant_id)
    await db.refresh(redriven_run)
    await db.refresh(approval)
    return _redrive_response(redriven_run, approval, already_redriven=False)
