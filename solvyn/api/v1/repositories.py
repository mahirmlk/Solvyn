"""Repository connections and read-only codebase intelligence."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership, require_tenant
from solvyn.db.models import Integration, Project, Repository
from solvyn.db.session import get_session
from solvyn.repositories.github import GitHubRepositoryError
from solvyn.repositories.providers import get_repository_provider

router = APIRouter(tags=["repositories", "repository intelligence"])


class RepositoryConnect(BaseModel):
    full_name: str = Field(..., min_length=3, max_length=255, description="GitHub owner/repository")


def _repository_dict(repository: Repository) -> dict[str, object]:
    return {
        "id": str(repository.id),
        "project_id": str(repository.project_id),
        "integration_id": str(repository.integration_id) if repository.integration_id else None,
        "full_name": repository.full_name,
        "provider": repository.provider,
        "default_branch": repository.default_branch,
        "url": repository.url,
        "is_active": bool(repository.is_active),
        "created_at": repository.created_at.isoformat() if repository.created_at else "",
    }


async def _load_authorized_repository(
    repository_id: uuid.UUID,
    db: AsyncSession,
    *,
    roles: tuple[str, ...] = ANY_ROLE,
) -> tuple[Repository, Project]:
    tenant_id = require_tenant()
    repository = await db.get(Repository, repository_id)
    if repository is None or not repository.is_active:
        raise HTTPException(status_code=404, detail="Repository not found")
    project = await db.get(Project, repository.project_id)
    if project is None or project.tenant_id != tenant_id or not project.is_active:
        raise HTTPException(status_code=404, detail="Repository not found")
    await require_org_membership(db, project.organization_id, roles=roles)
    return repository, project


def _provider_error(exc: GitHubRepositoryError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post("/projects/{project_id}/repositories", status_code=201, summary="Connect a public GitHub repository")
async def connect_repository(
    project_id: uuid.UUID,
    payload: RepositoryConnect,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_org_membership(db, project.organization_id, roles=OWNER_ADMIN)

    try:
        async with get_repository_provider("github") as github:
            metadata = await github.repository_metadata(payload.full_name)
        if metadata["private"]:
            raise HTTPException(status_code=422, detail="Only public GitHub repositories are supported right now")
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc

    duplicate = (
        await db.execute(
            select(Repository).where(
                Repository.project_id == project_id,
                Repository.full_name == metadata["full_name"],
            )
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="Repository is already connected to this project")

    integration = (
        await db.execute(
            select(Integration).where(
                Integration.organization_id == project.organization_id,
                Integration.provider == "github",
                Integration.external_id == "public-api",
                Integration.is_active == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if integration is None:
        integration = Integration(
            organization_id=project.organization_id,
            provider="github",
            external_id="public-api",
            name="Public GitHub API",
        )
        db.add(integration)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Repository connection conflicted; retry the request",
            ) from exc

    repository = Repository(
        project_id=project_id,
        integration_id=integration.id,
        full_name=metadata["full_name"],
        provider="github",
        default_branch=metadata["default_branch"],
        url=metadata["url"],
    )
    db.add(repository)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        duplicate_id = (
            await db.execute(
                select(Repository.id).where(
                    Repository.project_id == project_id,
                    Repository.full_name == metadata["full_name"],
                )
            )
        ).scalar_one_or_none()
        if duplicate_id is not None:
            raise HTTPException(status_code=409, detail="Repository is already connected to this project") from exc
        raise HTTPException(status_code=409, detail="Repository connection conflicted; retry the request") from exc
    await db.refresh(repository)
    return _repository_dict(repository)


@router.get("/projects/{project_id}/repositories", summary="List project repositories")
async def list_repositories(project_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id or not project.is_active:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_org_membership(db, project.organization_id, roles=ANY_ROLE)
    result = await db.execute(
        select(Repository)
        .where(Repository.project_id == project_id, Repository.is_active == True)  # noqa: E712
        .order_by(Repository.created_at)
    )
    items = [_repository_dict(repository) for repository in result.scalars().all()]
    return {"repositories": items, "count": len(items)}


@router.get("/repositories/{repository_id}/inspect", summary="Inspect repository structure and manifests")
async def inspect_repository(repository_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    repository, _ = await _load_authorized_repository(repository_id, db)
    try:
        async with get_repository_provider(repository.provider) as github:
            inspection = await github.inspect(repository.full_name)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    dependencies = github.parse_dependencies(inspection["manifests"])
    inspection["dependencies"] = dependencies
    inspection["project_context"] = {
        "description": inspection["repository"]["description"],
        "default_branch": inspection["repository"]["default_branch"],
        "top_level_entries": inspection["top_level_entries"],
        "languages": inspection["languages"],
        "frameworks": github.detect_frameworks(dependencies),
        "readme_excerpt": inspection["manifests"].get("README.md", "")[:2000],
    }
    return inspection


@router.get("/repositories/{repository_id}/files/search", summary="Search repository file paths")
async def search_repository_paths(
    repository_id: uuid.UUID,
    query: str = Query(..., min_length=1, max_length=100),
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, _ = await _load_authorized_repository(repository_id, db)
    try:
        async with get_repository_provider(repository.provider) as github:
            paths = await github.search_paths(repository.full_name, repository.default_branch, query)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {"query": query, "paths": paths, "count": len(paths), "search_scope": "file_paths"}


@router.get("/repositories/{repository_id}/branches", summary="List repository branches")
async def list_repository_branches(repository_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    repository, _ = await _load_authorized_repository(repository_id, db)
    try:
        async with get_repository_provider(repository.provider) as provider:
            branches = await provider.branches(repository.full_name)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {"branches": branches, "count": len(branches), "possibly_truncated": len(branches) == 100}
