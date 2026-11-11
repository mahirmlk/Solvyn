"""Tests for bounded repository manifest discovery."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import patch

import httpx
import pytest

from solvyn.repositories.dependencies import MAX_MANIFEST_BYTES, MAX_MANIFESTS, MAX_TOTAL_BYTES
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError

_COMMIT_REVISION = "c" * 40
_TREE_SHA = "a" * 40


def _mock_client(
    tree: list[dict[str, object]],
    blobs: dict[str, str | None],
    requests: list[str] | None = None,
    refs: list[str] | None = None,
) -> tuple[GitHubClient, httpx.AsyncClient]:
    seen = requests if requests is not None else []
    seen_refs = refs if refs is not None else []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/repos/octo/example":
            return httpx.Response(
                200,
                json={
                    "id": 123,
                    "full_name": "octo/example",
                    "description": "Repository used by a test",
                    "default_branch": "main",
                    "html_url": "https://github.com/octo/example",
                    "private": False,
                },
            )
        if request.url.path == "/repos/octo/example/branches/main":
            return httpx.Response(200, json={"commit": {"sha": _COMMIT_REVISION}})
        if request.url.path == f"/repos/octo/example/git/commits/{_COMMIT_REVISION}":
            return httpx.Response(200, json={"tree": {"sha": _TREE_SHA}})
        if request.url.path == f"/repos/octo/example/git/trees/{_TREE_SHA}":
            return httpx.Response(200, json={"sha": _TREE_SHA, "truncated": False, "tree": tree})
        if "/contents/" in request.url.path:
            path = request.url.path.split("/contents/", 1)[1]
            seen_refs.append(request.url.params.get("ref", ""))
            value = blobs.get(path)
            if value is None:
                return httpx.Response(200, json={"encoding": "none", "content": ""})
            return httpx.Response(
                200,
                json={"encoding": "base64", "content": base64.b64encode(value.encode("utf-8")).decode("ascii")},
            )
        return httpx.Response(404, json={"message": "Not Found"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubClient(http), http


def _blob(path: str, content: str, *, mode: str = "100644") -> dict[str, object]:
    return {"path": path, "type": "blob", "mode": mode, "size": len(content.encode("utf-8"))}


@pytest.mark.asyncio
async def test_inspect_reads_root_context_then_one_nested_manifest_level() -> None:
    contents = {
        "README.md": "# root context",
        "package.json": json.dumps({"dependencies": {"root-package": "1"}}),
        "requirements-dev.txt": "pytest>=8\n",
        "service/pyproject.toml": '[project]\ndependencies = ["fastapi>=0.1"]\n',
        "service/deeper/package.json": json.dumps({"dependencies": {"too-deep": "1"}}),
    }
    request_paths: list[str] = []
    tree = [_blob(path, content) for path, content in contents.items()]
    manifest_refs: list[str] = []
    github, http = _mock_client(tree, contents, requests=request_paths, refs=manifest_refs)
    try:
        async with github:
            inspection = await github.inspect("octo/example")
    finally:
        await http.aclose()

    assert inspection["revision"] == _COMMIT_REVISION
    assert inspection["tree_sha"] == _TREE_SHA
    assert request_paths[:4] == [
        "/repos/octo/example",
        "/repos/octo/example/branches/main",
        f"/repos/octo/example/git/commits/{_COMMIT_REVISION}",
        f"/repos/octo/example/git/trees/{_TREE_SHA}",
    ]
    assert list(inspection["manifests"]) == [
        "README.md",
        "package.json",
        "requirements-dev.txt",
        "service/pyproject.toml",
    ]
    assert inspection["manifest_discovery"] == {
        "candidate_count": 4,
        "manifest_candidate_count": 3,
        "context_candidate_count": 1,
        "scanned_count": 4,
        "omitted_count": 0,
        "truncated": False,
        "coverage": "complete",
        "max_depth": 1,
        "max_candidates": MAX_MANIFESTS,
        "max_file_bytes": MAX_MANIFEST_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
    }
    assert manifest_refs == [_COMMIT_REVISION] * 4
    dependencies = github.parse_dependencies(inspection["manifests"])
    assert dependencies["package.json"] == ["root-package"]
    assert dependencies["service/pyproject.toml"] == ["fastapi"]


@pytest.mark.asyncio
async def test_inspect_candidate_selection_is_deterministic_and_root_context_has_priority() -> None:
    readme = "# repository"
    nested = {
        f"service-{index:02d}/package.json": json.dumps({"dependencies": {f"dep-{index:02d}": "1"}})
        for index in reversed(range(MAX_MANIFESTS + 5))
    }
    contents = {"README.md": readme, **nested}
    tree = [_blob(path, content) for path, content in reversed(list(contents.items()))]
    requested: list[str] = []
    github, http = _mock_client(tree, contents, requested)
    try:
        async with github:
            inspection = await github.inspect("octo/example")
    finally:
        await http.aclose()

    selected_paths = [path.split("/contents/", 1)[1] for path in requested if "/contents/" in path]
    assert selected_paths == ["README.md"] + sorted(nested)[: MAX_MANIFESTS - 1]
    assert inspection["manifest_discovery"]["candidate_count"] == MAX_MANIFESTS + 6
    assert inspection["manifest_discovery"]["manifest_candidate_count"] == MAX_MANIFESTS + 5
    assert inspection["manifest_discovery"]["context_candidate_count"] == 1
    assert inspection["manifest_discovery"]["scanned_count"] == MAX_MANIFESTS
    assert inspection["manifest_discovery"]["omitted_count"] == 6
    assert inspection["manifest_discovery"]["truncated"] is True
    assert inspection["manifest_discovery"]["coverage"] == "partial"


@pytest.mark.asyncio
async def test_inspect_marks_oversized_and_unreadable_manifest_omissions() -> None:
    unreadable_content = "placeholder"
    tree = [
        {"path": "large/package.json", "type": "blob", "mode": "100644", "size": MAX_MANIFEST_BYTES + 1},
        _blob("unreadable/requirements.txt", unreadable_content),
        _blob("nul/package.json", "bad\x00text"),
        {"path": "link/package.json", "type": "blob", "mode": "120000", "size": 20},
    ]
    github, http = _mock_client(tree, {"unreadable/requirements.txt": None, "nul/package.json": "bad\x00text"})
    try:
        async with github:
            inspection = await github.inspect("octo/example")
    finally:
        await http.aclose()

    coverage = inspection["manifest_discovery"]
    assert coverage["candidate_count"] == 3
    assert coverage["scanned_count"] == 2
    assert coverage["omitted_count"] == 3
    assert coverage["truncated"] is True
    assert inspection["manifests"] == {}


@pytest.mark.asyncio
async def test_inspect_enforces_aggregate_manifest_byte_budget() -> None:
    body = "#" + "x" * (MAX_MANIFEST_BYTES - 1)
    contents = {f"pkg-{index:02d}/requirements.txt": body for index in range(9)}
    tree = [_blob(path, content) for path, content in contents.items()]
    github, http = _mock_client(tree, contents)
    try:
        async with github:
            inspection = await github.inspect("octo/example")
    finally:
        await http.aclose()

    coverage = inspection["manifest_discovery"]
    assert coverage["candidate_count"] == 9
    assert coverage["scanned_count"] == MAX_TOTAL_BYTES // MAX_MANIFEST_BYTES
    assert sum(len(content.encode("utf-8")) for content in inspection["manifests"].values()) == MAX_TOTAL_BYTES
    assert coverage["omitted_count"] == 1
    assert coverage["truncated"] is True
    assert coverage["coverage"] == "partial"


@pytest.mark.asyncio
async def test_inspect_reserves_unknown_size_unreadable_blobs_against_aggregate_budget() -> None:
    tree = [
        {"path": f"pkg-{index:02d}/requirements.txt", "type": "blob", "mode": "100644"}
        for index in range(9)
    ]
    requests: list[str] = []
    github, http = _mock_client(tree, {entry["path"]: None for entry in tree}, requests)
    try:
        async with github:
            inspection = await github.inspect("octo/example")
    finally:
        await http.aclose()

    content_reads = [path for path in requests if "/contents/" in path]
    assert len(content_reads) == MAX_TOTAL_BYTES // MAX_MANIFEST_BYTES
    assert inspection["manifest_discovery"]["scanned_count"] == len(content_reads)
    assert inspection["manifest_discovery"]["omitted_count"] == len(tree)
    assert inspection["manifest_discovery"]["truncated"] is True


@pytest.mark.asyncio
async def test_inspect_total_deadline_returns_safe_timeout() -> None:
    github, http = _mock_client([], {})

    async def slow_metadata(_: str) -> dict[str, object]:
        await asyncio.sleep(1)
        return {}

    with patch.object(github, "repository_metadata", new=slow_metadata):
        with patch("solvyn.repositories.github._MAX_INSPECTION_SECONDS", 0.001):
            with pytest.raises(GitHubRepositoryError) as exc:
                await github.inspect("octo/example")
    await http.aclose()
    assert exc.value.status_code == 504
    assert str(exc.value) == "Repository inspection timed out"


def test_provider_parser_delegates_nested_manifest_names_to_bounded_parser() -> None:
    manifests = {
        "apps/api/requirements-prod.txt": "fastapi>=0.1\n",
        "apps/web/package.json": json.dumps({"dependencies": {"react": "19"}}),
    }
    assert GitHubClient.parse_dependencies(manifests) == {
        "apps/api/requirements-prod.txt": ["fastapi"],
        "apps/web/package.json": ["react"],
    }
