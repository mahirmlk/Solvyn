"""Factory for repository providers."""

from __future__ import annotations

from solvyn.repositories.github import GitHubClient, GitHubRepositoryError
from solvyn.repositories.protocols import RepositoryProvider


def get_repository_provider(name: str) -> RepositoryProvider:
    if name == "github":
        return GitHubClient()
    raise GitHubRepositoryError(501, f"Repository provider '{name}' is not supported")
