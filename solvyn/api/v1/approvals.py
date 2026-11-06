"""Human approval inbox.

Agents request approval for plan / command / file-change / pull-request /
merge / deploy actions. A human approves or rejects; the decision is
audited, and linked teammate handoffs become resumable after approval.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership, require_tenant
from solvyn.db.models import Approval, Organization, OrganizationMembership, Project, Task, TaskHandoff, TeammateProfile, VerificationRun
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["approvals"])


def _approval_dict(a: Approval, *, include_payload: bool = True) -> dict[str, object]:
    return {
        "id": str(a.id),
        "organization_id": str(a.organization_id),
        "project_id": str(a.project_id) if a.project_id else None,
        "teammate_id": str(a.teammate_id) if a.teammate_id else None,
        "task_handoff_id": str(a.task_handoff_id) if a.task_handoff_id else None,
        "verification_run_id": str(a.verification_run_id) if a.verification_run_id else None,
        "kind": a.kind,
        "summary": a.summary,
        "payload": a.payload if include_payload else None,
        "status": a.status,
        "requested_by": a.requested_by,
        "decided_by": str(a.decided_by) if a.decided_by else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decision_note": a.decision_note or "",
        "created_at": a.created_at.isoformat() if a.created_at else "",
        "updated_at": a.updated_at.isoformat() if a.updated_at else "",
    }


class ApprovalCreate(BaseModel):
    organization_id: uuid.UUID
    project_id: uuid.UUID | None = None
    teammate_id: uuid.UUID | None = None
    kind: str = Field(..., min_length=1, max_length=50)
    summary: str = Field(..., min_length=1)
    payload: str | None = None
    requested_by: str = ""


class DecisionIn(BaseModel):
    note: str = ""


@router.get("/approvals", summary="List pending approvals (inbox)")
async def list_approvals(status: str = "pending", db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    user_id = tenant_context.user_id
    if user_id is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    result = await db.execute(
        select(Approval, OrganizationMembership.role)
        .join(Organization, Approval.organization_id == Organization.id)
        .join(
            OrganizationMembership,
            (OrganizationMembership.organization_id == Organization.id)
            & (OrganizationMembership.user_id == user_id)
            & OrganizationMembership.role.in_(ANY_ROLE),
        )
        .where(Organization.tenant_id == tenant_id, Organization.is_active == True, Approval.status == status)  # noqa: E712
        .order_by(Approval.created_at.desc())
    )
    items = [_approval_dict(approval, include_payload=role in OWNER_ADMIN) for approval, role in result.all()]
    return {"approvals": items, "count": len(items)}


@router.post("/approvals", status_code=201, summary="Request approval (agent-facing)")
async def create_approval(payload: ApprovalCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    await require_org_membership(db, payload.organization_id, roles=("owner", "admin", "member"))
    if payload.project_id is not None:
        project = await db.get(Project, payload.project_id)
        if project is None or project.organization_id != payload.organization_id:
            raise HTTPException(status_code=404, detail="Project not found")
    if payload.teammate_id is not None:
        teammate = await db.get(TeammateProfile, payload.teammate_id)
        if teammate is None or teammate.organization_id != payload.organization_id:
            raise HTTPException(status_code=404, detail="Teammate not found")
    approval = Approval(
        organization_id=payload.organization_id,
        project_id=payload.project_id,
        teammate_id=payload.teammate_id,
        kind=payload.kind,
        summary=payload.summary,
        payload=payload.payload,
        requested_by=payload.requested_by,
    )
    db.add(approval)
    await db.commit()
    await db.refresh(approval)
    return _approval_dict(approval)


@router.get("/approvals/{approval_id}", summary="Get an approval")
async def get_approval(approval_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    membership = await require_org_membership(db, approval.organization_id, roles=ANY_ROLE)
    return _approval_dict(approval, include_payload=membership.role in OWNER_ADMIN)


@router.post("/approvals/{approval_id}/approve", summary="Approve a pending request")
async def approve(approval_id: uuid.UUID, payload: DecisionIn, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    # Merge/deploy approvals are a human authorization boundary: only org owners/admins.
    await require_org_membership(db, approval.organization_id, roles=OWNER_ADMIN)
    if approval.status != "pending":
        raise HTTPException(status_code=400, detail=f"Approval is already {approval.status}")
    if approval.task_handoff_id is not None:
        tenant_id = require_tenant()
        resumed = await db.execute(
            update(TaskHandoff)
            .where(
                TaskHandoff.id == approval.task_handoff_id,
                TaskHandoff.tenant_id == tenant_id,
                TaskHandoff.status == "waiting_approval",
            )
            .values(status="pending", error=None)
            .returning(TaskHandoff.child_task_id)
        )
        child_task_id = resumed.scalar_one_or_none()
        if child_task_id is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Task handoff is no longer waiting for approval")
        resumed_task = await db.execute(
            update(Task)
            .where(Task.id == child_task_id, Task.tenant_id == tenant_id, Task.status == "waiting_approval")
            .values(status="pending", error=None)
            .returning(Task.id)
        )
        if resumed_task.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Task handoff is no longer waiting for approval")
    if approval.verification_run_id is not None:
        tenant_id = require_tenant()
        verification = await db.execute(
            update(VerificationRun)
            .where(
                VerificationRun.id == approval.verification_run_id,
                VerificationRun.tenant_id == tenant_id,
                VerificationRun.status == "pending_approval",
            )
            .values(status="approved")
            .returning(VerificationRun.id)
        )
        if verification.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Verification request is no longer pending approval")
    decision = await db.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == "pending")
        .values(
            status="approved",
            decided_by=tenant_context.user_id,
            decided_at=func.now(),
            decision_note=payload.note,
            updated_at=func.now(),
        )
        .returning(Approval.id)
    )
    if decision.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Approval is no longer pending")
    await db.commit()
    await db.refresh(approval)
    return _approval_dict(approval)


@router.post("/approvals/{approval_id}/reject", summary="Reject a pending request")
async def reject(approval_id: uuid.UUID, payload: DecisionIn, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    await require_org_membership(db, approval.organization_id, roles=OWNER_ADMIN)
    if approval.status != "pending":
        raise HTTPException(status_code=400, detail=f"Approval is already {approval.status}")
    if approval.task_handoff_id is not None:
        tenant_id = require_tenant()
        rejected = await db.execute(
            update(TaskHandoff)
            .where(
                TaskHandoff.id == approval.task_handoff_id,
                TaskHandoff.tenant_id == tenant_id,
                TaskHandoff.status == "waiting_approval",
            )
            .values(status="rejected", error=payload.note or "Task handoff was rejected")
            .returning(TaskHandoff.child_task_id)
        )
        child_task_id = rejected.scalar_one_or_none()
        if child_task_id is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Task handoff is no longer waiting for approval")
        rejected_task = await db.execute(
            update(Task)
            .where(Task.id == child_task_id, Task.tenant_id == tenant_id, Task.status == "waiting_approval")
            .values(status="failed", error=payload.note or "Task handoff was rejected", completed_at=func.now())
            .returning(Task.id)
        )
        if rejected_task.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Task handoff is no longer waiting for approval")
    if approval.verification_run_id is not None:
        tenant_id = require_tenant()
        verification = await db.execute(
            update(VerificationRun)
            .where(
                VerificationRun.id == approval.verification_run_id,
                VerificationRun.tenant_id == tenant_id,
                VerificationRun.status == "pending_approval",
            )
            .values(status="rejected", error=payload.note or "Verification request was rejected", completed_at=func.now())
            .returning(VerificationRun.id)
        )
        if verification.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Verification request is no longer pending approval")
    decision = await db.execute(
        update(Approval)
        .where(Approval.id == approval_id, Approval.status == "pending")
        .values(
            status="rejected",
            decided_by=tenant_context.user_id,
            decided_at=func.now(),
            decision_note=payload.note,
            updated_at=func.now(),
        )
        .returning(Approval.id)
    )
    if decision.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Approval is no longer pending")
    await db.commit()
    await db.refresh(approval)
    return _approval_dict(approval)
