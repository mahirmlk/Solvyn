"""Persistent AI teammate profiles and assigned-project runtime endpoint.

The current teammate run path only invokes an LLM-backed registered agent.
Teammate permissions are loaded as runtime context, but tool dispatch is not
available through this endpoint until permission-enforced dispatch is wired.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.teammate_runtime import (
    ROLE_AGENT_TYPES,
    TeammateRunConflictError,
    TeammateRunDeniedError,
    run_teammate,
    validate_role_agent_type,
)
from solvyn.api.v1._rbac import ANY_ROLE, MEMBER_OR_ABOVE, OWNER_ADMIN, require_org_membership, require_tenant
from solvyn.db.models import (
    PermissionLevel,
    Project,
    ProjectKnowledge,
    TeammateMemory,
    TeammatePermission,
    TeammateProfile,
    TeammateProjectAssignment,
    TeammateStatus,
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
    organization_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=255)
    role: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    responsibilities: str = ""
    system_instructions: str = ""
    agent_type: str | None = Field(default=None, max_length=100)
    model_provider: str | None = None
    model_name: str | None = None
    max_tokens_per_run: int | None = Field(default=None, ge=1, le=16_000)
    max_run_seconds: int | None = Field(default=None, ge=1, le=900)
    permissions: list[str] | None = None
    project_ids: list[uuid.UUID] | None = None


class TeammateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    responsibilities: str | None = None
    system_instructions: str | None = None
    is_enabled: bool | None = None
    model_provider: str | None = None
    model_name: str | None = None
    max_tokens_per_run: int | None = Field(default=None, ge=1, le=16_000)
    max_run_seconds: int | None = Field(default=None, ge=1, le=900)
    permissions: list[str] | None = None
    project_ids: list[uuid.UUID] | None = None


class TeammateRun(BaseModel):
    project_id: uuid.UUID
    input_data: dict[str, object] = Field(default_factory=dict)


class TeammateMemoryIn(BaseModel):
    project_id: uuid.UUID
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=10_000)


async def _assigned_project(
    db: AsyncSession,
    teammate: TeammateProfile,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Project:
    result = await db.execute(
        select(Project)
        .join(TeammateProjectAssignment, TeammateProjectAssignment.project_id == Project.id)
        .where(
            TeammateProjectAssignment.teammate_id == teammate.id,
            Project.id == project_id,
            Project.tenant_id == tenant_id,
            Project.organization_id == teammate.organization_id,
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Assigned project not found")
    return project


@router.get("/organizations/{org_id}/teammates", summary="List teammates in an organization")
async def list_teammates(org_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    await require_org_membership(db, org_id, roles=ANY_ROLE)
    result = await db.execute(select(TeammateProfile).where(TeammateProfile.organization_id == org_id).order_by(TeammateProfile.name))
    items = [_teammate_dict(t) for t in result.scalars().all()]
    return {"teammates": items, "count": len(items)}


@router.post("/teammates", status_code=201, summary="Create a persistent teammate")
async def create_teammate(payload: TeammateCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    await require_org_membership(db, payload.organization_id, roles=OWNER_ADMIN)
    org_id = payload.organization_id

    agent_type = payload.agent_type or ROLE_AGENT_TYPES.get(payload.role)
    if agent_type is None:
        raise HTTPException(status_code=400, detail=f"Unsupported teammate role: {payload.role}")
    try:
        validate_role_agent_type(payload.role, agent_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    duplicate = (
        await db.execute(
            select(TeammateProfile).where(TeammateProfile.organization_id == org_id, TeammateProfile.name == payload.name)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"Teammate '{payload.name}' already exists in this organization")

    granted = payload.permissions if payload.permissions is not None else _DEFAULT_PERMISSIONS.get(payload.role, [PermissionLevel.READ_ONLY.value])
    unknown = [p for p in granted if p not in _VALID_PERMISSIONS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}")

    # Verify project visibility before persisting anything.
    project_rows = []
    for project_id in payload.project_ids or []:
        project = await db.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id or project.organization_id != org_id:
            raise HTTPException(status_code=400, detail=f"Project {project_id} is not visible to this tenant")
        project_rows.append(project)

    teammate = TeammateProfile(
        organization_id=org_id,
        tenant_id=tenant_id,
        name=payload.name,
        role=payload.role,
        description=payload.description,
        responsibilities=payload.responsibilities,
        system_instructions=payload.system_instructions,
        agent_type=agent_type,
        model_provider=payload.model_provider,
        model_name=payload.model_name,
        max_tokens_per_run=payload.max_tokens_per_run,
        max_run_seconds=payload.max_run_seconds,
    )
    db.add(teammate)
    await db.flush()

    for perm in granted:
        db.add(TeammatePermission(teammate_id=teammate.id, permission=perm, granted=True))
    for project in project_rows:
        db.add(TeammateProjectAssignment(teammate_id=teammate.id, project_id=project.id))

    await db.commit()
    await db.refresh(teammate)
    return _teammate_dict(teammate)


@router.get("/teammates/{teammate_id}", summary="Get a teammate with permissions")
async def get_teammate(teammate_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=ANY_ROLE)
    perms = (await db.execute(select(TeammatePermission).where(TeammatePermission.teammate_id == teammate_id))).scalars().all()
    out = _teammate_dict(teammate)
    out["permissions"] = [p.permission for p in perms if p.granted]
    return out


@router.patch("/teammates/{teammate_id}", summary="Update a teammate")
async def update_teammate(teammate_id: uuid.UUID, payload: TeammateUpdate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)

    if payload.permissions is not None:
        unknown = [p for p in payload.permissions if p not in _VALID_PERMISSIONS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}")
    project_rows: list[Project] = []
    for project_id in payload.project_ids or []:
        project = await db.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id or project.organization_id != teammate.organization_id:
            raise HTTPException(status_code=400, detail=f"Project {project_id} is not visible to this tenant")
        project_rows.append(project)

    data = payload.model_dump(exclude_unset=True, exclude={"permissions", "project_ids"})
    for field_name, value in data.items():
        if value is not None:
            setattr(teammate, field_name, value)
    if payload.is_enabled is True and teammate.status == TeammateStatus.DISABLED:
        teammate.status = TeammateStatus.IDLE
    elif payload.is_enabled is False and teammate.status != TeammateStatus.BUSY:
        teammate.status = TeammateStatus.DISABLED
    if payload.permissions is not None:
        existing_permissions = (await db.execute(select(TeammatePermission).where(TeammatePermission.teammate_id == teammate_id))).scalars().all()
        for permission_row in existing_permissions:
            await db.delete(permission_row)
        for perm in payload.permissions:
            db.add(TeammatePermission(teammate_id=teammate.id, permission=perm, granted=True))
    if payload.project_ids is not None:
        existing_assignments = (await db.execute(select(TeammateProjectAssignment).where(TeammateProjectAssignment.teammate_id == teammate_id))).scalars().all()
        for assignment_row in existing_assignments:
            await db.delete(assignment_row)
        for project in project_rows:
            db.add(TeammateProjectAssignment(teammate_id=teammate.id, project_id=project.id))
    db.add(teammate)
    await db.commit()
    await db.refresh(teammate)
    return _teammate_dict(teammate)


@router.delete("/teammates/{teammate_id}", summary="Disable a teammate (soft delete)")
async def disable_teammate(teammate_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)
    teammate.is_enabled = False
    if teammate.status != TeammateStatus.BUSY:
        teammate.status = TeammateStatus.DISABLED
    db.add(teammate)
    await db.commit()
    return {"ok": True, "teammate_id": str(teammate_id), "is_enabled": False}


@router.post("/teammates/{teammate_id}/run", summary="Run a teammate on an assigned project")
async def execute_teammate(
    teammate_id: uuid.UUID,
    payload: TeammateRun,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    try:
        output = await run_teammate(
            db,
            teammate,
            tenant_id=tenant_id,
            project_id=payload.project_id,
            input_data=payload.input_data,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TeammateRunDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TeammateRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Teammate run exceeded its time limit") from exc
    return {"teammate_id": str(teammate_id), "project_id": str(payload.project_id), "output": output}


@router.get("/teammates/{teammate_id}/memory", summary="List persistent teammate memory")
async def list_teammate_memory(
    teammate_id: uuid.UUID,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    await _assigned_project(db, teammate, project_id, tenant_id)
    result = await db.execute(
        select(TeammateMemory)
        .where(
            TeammateMemory.tenant_id == tenant_id,
            TeammateMemory.teammate_id == teammate_id,
            TeammateMemory.project_id == project_id,
        )
        .order_by(TeammateMemory.key)
    )
    entries = [
        {"key": row.key, "value": row.value, "updated_at": row.updated_at.isoformat() if row.updated_at else ""}
        for row in result.scalars().all()
    ]
    return {"teammate_id": str(teammate_id), "project_id": str(project_id), "memory": entries, "count": len(entries)}


@router.put("/teammates/{teammate_id}/memory", summary="Store persistent teammate memory")
async def upsert_teammate_memory(
    teammate_id: uuid.UUID,
    payload: TeammateMemoryIn,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)
    await _assigned_project(db, teammate, payload.project_id, tenant_id)
    secret_match = _looks_like_secret(f"{payload.key}\n{payload.value}")
    if secret_match is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to store possible secret material ({secret_match}). Teammate memory must not contain credentials.",
        )
    existing = (
        await db.execute(
            select(TeammateMemory).where(
                TeammateMemory.tenant_id == tenant_id,
                TeammateMemory.teammate_id == teammate_id,
                TeammateMemory.project_id == payload.project_id,
                TeammateMemory.key == payload.key,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = TeammateMemory(
            tenant_id=tenant_id,
            teammate_id=teammate_id,
            project_id=payload.project_id,
            key=payload.key,
            value=payload.value,
        )
        db.add(existing)
    else:
        existing.value = payload.value
        db.add(existing)
    await db.commit()
    await db.refresh(existing)
    return {
        "teammate_id": str(teammate_id),
        "project_id": str(payload.project_id),
        "key": existing.key,
        "updated_at": existing.updated_at.isoformat() if existing.updated_at else "",
    }


@router.delete("/teammates/{teammate_id}/memory/{key}", summary="Delete persistent teammate memory")
async def delete_teammate_memory(
    teammate_id: uuid.UUID,
    key: str,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)
    await _assigned_project(db, teammate, project_id, tenant_id)
    record = (
        await db.execute(
            select(TeammateMemory).where(
                TeammateMemory.tenant_id == tenant_id,
                TeammateMemory.teammate_id == teammate_id,
                TeammateMemory.project_id == project_id,
                TeammateMemory.key == key,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Teammate memory not found")
    await db.delete(record)
    await db.commit()
    return {"ok": True, "teammate_id": str(teammate_id), "project_id": str(project_id), "key": key}


# ---------------------------------------------------------------------------
# Project knowledge (validated, scoped — never stores secrets)
# ---------------------------------------------------------------------------


class KnowledgeIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=50)
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1)


_SECRET_PATTERNS = (
    ("AWS access key pattern", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key pattern", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token pattern", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("Slack token pattern", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("credential assignment pattern", re.compile(r"(?:password|api_key|secret|client_secret)\s*[:=]\s*\S+", re.I)),
    ("API token pattern", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
)


def _looks_like_secret(value: str) -> str | None:
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(value) is not None:
            return label
    return None


@router.get("/projects/{project_id}/knowledge", summary="List project knowledge")
async def list_knowledge(project_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_org_membership(db, project.organization_id, roles=ANY_ROLE)
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
    tenant_id = require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_org_membership(db, project.organization_id, roles=OWNER_ADMIN)
    hit = _looks_like_secret(f"{payload.key}\n{payload.value}")
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to store possible secret material ({hit}). Knowledge records must never contain credentials.",
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
