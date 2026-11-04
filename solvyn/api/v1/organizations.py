"""Organizations, workspaces, and projects.

All reads/writes are scoped to the authenticated tenant + organization
membership. An organization maps onto a Tenant (the isolation boundary);
projects may optionally live inside a workspace.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Organization, OrganizationMembership, Project, Workspace
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["organizations", "workspaces", "projects"])


def _require_tenant() -> uuid.UUID:
    if not tenant_context.is_authenticated:
        raise HTTPException(status_code=401, detail="Authentication required")
    if tenant_context.tenant_id is None:
        raise HTTPException(status_code=401, detail="No tenant context")
    return tenant_context.tenant_id


async def _membership_role(db: AsyncSession, org_id: uuid.UUID) -> str | None:
    user_id = tenant_context.user_id
    if user_id is None:
        return None
    result = await db.execute(
        select(OrganizationMembership).where(
            OrganizationMembership.user_id == user_id,
            OrganizationMembership.organization_id == org_id,
        )
    )
    row = result.scalar_one_or_none()
    return row.role if row else None


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


class OrganizationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")


class OrganizationOut(BaseModel):
    id: str
    name: str
    slug: str
    tenant_id: str
    is_active: bool
    created_at: str


def _org_dict(org: Organization) -> dict[str, object]:
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "tenant_id": str(org.tenant_id),
        "is_active": bool(org.is_active),
        "created_at": org.created_at.isoformat() if org.created_at else "",
    }


@router.get("/organizations", summary="List organizations visible to this tenant")
async def list_organizations(db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    result = await db.execute(select(Organization).where(Organization.tenant_id == tenant_id).order_by(Organization.created_at))
    items = [_org_dict(o) for o in result.scalars().all()]
    return {"organizations": items, "count": len(items)}


@router.post("/organizations", status_code=201, summary="Create an organization")
async def create_organization(payload: OrganizationCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    existing = (await db.execute(select(Organization).where(Organization.slug == payload.slug))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Organization with slug '{payload.slug}' already exists")
    org = Organization(name=payload.name, slug=payload.slug, tenant_id=tenant_id)
    db.add(org)
    await db.flush()
    if tenant_context.user_id is not None:
        db.add(OrganizationMembership(user_id=tenant_context.user_id, organization_id=org.id, role="owner"))
    await db.commit()
    await db.refresh(org)
    return _org_dict(org)


@router.get("/organizations/{org_id}", summary="Get an organization")
async def get_organization(org_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    return _org_dict(org)


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


class WorkspaceCreate(BaseModel):
    organization_id: str
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")


class WorkspaceOut(BaseModel):
    id: str
    organization_id: str
    name: str
    slug: str
    created_at: str


def _ws_dict(ws: Workspace) -> dict[str, object]:
    return {
        "id": str(ws.id),
        "organization_id": str(ws.organization_id),
        "name": ws.name,
        "slug": ws.slug,
        "created_at": ws.created_at.isoformat() if ws.created_at else "",
    }


@router.get("/workspaces", summary="List workspaces in this tenant")
async def list_workspaces(db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    result = await db.execute(
        select(Workspace)
        .join(Organization, Workspace.organization_id == Organization.id)
        .where(Organization.tenant_id == tenant_id)
        .order_by(Workspace.created_at)
    )
    items = [_ws_dict(w) for w in result.scalars().all()]
    return {"workspaces": items, "count": len(items)}


@router.post("/workspaces", status_code=201, summary="Create a workspace")
async def create_workspace(payload: WorkspaceCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org_id = uuid.UUID(payload.organization_id)
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    ws = Workspace(organization_id=org_id, name=payload.name, slug=payload.slug)
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    return _ws_dict(ws)


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    organization_id: str
    workspace_id: str | None = None
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = ""
    instructions: str = ""


class ProjectOut(BaseModel):
    id: str
    organization_id: str
    workspace_id: str | None
    name: str
    slug: str
    description: str
    instructions: str
    is_active: bool
    created_at: str


def _project_dict(p: Project) -> dict[str, object]:
    return {
        "id": str(p.id),
        "organization_id": str(p.organization_id),
        "workspace_id": str(p.workspace_id) if p.workspace_id else None,
        "name": p.name,
        "slug": p.slug,
        "description": p.description or "",
        "instructions": p.instructions or "",
        "is_active": bool(p.is_active),
        "created_at": p.created_at.isoformat() if p.created_at else "",
    }


@router.get("/organizations/{org_id}/projects", summary="List projects in an organization")
async def list_projects(org_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    result = await db.execute(select(Project).where(Project.organization_id == org_id).order_by(Project.created_at))
    items = [_project_dict(p) for p in result.scalars().all()]
    return {"projects": items, "count": len(items)}


@router.post("/projects", status_code=201, summary="Create a project")
async def create_project(payload: ProjectCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    org_id = uuid.UUID(payload.organization_id)
    org = await db.get(Organization, org_id)
    if org is None or org.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Organization not found")
    duplicate = (
        await db.execute(select(Project).where(Project.organization_id == org_id, Project.slug == payload.slug))
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"Project '{payload.slug}' already exists in this organization")
    workspace_id = uuid.UUID(payload.workspace_id) if payload.workspace_id else None
    if workspace_id is not None:
        ws = await db.get(Workspace, workspace_id)
        if ws is None or ws.organization_id != org_id:
            raise HTTPException(status_code=400, detail="Workspace does not belong to this organization")
    project = Project(
        organization_id=org_id,
        workspace_id=workspace_id,
        tenant_id=tenant_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        instructions=payload.instructions,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_dict(project)


@router.get("/projects/{project_id}", summary="Get a project")
async def get_project(project_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return _project_dict(project)


@router.patch("/projects/{project_id}", summary="Update a project")
async def update_project(project_id: uuid.UUID, payload: ProjectCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = _require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    project.name = payload.name
    project.description = payload.description
    project.instructions = payload.instructions
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _project_dict(project)
