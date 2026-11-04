"""Human approval inbox.

Agents request approval for plan / command / file-change / pull-request /
merge / deploy actions. A human approves or rejects; the decision is
audited and the agent execution resumes (Phase 5 wires resume).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Approval, Organization
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["approvals"])


def _require_tenant() -> uuid.UUID:
    if not tenant_context.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    if tenant_context.tenant_id is None:
        raise HTTPException(status_code=401, detail="No tenant context")
    return tenant_context.tenant_id


def _approval_dict(a: Approval) -> dict[str, object]:
    return {
        "id": str(a.id),
        "organization_id": str(a.organization_id),
        "project_id": str(a.project_id) if a.project_id else None,
        "teammate_id": str(a.teammate_id) if a.teammate_id else None,
        "kind": a.kind,
        "summary": a.summary,
        "status": a.status,
        "requested_by": a.requested_by,
        "decided_by": str(a.decided_by) if a.decided_by else None,
        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
        "decision_note": a.decision_note or "",
        "created_at": a.created_at.isoformat() if a.created_at else "",
        "updated_at": a.updated_at.isoformat() if a.updated_at else "",
    }


class ApprovalCreate(BaseModel):
    organization_id: str
    project_id: str | None = None
    teammate_id: str | None = None
    kind: str = Field(..., min_length=1, max_length=50)
    summary: str = Field(..., min_length=1)
    payload: str | None = None
    requested_by: str = ""


class DecisionIn(BaseModel):
    note: str = ""


@router.get("/approvals", summary="List pending approvals (inbox)")
async def list_approvals(status: str = "pending", db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    result = await db.execute(
        select(Approval)
        .join(Organization, Approval.organization_id == Organization.id)
        .where(Organization.tenant_id == tenant_id, Approval.status == status)
        .order_by(Approval.created_at.desc())
    )
    items = [_approval_dict(a) for a in result.scalars().all()]
    return {"approvals": items, "count": len(items)}


@router.post("/approvals", status_code=201, summary="Request approval (agent-facing)")
async def create_approval(payload: ApprovalCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org_id = uuid.UUID(payload.organization_id)
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    approval = Approval(
        organization_id=org_id,
        project_id=uuid.UUID(payload.project_id) if payload.project_id else None,
        teammate_id=uuid.UUID(payload.teammate_id) if payload.teammate_id else None,
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
    tenant_id = _require_tenant()
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    org = await db.get(Organization, approval.organization_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Approval not found")
    return _approval_dict(approval)


@router.post("/approvals/{approval_id}/approve", summary="Approve a pending request")
async def approve(approval_id: uuid.UUID, payload: DecisionIn, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    org = await db.get(Organization, approval.organization_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=400, detail=f"Approval is already {approval.status}")
    approval.status = "approved"
    approval.decided_by = tenant_context.user_id
    approval.decision_note = payload.note
    db.add(approval)
    await db.commit()
    await db.refresh(approval)
    return _approval_dict(approval)


@router.post("/approvals/{approval_id}/reject", summary="Reject a pending request")
async def reject(approval_id: uuid.UUID, payload: DecisionIn, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    approval = await db.get(Approval, approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    org = await db.get(Organization, approval.organization_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != "pending":
        raise HTTPException(status_code=400, detail=f"Approval is already {approval.status}")
    approval.status = "rejected"
    approval.decided_by = tenant_context.user_id
    approval.decision_note = payload.note
    db.add(approval)
    await db.commit()
    await db.refresh(approval)
    return _approval_dict(approval)
