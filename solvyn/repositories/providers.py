"""Factory for repository providers."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Integration, Repository
from solvyn.repositories.credentials import (
    GitHubCredentialNotFoundError,
    load_organization_github_token,
)
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError
from solvyn.repositories.protocols import RepositoryProvider


def get_repository_provider(name: str, *, token: str | None = None) -> RepositoryProvider:
    if name == "github":
        return GitHubClient(token=token)
    raise GitHubRepositoryError(501, f"Repository provider '{name}' is not supported")


async def get_authorized_repository_provider(
    name: str,
    db: AsyncSession,
    repository: Repository,
    organization_id: uuid.UUID,
) -> RepositoryProvider:
    """Build a GitHub client using only the credential explicitly linked to this repository."""
    if name != "github":
        return get_repository_provider(name)
    if repository.integration_id is None:
        return get_repository_provider(name)
    integration = await db.get(Integration, repository.integration_id)
    if (
        integration is None
        or integration.organization_id != organization_id
        or integration.provider != "github"
        or not integration.is_active
    ):
        raise GitHubCredentialNotFoundError("Repository credential is unavailable")
    if integration.external_id != "organization-token":
        return get_repository_provider(name)
    token = await load_organization_github_token(db, organization_id)
    return get_repository_provider(name, token=token)
