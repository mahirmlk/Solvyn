"""Factory for repository providers."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Integration, Repository
from solvyn.repositories.credentials import (
    GitHubCredentialNotFoundError,
)
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError
from solvyn.repositories.github_app import GitHubAppClient
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
    *,
    permissions: dict[str, str] | None = None,
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
    if integration.github_installation_id is not None:
        if repository.github_repository_id is None:
            raise GitHubCredentialNotFoundError("Repository is missing its GitHub numeric identity; reconnect it")
        async with GitHubAppClient() as app:
            token = await app.installation_token(
                integration.github_installation_id,
                (repository.github_repository_id,),
                permissions,
            )
        return get_repository_provider(name, token=token)
    if integration.external_id == "public-api":
        return get_repository_provider(name)
    raise GitHubCredentialNotFoundError(
        "Repository is not bound to a GitHub App installation; reconnect it with installation-scoped access"
    )
