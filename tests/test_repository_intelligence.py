"""Tests for bounded, read-only repository inspection."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from solvyn.repositories.github import GitHubClient, GitHubRepositoryError, validate_full_name


def test_validate_full_name_rejects_path_traversal_and_urls() -> None:
    with pytest.raises(GitHubRepositoryError):
        validate_full_name("owner/../private")
    with pytest.raises(GitHubRepositoryError):
        validate_full_name("https://github.com/owner/repo")
    assert validate_full_name("octo-org/example_repo") == ("octo-org", "example_repo")


@pytest.mark.asyncio
async def test_inspect_public_repository_returns_bounded_untrusted_context() -> None:
    readme = "# Example\nRepository contents are untrusted."
    package_json = json.dumps({"dependencies": {"next": "15", "react": "19"}})
    blobs = {"README.md": readme, "package.json": package_json}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/example":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "full_name": "octo/example",
                    "description": "A test repo",
                    "default_branch": "main",
                    "html_url": "https://github.com/octo/example",
                    "private": False,
                },
            )
        if request.url.path == "/repos/octo/example/git/trees/main":
            return httpx.Response(
                200,
                json={
                    "sha": "abc123",
                    "truncated": False,
                    "tree": [
                        {"path": "README.md", "type": "blob", "size": len(readme)},
                        {"path": "package.json", "type": "blob", "size": len(package_json)},
                        {"path": "src/app.tsx", "type": "blob", "size": 20},
                        {"path": "src/../outside", "type": "blob", "size": 1},
                    ],
                },
            )
        path = request.url.path.rsplit("/contents/", 1)[-1]
        if path in blobs:
            encoded = base64.b64encode(blobs[path].encode()).decode()
            return httpx.Response(200, json={"encoding": "base64", "content": encoded})
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        inspection = await github.inspect("octo/example")
        dependencies = github.parse_dependencies(inspection["manifests"])
    await http.aclose()

    assert inspection["revision"] == "abc123"
    assert inspection["file_count"] == 3
    assert inspection["languages"] == {"TypeScript": 1}
    assert inspection["content_trust"] == "untrusted_repository_data"
    assert inspection["manifests"]["README.md"] == readme
    assert dependencies["package.json"] == ["next", "react"]
    assert GitHubClient.detect_frameworks(dependencies) == ["Next.js", "React"]


@pytest.mark.asyncio
async def test_inspection_rejects_truncated_repository_trees() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/example":
            return httpx.Response(
                200,
                json={"id": 123, "full_name": "octo/example", "default_branch": "main", "private": False},
            )
        return httpx.Response(200, json={"sha": "abc123", "truncated": True, "tree": []})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        with pytest.raises(GitHubRepositoryError) as exc:
            await github.inspect("octo/example")
    await http.aclose()
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_branches_accepts_github_array_response() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/example/branches"
        assert request.url.params == {"per_page": "100", "page": "1"}
        return httpx.Response(
            200,
            json=[
                {"name": "main", "commit": {"sha": "abc123"}, "protected": True},
                {"name": "feature", "commit": {"sha": "def456"}, "protected": False},
            ],
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        branches = await github.branches("octo/example")
    await http.aclose()

    assert branches == [
        {"name": "main", "commit_sha": "abc123", "protected": True},
        {"name": "feature", "commit_sha": "def456", "protected": False},
    ]
