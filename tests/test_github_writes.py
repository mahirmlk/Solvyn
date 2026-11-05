"""Boundaries and GitHub request shapes for repository write operations."""

from __future__ import annotations

import uuid
from typing import cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from solvyn.api.v1 import repositories as repository_api
from solvyn.api.v1._rbac import OWNER_ADMIN
from solvyn.db.models import Project, Repository
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError


@pytest.mark.asyncio
async def test_create_branch_uses_source_sha_and_solvyn_prefix() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        if request.method == "GET":
            assert request.url.path.endswith("/git/ref/heads/main")
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        assert request.method == "POST"
        assert request.url.path.endswith("/git/refs")
        assert request.read() == b'{"ref":"refs/heads/solvyn/task-12","sha":"base-sha"}'
        return httpx.Response(201, json={"object": {"sha": "base-sha"}})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        result = await github.create_branch("octo/repo", "solvyn/task-12", "main")
    await http.aclose()

    assert result == {"name": "solvyn/task-12", "sha": "base-sha"}


@pytest.mark.asyncio
async def test_create_branch_at_commit_uses_exact_pinned_revision() -> None:
    source_sha = "a" * 40

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path.endswith("/git/refs")
        assert request.read() == (
            b'{"ref":"refs/heads/solvyn/task-12","sha":"' + source_sha.encode() + b'"}'
        )
        return httpx.Response(201, json={"object": {"sha": source_sha}})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        result = await github.create_branch_at_commit("octo/repo", "solvyn/task-12", source_sha)
    await http.aclose()

    assert result == {"name": "solvyn/task-12", "sha": source_sha}


@pytest.mark.asyncio
async def test_create_branch_at_commit_rejects_invalid_revision_without_request() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid source revision must not make a GitHub request")

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        with pytest.raises(GitHubRepositoryError, match="Source commit revision is invalid"):
            await github.create_branch_at_commit("octo/repo", "solvyn/task-12", "not-a-sha")
    await http.aclose()


@pytest.mark.asyncio
async def test_commit_files_checks_expected_head_and_updates_ref_without_force() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        if request.method == "GET" and request.url.path.endswith("/git/ref/heads/solvyn/task-12"):
            return httpx.Response(200, json={"object": {"sha": "head-sha"}})
        if request.method == "GET" and request.url.path.endswith("/git/commits/head-sha"):
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        if request.method == "POST" and request.url.path.endswith("/git/trees"):
            assert request.read() == (
                b'{"base_tree":"tree-base","tree":[{"path":"docs/hello.txt","mode":"100644",'
                b'"type":"blob","content":"hello"}]}'
            )
            return httpx.Response(201, json={"sha": "tree-next"})
        if request.method == "POST" and request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit-sha"})
        if request.method == "PATCH" and request.url.path.endswith("/git/refs/heads/solvyn/task-12"):
            assert request.read() == b'{"sha":"commit-sha","force":false}'
            return httpx.Response(200, json={"ref": "refs/heads/solvyn/task-12"})
        raise AssertionError(f"Unexpected GitHub request: {request.method} {request.url}")

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        result = await github.commit_files(
            "octo/repo",
            "solvyn/task-12",
            expected_head_sha="head-sha",
            message="Add greeting",
            files=[{"path": "docs/hello.txt", "content": "hello"}],
        )
    await http.aclose()

    assert result == {"branch": "solvyn/task-12", "sha": "commit-sha"}


@pytest.mark.asyncio
async def test_commit_files_rejects_stale_head_before_writing() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={"object": {"sha": "new-head"}})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        with pytest.raises(GitHubRepositoryError, match="Branch head changed") as exc:
            await github.commit_files(
                "octo/repo",
                "solvyn/task-12",
                expected_head_sha="old-head",
                message="Add greeting",
                files=[{"path": "hello.txt", "content": "hello"}],
            )
    await http.aclose()

    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_create_branch_rejects_source_ref_traversal_before_request() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid source branch must not make a GitHub request")

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        with pytest.raises(GitHubRepositoryError, match="Branch name is invalid"):
            await github.create_branch("octo/repo", "solvyn/task-12", "../../other")
    await http.aclose()


@pytest.mark.asyncio
async def test_pull_request_creation_always_starts_as_draft() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret-token"
        assert request.method == "POST"
        assert request.read() == b'{"title":"Review this","body":"Please review","head":"solvyn/task-12","base":"main","draft":true}'
        return httpx.Response(
            201,
            json={"number": 42, "title": "Review this", "html_url": "https://github.com/octo/repo/pull/42", "state": "open", "draft": True},
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="secret-token") as github:
        result = await github.create_pull_request(
            "octo/repo", title="Review this", body="Please review", head="solvyn/task-12", base="main"
        )
    await http.aclose()

    assert result == {
        "number": 42,
        "title": "Review this",
        "url": "https://github.com/octo/repo/pull/42",
        "state": "open",
        "draft": True,
    }


def test_commit_validation_rejects_traversal_and_duplicate_paths() -> None:
    with pytest.raises(GitHubRepositoryError, match="invalid or duplicate"):
        GitHubClient._validate_commit_files([{"path": "../secrets.txt", "content": ""}])
    with pytest.raises(GitHubRepositoryError, match="invalid or duplicate"):
        GitHubClient._validate_commit_files(
            [{"path": "same.txt", "content": "one"}, {"path": "same.txt", "content": "two"}]
        )


def test_commit_validation_enforces_file_and_total_byte_caps() -> None:
    with pytest.raises(GitHubRepositoryError, match="between 1 and 50"):
        GitHubClient._validate_commit_files([{"path": f"{index}.txt", "content": ""} for index in range(51)])
    with pytest.raises(GitHubRepositoryError, match="500 KB"):
        GitHubClient._validate_commit_files(
            [{"path": "one.txt", "content": "a" * 300_000}, {"path": "two.txt", "content": "b" * 200_001}]
        )


@pytest.mark.asyncio
async def test_write_context_requires_owner_admin_and_uses_organization_token() -> None:
    from types import SimpleNamespace

    repository = cast(Repository, SimpleNamespace(provider="github"))
    organization_id = uuid.uuid4()
    repository_id = uuid.uuid4()
    project = cast(Project, SimpleNamespace(organization_id=organization_id))
    db = object()
    with (
        patch.object(repository_api, "_load_authorized_repository", new_callable=AsyncMock) as authorize,
        patch.object(repository_api, "load_organization_github_token", new_callable=AsyncMock) as load_token,
    ):
        authorize.return_value = (repository, project)
        load_token.return_value = "secret-token"
        result = await repository_api._load_github_write_context(repository_id, db)  # type: ignore[arg-type]

    authorize.assert_awaited_once_with(repository_id, db, roles=OWNER_ADMIN)
    load_token.assert_awaited_once_with(db, organization_id)
    assert result[0] is repository
    assert result[1] is project
    assert result[2] == "secret-token"
