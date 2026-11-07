"""Organization-admin views of durable verification queue state."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import OWNER_ADMIN, require_tenant
from solvyn.db.models import Approval, Organization, OrganizationMembership, Task, VerificationRun
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

    return {
        "counts": {queue_status: int(count) for queue_status, count in counts if queue_status in _QUEUE_STATUSES},
        "status_filter": status,
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": str(run.id),
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
