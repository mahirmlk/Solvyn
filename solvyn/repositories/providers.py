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


def require_github_app_repository_binding(
    integration: Integration | None,
    repository: Repository,
    organization_id: uuid.UUID,
) -> tuple[int, int]:
    """Return the verified installation/repository IDs or fail closed."""
    installation_id = integration.github_installation_id if integration is not None else None
    repository_id = getattr(repository, "github_repository_id", None)
    if (
        integration is None
        or integration.organization_id != organization_id
        or integration.provider != "github"
        or not integration.is_active
        or not isinstance(installation_id, int)
        or installation_id < 1
        or not isinstance(repository_id, int)
        or repository_id < 1
    ):
        raise GitHubCredentialNotFoundError(
            "Repository is not bound to an active GitHub App installation for this organization"
        )
    return installation_id, repository_id


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
        installation_id, repository_id = require_github_app_repository_binding(
            integration, repository, organization_id
        )
        async with GitHubAppClient() as app:
            token = await app.installation_token(
                installation_id,
                (repository_id,),
                permissions,
            )
        return get_repository_provider(name, token=token)
    if integration.external_id == "public-api":
        return get_repository_provider(name)
    raise GitHubCredentialNotFoundError(
        "Repository is not bound to a GitHub App installation; reconnect it with installation-scoped access"
    )
