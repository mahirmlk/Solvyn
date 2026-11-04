"""Persistent AI teammates (teammate_profiles).

Teammates are durable entities with a role, responsibilities, system
instructions, model overrides, and explicit least-privilege grants.
Tool permissions are enforced by the tool gateway at execution time —
never by system prompts alone (see Phase 3 sandbox-execution).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import (
    Organization,
    PermissionLevel,
    Project,
    ProjectKnowledge,
    TeammatePermission,
    TeammateProfile,
    TeammateProjectAssignment,
)
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["teammates"])

# Least-privilege defaults per role. Writers grant the minimum needed to do
# the job inside an isolated workspace; merge/deploy always require approval.
_DEFAULT_PERMISSIONS: dict[str, list[str]] = {
    "Tech Lead": [PermissionLevel.READ_ONLY.value, PermissionLevel.PROPOSE_CHANGES.value],
    "Software Engineer": [
        PermissionLevel.READ_ONLY.value,
        PermissionLevel.PROPOSE_CHANGES.value,
        PermissionLevel.MODIFY_FILES.value,
        PermissionLevel.EXECUTE_COMMANDS.value,
        PermissionLevel.CREATE_BRANCH.value,
        PermissionLevel.CREATE_PULL_REQUEST.value,
    ],
    "QA Engineer": [
        PermissionLevel.READ_ONLY.value,
        PermissionLevel.EXECUTE_COMMANDS.value,
    ],
    "Security Engineer": [PermissionLevel.READ_ONLY.value],
}

_VALID_PERMISSIONS = {p.value for p in PermissionLevel}


def _require_tenant() -> uuid.UUID:
    if not tenant_context.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    if tenant_context.tenant_id is None:
        raise HTTPException(status_code=401, detail="No tenant context")
    return tenant_context.tenant_id


async def _load_teammate(db: AsyncSession, teammate_id: uuid.UUID, tenant_id: uuid.UUID) -> TeammateProfile:
    teammate = await db.get(TeammateProfile, teammate_id)
    if teammate is None or teammate.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Teammate not found")
    return teammate


def _teammate_dict(t: TeammateProfile) -> dict[str, object]:
    return {
        "id": str(t.id),
        "organization_id": str(t.organization_id),
        "name": t.name,
        "role": t.role,
        "description": t.description or "",
        "responsibilities": t.responsibilities or "",
        "agent_type": t.agent_type,
        "is_enabled": bool(t.is_enabled),
        "status": t.status,
        "model_provider": t.model_provider,
        "model_name": t.model_name,
        "max_tokens_per_run": t.max_tokens_per_run,
        "max_run_seconds": t.max_run_seconds,
        "created_at": t.created_at.isoformat() if t.created_at else "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else "",
    }


class TeammateCreate(BaseModel):
    organization_id: str
    name: str = Field(..., min_length=1, max_length=255)
    role: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    responsibilities: str = ""
    system_instructions: str = ""
    agent_type: str = Field(default="backend-engineer", max_length=100)
    model_provider: str | None = None
    model_name: str | None = None
    max_tokens_per_run: int | None = Field(default=None, ge=1)
    max_run_seconds: int | None = Field(default=None, ge=1)
    permissions: list[str] | None = None
    project_ids: list[str] | None = None


class TeammateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    responsibilities: str | None = None
    system_instructions: str | None = None
    is_enabled: bool | None = None
    model_provider: str | None = None
    model_name: str | None = None
    max_tokens_per_run: int | None = None
    max_run_seconds: int | None = None
    permissions: list[str] | None = None
    project_ids: list[str] | None = None


@router.get("/organizations/{org_id}/teammates", summary="List teammates in an organization")
async def list_teammates(org_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    result = await db.execute(select(TeammateProfile).where(TeammateProfile.organization_id == org_id).order_by(TeammateProfile.name))
    items = [_teammate_dict(t) for t in result.scalars().all()]
    return {"teammates": items, "count": len(items)}


@router.post("/teammates", status_code=201, summary="Create a persistent teammate")
async def create_teammate(payload: TeammateCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org_id = uuid.UUID(payload.organization_id)
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    duplicate = (
        await db.execute(
            select(TeammateProfile).where(TeammateProfile.organization_id == org_id, TeammateProfile.name == payload.name)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"Teammate '{payload.name}' already exists in this organization")

    teammate = TeammateProfile(
        organization_id=org_id,
        tenant_id=tenant_id,
        name=payload.name,
        role=payload.role,
        description=payload.description,
        responsibilities=payload.responsibilities,
        system_instructions=payload.system_instructions,
        agent_type=payload.agent_type,
        model_provider=payload.model_provider,
        model_name=payload.model_name,
        max_tokens_per_run=payload.max_tokens_per_run,
        max_run_seconds=payload.max_run_seconds,
    )
    db.add(teammate)
    await db.flush()

    granted = payload.permissions if payload.permissions is not None else _DEFAULT_PERMISSIONS.get(payload.role, [PermissionLevel.READ_ONLY.value])
    for perm in granted:
        if perm not in _VALID_PERMISSIONS:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {perm}")
        db.add(TeammatePermission(teammate_id=teammate.id, permission=perm, granted=True))

    for project_id_str in payload.project_ids or []:
        project = await db.get(Project, uuid.UUID(project_id_str))
        if project is None or project.tenant_id != tenant_id:
            raise HTTPException(status_code=400, detail=f"Project {project_id_str} is not visible to this tenant")
        db.add(TeammateProjectAssignment(teammate_id=teammate.id, project_id=project.id))

    await db.commit()
    await db.refresh(teammate)
    return _teammate_dict(teammate)


@router.get("/teammates/{teammate_id}", summary="Get a teammate with permissions")
async def get_teammate(teammate_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    perms = (await db.execute(select(TeammatePermission).where(TeammatePermission.teammate_id == teammate_id))).scalars().all()
    out = _teammate_dict(teammate)
    out["permissions"] = [p.permission for p in perms if p.granted]
    return out


@router.patch("/teammates/{teammate_id}", summary="Update a teammate")
async def update_teammate(teammate_id: uuid.UUID, payload: TeammateUpdate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    data = payload.model_dump(exclude_unset=True, exclude={"permissions", "project_ids"})
    for field_name, value in data.items():
        if value is not None:
            setattr(teammate, field_name, value)
    if payload.permissions is not None:
        existing = (await db.execute(select(TeammatePermission).where(TeammatePermission.teammate_id == teammate_id))).scalars().all()
        for row in existing:
            await db.delete(row)
        for perm in payload.permissions:
            if perm not in _VALID_PERMISSIONS:
                raise HTTPException(status_code=400, detail=f"Unknown permission: {perm}")
            db.add(TeammatePermission(teammate_id=teammate.id, permission=perm, granted=True))
    if payload.project_ids is not None:
        existing = (await db.execute(select(TeammateProjectAssignment).where(TeammateProjectAssignment.teammate_id == teammate_id))).scalars().all()
        for row in existing:
            await db.delete(row)
        for project_id_str in payload.project_ids:
            project = await db.get(Project, uuid.UUID(project_id_str))
            if project is None or project.tenant_id != tenant_id:
                raise HTTPException(status_code=400, detail=f"Project {project_id_str} is not visible to this tenant")
            db.add(TeammateProjectAssignment(teammate_id=teammate.id, project_id=project.id))
    db.add(teammate)
    await db.commit()
    await db.refresh(teammate)
    return _teammate_dict(teammate)


@router.delete("/teammates/{teammate_id}", summary="Disable a teammate (soft delete)")
async def disable_teammate(teammate_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    teammate.is_enabled = False
    db.add(teammate)
    await db.commit()
    return {"ok": True, "teammate_id": str(teammate_id), "is_enabled": False}


# ---------------------------------------------------------------------------
# Project knowledge (validated, scoped — never stores secrets)
# ---------------------------------------------------------------------------


class KnowledgeIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=50)
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1)


_SECRET_PATTERNS = ("sk-", "xox", "AKIA", "BEGIN PRIVATE KEY", "password =", "api_key =", "secret =", "ghp_")


def _looks_like_secret(value: str) -> str | None:
    lowered = value.lower()
    for pat in _SECRET_PATTERNS:
        if pat.lower() in lowered:
            return pat
    return None


@router.get("/projects/{project_id}/knowledge", summary="List project knowledge")
async def list_knowledge(project_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    result = await db.execute(
        select(ProjectKnowledge)
        .where(ProjectKnowledge.project_id == project_id)
        .order_by(ProjectKnowledge.category, ProjectKnowledge.key)
    )
    items = [
        {"id": str(k.id), "category": k.category, "key": k.key, "value": k.value, "updated_at": k.updated_at.isoformat() if k.updated_at else ""}
        for k in result.scalars().all()
    ]
    return {"knowledge": items, "count": len(items)}


@router.put("/projects/{project_id}/knowledge", summary="Upsert a project knowledge entry")
async def upsert_knowledge(project_id: uuid.UUID, payload: KnowledgeIn, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    hit = _looks_like_secret(payload.value)
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to store possible secret material (matched '{hit}'). Knowledge records must never contain credentials.",
        )
    existing = (
        await db.execute(
            select(ProjectKnowledge).where(
                ProjectKnowledge.project_id == project_id,
                ProjectKnowledge.category == payload.category,
                ProjectKnowledge.key == payload.key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.value = payload.value
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        row = existing
    else:
        row = ProjectKnowledge(project_id=project_id, category=payload.category, key=payload.key, value=payload.value, created_by=tenant_context.user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return {"id": str(row.id), "category": row.category, "key": row.key, "updated_at": row.updated_at.isoformat() if row.updated_at else ""}
