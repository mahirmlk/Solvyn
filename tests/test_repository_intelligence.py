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
async def test_inspect_private_repository_uses_scoped_token_without_returning_it() -> None:
    token = "ghp-private-repository-token"
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com"
        assert request.headers["authorization"] == f"Bearer {token}"
        requests.append(request.url.path)
        if request.url.path == "/repos/acme/private-service":
            return httpx.Response(
                200,
                json={
                    "id": 456,
                    "full_name": "acme/private-service",
                    "description": "Private service",
                    "default_branch": "main",
                    "html_url": "https://github.com/acme/private-service",
                    "private": True,
                },
            )
        if request.url.path == "/repos/acme/private-service/git/trees/main":
            return httpx.Response(
                200,
                json={"sha": "b" * 40, "truncated": False, "tree": [{"path": "README.md", "type": "blob", "size": 6}]},
            )
        if request.url.path == "/repos/acme/private-service/contents/README.md":
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"secret").decode()})
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token=token) as github:
        inspection = await github.inspect("acme/private-service")
    await http.aclose()

    assert inspection["repository"]["private"] is True
    assert inspection["manifests"]["README.md"] == "secret"
    assert token not in repr(inspection)
    assert requests == [
        "/repos/acme/private-service",
        "/repos/acme/private-service/git/trees/main",
        "/repos/acme/private-service/contents/README.md",
    ]


@pytest.mark.asyncio
async def test_inspect_pins_tree_and_manifests_to_requested_revision() -> None:
    revision = "c" * 40
    requests: list[tuple[str, dict[str, str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path == "/repos/octo/example":
            return httpx.Response(
                200,
                json={"id": 123, "full_name": "octo/example", "default_branch": "main", "private": False},
            )
        if request.url.path == f"/repos/octo/example/git/trees/{revision}":
            return httpx.Response(
                200,
                json={"sha": "d" * 40, "truncated": False, "tree": [{"path": "README.md", "type": "blob", "size": 6}]},
            )
        if request.url.path == "/repos/octo/example/contents/README.md":
            assert request.url.params.get("ref") == revision
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"pinned").decode()})
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        inspection = await github.inspect("octo/example", revision)
    await http.aclose()

    assert inspection["revision"] == "d" * 40
    assert inspection["manifests"]["README.md"] == "pinned"
    assert requests == [
        ("/repos/octo/example", {}),
        (f"/repos/octo/example/git/trees/{revision}", {}),
        ("/repos/octo/example/contents/README.md", {"ref": revision}),
    ]


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
        assert dict(request.url.params) == {"per_page": "100", "page": "1"}
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


@pytest.mark.asyncio
async def test_branch_commit_resolves_to_an_immutable_commit_sha() -> None:
    expected_sha = "a" * 40

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/example/branches/main"
        return httpx.Response(200, json={"commit": {"sha": expected_sha}})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        sha = await github.branch_commit("octo/example", "main")
    await http.aclose()

    assert sha == expected_sha


@pytest.mark.asyncio
async def test_text_file_allows_bounded_full_text_without_truncation() -> None:
    content = "x" * 80_000

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/example/contents/large.txt"
        assert request.url.params["ref"] == "a" * 40
        encoded = base64.b64encode(content.encode()).decode()
        return httpx.Response(200, json={"encoding": "base64", "content": encoded})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        fetched = await github.text_file("octo/example", "large.txt", "a" * 40, max_bytes=100_000)
        too_small = await github.text_file("octo/example", "large.txt", "a" * 40, max_bytes=64_000)
    await http.aclose()

    assert fetched == content
    assert too_small is None


@pytest.mark.asyncio
async def test_pull_requests_lists_bounded_live_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/example/pulls"
        assert dict(request.url.params) == {
            "state": "open",
            "per_page": "25",
            "page": "2",
            "sort": "updated",
            "direction": "desc",
        }
        return httpx.Response(
            200,
            json=[
                {
                    "number": 42,
                    "title": "Reviewable change",
                    "state": "open",
                    "draft": False,
                    "merged_at": None,
                    "html_url": "https://github.com/octo/example/pull/42",
                    "user": {"login": "octocat"},
                    "head": {"ref": "feature", "sha": "abc123"},
                    "base": {"ref": "main"},
                    "created_at": "2026-11-05T12:00:00Z",
                    "updated_at": "2026-11-05T13:00:00Z",
                }
            ],
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        pull_requests = await github.pull_requests("octo/example", limit=25, page=2)
    await http.aclose()

    assert pull_requests == [
        {
            "number": 42,
            "title": "Reviewable change",
            "state": "open",
            "draft": False,
            "merged": False,
            "url": "https://github.com/octo/example/pull/42",
            "author": "octocat",
            "head_ref": "feature",
            "head_sha": "abc123",
            "base_ref": "main",
            "created_at": "2026-11-05T12:00:00Z",
            "updated_at": "2026-11-05T13:00:00Z",
        }
    ]


@pytest.mark.asyncio
async def test_pull_request_detail_returns_bounded_diff_and_latest_review_state() -> None:
    diff = "diff --git a/app.py b/app.py\n+print('change')\n"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/example/pulls/42":
            if request.headers.get("accept") == "application/vnd.github.diff":
                return httpx.Response(200, text=diff)
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "title": "Reviewable change",
                    "body": "Please review.",
                    "state": "open",
                    "draft": False,
                    "merged_at": None,
                    "html_url": "https://github.com/octo/example/pull/42",
                    "head": {"ref": "feature", "sha": "abc123"},
                    "base": {"ref": "main"},
                    "requested_reviewers": [{"login": "reviewer-three"}],
                },
            )
        if request.url.path == "/repos/octo/example/pulls/42/reviews":
            page = request.url.params.get("page")
            if page == "1":
                return httpx.Response(
                    200,
                    json=[
                        {"user": {"login": "reviewer-one"}, "state": "APPROVED", "submitted_at": "2026-11-05T12:00:00Z"},
                        *[
                            {
                                "user": {"login": f"commenter-{index}"},
                                "state": "COMMENTED",
                                "submitted_at": "2026-11-05T12:30:00Z",
                            }
                            for index in range(99)
                        ],
                    ],
                )
            return httpx.Response(
                200,
                json=[
                    {"user": {"login": "reviewer-one"}, "state": "CHANGES_REQUESTED", "submitted_at": "2026-11-05T13:00:00Z"},
                    {"user": {"login": "reviewer-two"}, "state": "APPROVED", "submitted_at": "2026-11-05T13:05:00Z"},
                ],
            )
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        result = await github.pull_request_detail("octo/example", 42)
    await http.aclose()

    assert result["diff"] == diff
    assert result["content_trust"] == "untrusted_repository_data"
    review_status = result["review_status"]
    assert review_status == {
        "state": "changes_requested",
        "approvals": 1,
        "changes_requested": 1,
        "requested_reviewers": ["reviewer-three"],
        "reviewer_decisions": review_status["reviewer_decisions"],
        "history_truncated": False,
    }
    decisions = {review["login"]: review for review in review_status["reviewer_decisions"]}
    assert len(decisions) == 101
    assert decisions["reviewer-one"]["state"] == "CHANGES_REQUESTED"
    assert decisions["reviewer-two"]["state"] == "APPROVED"


@pytest.mark.asyncio
async def test_pull_request_status_does_not_fetch_diff_or_unbounded_review_history() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/repos/octo/example/pulls/42":
            return httpx.Response(
                200,
                json={
                    "number": 42,
                    "title": "Current title",
                    "state": "open",
                    "draft": True,
                    "merged_at": None,
                    "html_url": "https://github.com/octo/example/pull/42",
                    "head": {"ref": "solvyn/task-42", "sha": "abc123"},
                    "base": {"ref": "main"},
                    "requested_reviewers": [],
                },
            )
        if request.url.path == "/repos/octo/example/pulls/42/reviews":
            assert request.url.params["page"] == "1"
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": f"reviewer-{index}"},
                        "state": "APPROVED",
                        "submitted_at": "2026-11-06T08:00:00Z",
                    }
                    for index in range(100)
                ],
            )
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        result = await github.pull_request_status("octo/example", 42)
    await http.aclose()

    assert result["state"] == "open"
    assert result["draft"] is True
    assert result["review_status"]["state"] == "incomplete_history"
    assert result["review_status"]["history_truncated"] is True
    assert requests == ["/repos/octo/example/pulls/42", "/repos/octo/example/pulls/42/reviews"]


@pytest.mark.asyncio
async def test_pull_request_diff_rejects_oversized_responses() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/pulls/42") and request.headers.get("accept") == "application/vnd.github.diff":
            return httpx.Response(200, content=b"x" * 1_000_001)
        return httpx.Response(
            200,
            json={"number": 42, "head": {}, "base": {}, "requested_reviewers": []},
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        with pytest.raises(GitHubRepositoryError) as exc:
            await github.pull_request_detail("octo/example", 42)
    await http.aclose()

    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_pull_request_review_history_reports_when_bounded_scan_is_incomplete() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": f"reviewer-{index}"},
                        "state": "APPROVED",
                        "submitted_at": "2026-11-05T12:00:00Z",
                    }
                    for index in range(100)
                ],
            )
        if request.headers.get("accept") == "application/vnd.github.diff":
            return httpx.Response(200, text="diff")
        return httpx.Response(
            200,
            json={"number": 42, "head": {}, "base": {}, "requested_reviewers": []},
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        result = await github.pull_request_detail("octo/example", 42)
    await http.aclose()

    assert result["review_status"]["state"] == "incomplete_history"
    assert result["review_status"]["history_truncated"] is True


@pytest.mark.asyncio
async def test_pull_request_review_history_accepts_exact_page_cap() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/reviews"):
            if request.url.params.get("page") == "12":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "user": {"login": f"reviewer-{index}"},
                        "state": "APPROVED",
                        "submitted_at": "2026-11-05T12:00:00Z",
                    }
                    for index in range(100)
                ],
            )
        if request.headers.get("accept") == "application/vnd.github.diff":
            return httpx.Response(200, text="diff")
        return httpx.Response(
            200,
            json={"number": 42, "head": {}, "base": {}, "requested_reviewers": []},
        )

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        result = await github.pull_request_detail("octo/example", 42)
    await http.aclose()

    assert result["review_status"]["state"] == "approved"
    assert result["review_status"]["history_truncated"] is False


@pytest.mark.asyncio
async def test_provider_maps_explicit_rate_limit_response() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"message": "untrusted upstream details"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http) as github:
        with pytest.raises(GitHubRepositoryError) as exc:
            await github.repository_metadata("octo/example")
    await http.aclose()

    assert exc.value.status_code == 429
    assert "untrusted upstream details" not in exc.value.message
