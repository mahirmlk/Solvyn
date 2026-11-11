"""Repository connections, intelligence, and bounded GitHub writes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership, require_tenant
from solvyn.db.models import Integration, Project, Repository
from solvyn.db.session import get_session
from solvyn.repositories.credentials import (
    CredentialEncryptionUnavailableError,
    GitHubCredentialNotFoundError,
    load_organization_github_connection,
    load_organization_github_token,
)
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError
from solvyn.repositories.protocols import RepositoryProvider
from solvyn.repositories.providers import get_authorized_repository_provider, get_repository_provider

router = APIRouter(tags=["repositories", "repository intelligence"])


class RepositoryConnect(BaseModel):
    full_name: str = Field(..., min_length=3, max_length=255, description="GitHub owner/repository")


class BranchCreate(BaseModel):
    branch_name: str = Field(..., min_length=8, max_length=240)
    from_branch: str | None = Field(default=None, max_length=255)


class CommitFileInput(BaseModel):
    path: str = Field(..., min_length=1, max_length=512)
    content: str = Field(..., max_length=500_000)


class CommitCreate(BaseModel):
    branch: str = Field(..., min_length=8, max_length=240)
    expected_head_sha: str = Field(..., min_length=1, max_length=64)
    message: str = Field(..., min_length=1, max_length=2000)
    files: list[CommitFileInput] = Field(..., min_length=1, max_length=50)


class PullRequestCreate(BaseModel):
    head: str = Field(..., min_length=8, max_length=240)
    title: str = Field(..., min_length=1, max_length=256)
    body: str = Field(default="", max_length=20_000)


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


async def _load_github_write_context(
    repository_id: uuid.UUID, db: AsyncSession
) -> tuple[Repository, Project, str]:
    repository, project = await _load_authorized_repository(repository_id, db, roles=OWNER_ADMIN)
    if repository.provider != "github":
        raise HTTPException(status_code=422, detail="Repository provider does not support GitHub writes")
    try:
        token = await load_organization_github_token(db, project.organization_id)
    except GitHubCredentialNotFoundError as exc:
        raise HTTPException(status_code=409, detail="Connect a GitHub organization token before writing") from exc
    except CredentialEncryptionUnavailableError as exc:
        raise HTTPException(status_code=503, detail="GitHub credential cannot be decrypted with current configuration") from exc
    return repository, project, token


@asynccontextmanager
async def _authorized_provider(
    repository: Repository, project: Project, db: AsyncSession
) -> AsyncIterator[RepositoryProvider]:
    try:
        provider = await get_authorized_repository_provider(
            repository.provider, db, repository, project.organization_id
        )
    except GitHubCredentialNotFoundError as exc:
        raise HTTPException(status_code=409, detail="Reconnect the GitHub organization integration") from exc
    except CredentialEncryptionUnavailableError as exc:
        raise HTTPException(status_code=503, detail="GitHub credential cannot be decrypted with current configuration") from exc
    async with provider as client:
        yield client


def _provider_error(exc: GitHubRepositoryError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.post("/projects/{project_id}/repositories", status_code=201, summary="Connect a GitHub repository")
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

    token_integration = None
    token = None
    try:
        try:
            token_integration, token = await load_organization_github_connection(db, project.organization_id)
        except GitHubCredentialNotFoundError:
            pass
        if token:
            try:
                async with GitHubClient(token=token) as github:
                    metadata = await github.repository_metadata(payload.full_name)
            except GitHubRepositoryError as exc:
                if exc.status_code != 404:
                    raise
                async with get_repository_provider("github") as github:
                    metadata = await github.repository_metadata(payload.full_name)
        else:
            async with get_repository_provider("github") as github:
                metadata = await github.repository_metadata(payload.full_name)
    except CredentialEncryptionUnavailableError as exc:
        raise HTTPException(status_code=503, detail="GitHub credential cannot be decrypted with current configuration") from exc
    except GitHubRepositoryError as exc:
        if exc.status_code == 404 and token_integration is None:
            raise HTTPException(
                status_code=409,
                detail="Connect an organization GitHub token with repository access before connecting this repository",
            ) from exc
        raise _provider_error(exc) from exc

    if metadata["private"] and token_integration is None:
        raise HTTPException(
            status_code=409,
            detail="Connect an organization GitHub token with repository access before connecting this repository",
        )

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

    integration = token_integration if metadata["private"] else (
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
    repository, project = await _load_authorized_repository(repository_id, db)
    try:
        async with _authorized_provider(repository, project, db) as github:
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
    repository, project = await _load_authorized_repository(repository_id, db)
    try:
        async with _authorized_provider(repository, project, db) as github:
            paths = await github.search_paths(repository.full_name, repository.default_branch, query)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {"query": query, "paths": paths, "count": len(paths), "search_scope": "file_paths"}


@router.get("/repositories/{repository_id}/content/search", summary="Search bounded repository text content")
async def search_repository_content(
    repository_id: uuid.UUID,
    query: str = Query(..., min_length=1, max_length=200),
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, project = await _load_authorized_repository(repository_id, db)
    try:
        async with _authorized_provider(repository, project, db) as provider:
            return await provider.search_content(repository.full_name, repository.default_branch, query)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc


@router.get("/repositories/{repository_id}/branches", summary="List repository branches")
async def list_repository_branches(repository_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    repository, project = await _load_authorized_repository(repository_id, db)
    try:
        async with _authorized_provider(repository, project, db) as provider:
            branches = await provider.branches(repository.full_name)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {"branches": branches, "count": len(branches), "possibly_truncated": len(branches) == 100}


@router.post("/repositories/{repository_id}/branches", status_code=201, summary="Create a Solvyn work branch")
async def create_repository_branch(
    repository_id: uuid.UUID,
    payload: BranchCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, _, token = await _load_github_write_context(repository_id, db)
    source = payload.from_branch or repository.default_branch
    try:
        async with GitHubClient(token=token) as github:
            branch = await github.create_branch(repository.full_name, payload.branch_name, source)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {"name": branch["name"], "sha": branch["sha"]}


@router.post("/repositories/{repository_id}/commits", status_code=201, summary="Commit bounded text changes")
async def create_repository_commit(
    repository_id: uuid.UUID,
    payload: CommitCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, _, token = await _load_github_write_context(repository_id, db)
    files = [{"path": item.path, "content": item.content} for item in payload.files]
    try:
        async with GitHubClient(token=token) as github:
            commit = await github.commit_files(
                repository.full_name,
                payload.branch,
                expected_head_sha=payload.expected_head_sha,
                message=payload.message,
                files=files,
            )
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {"branch": commit["branch"], "sha": commit["sha"]}


@router.post("/repositories/{repository_id}/pull-requests", status_code=201, summary="Open a draft pull request")
async def create_repository_pull_request(
    repository_id: uuid.UUID,
    payload: PullRequestCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, _, token = await _load_github_write_context(repository_id, db)
    try:
        async with GitHubClient(token=token) as github:
            result = await github.create_pull_request(
                repository.full_name,
                title=payload.title,
                body=payload.body,
                head=payload.head,
                base=repository.default_branch,
            )
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return result


@router.get("/repositories/{repository_id}/pull-requests", summary="Read live pull requests")
async def list_repository_pull_requests(
    repository_id: uuid.UUID,
    state: Literal["open", "closed", "all"] = "open",
    limit: int = Query(default=50, ge=1, le=100),
    page: int = Query(default=1, ge=1, le=100),
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, project = await _load_authorized_repository(repository_id, db)
    try:
        async with _authorized_provider(repository, project, db) as provider:
            pull_requests = await provider.pull_requests(repository.full_name, state=state, limit=limit, page=page)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return {
        "pull_requests": pull_requests,
        "count": len(pull_requests),
        "state": state,
        "page": page,
        "limit": limit,
        "synchronization": "live_github_snapshot",
        "content_trust": "untrusted_repository_data",
    }


@router.get("/repositories/{repository_id}/pull-requests/{number}", summary="Inspect a pull request diff and review status")
async def inspect_repository_pull_request(
    repository_id: uuid.UUID,
    number: int,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    repository, project = await _load_authorized_repository(repository_id, db)
    try:
        async with _authorized_provider(repository, project, db) as provider:
            pull_request = await provider.pull_request_detail(repository.full_name, number)
    except GitHubRepositoryError as exc:
        raise _provider_error(exc) from exc
    return pull_request
