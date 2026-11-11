"""Bounded, read-only GitHub repository inspection.

Repository names and file contents are remote user-controlled input. This
module returns data only; callers must preserve the untrusted marker when
passing it into any future agent context.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import tomllib
from collections import Counter
from typing import Any
from urllib.parse import quote

import httpx

from solvyn.repositories.protocols import RepositoryProvider

_OWNER_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_MANIFESTS = (
    "README.md",
    "pyproject.toml",
    "requirements.txt",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
    "Dockerfile",
    "docker-compose.yml",
    "Makefile",
)
_EXTENSIONS = {
    ".py": "Python",
    ".pyi": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".vue": "Vue",
    ".svelte": "Svelte",
}
_MAX_TREE_ENTRIES = 20_000
_MAX_TEXT_FILE_BYTES = 64_000
_MAX_WORKSPACE_FILE_BYTES = 1_000_000
_MAX_DIFF_BYTES = 1_000_000
_MAX_COMMIT_FILES = 50
_MAX_COMMIT_BYTES = 500_000
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,239}$")
_MAX_REVIEW_PAGES = 10
_REVIEWS_PER_PAGE = 100
_MAX_MANIFESTS = 6
_CONTENT_SEARCH_EXTENSIONS = frozenset({
    ".c", ".cc", ".cfg", ".conf", ".cpp", ".cs", ".css", ".csv", ".cxx", ".dart",
    ".dockerfile", ".go", ".graphql", ".h", ".hpp", ".html", ".ini", ".java", ".js",
    ".json", ".jsx", ".kt", ".kts", ".lock", ".lua", ".md", ".mjs", ".php", ".py",
    ".pyi", ".rb", ".rs", ".rst", ".scss", ".sh", ".sql", ".svelte", ".swift", ".toml",
    ".ts", ".tsx", ".txt", ".vue", ".xml", ".yaml", ".yml", ".zsh",
})
_MAX_CONTENT_SEARCH_CANDIDATES = 25
_MAX_CONTENT_SEARCH_FILE_BYTES = 64_000
_MAX_CONTENT_SEARCH_TOTAL_BYTES = 1_000_000
_MAX_CONTENT_SEARCH_MATCHES = 20
_MAX_CONTENT_SEARCH_SNIPPET_CHARS = 500
_MAX_CONTENT_SEARCH_PATH_CHARS = 512
_MAX_CONTENT_SEARCH_OUTPUT_BYTES = 16_000
_MAX_CONTENT_SEARCH_SECONDS = 45


class GitHubRepositoryError(Exception):
    """A safe provider error suitable for conversion to an HTTP response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def _validate_git_branch(branch: str) -> None:
    segments = branch.split("/")
    if (
        not _BRANCH_NAME_RE.fullmatch(branch)
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or branch.endswith(("/", "."))
        or any(segment.startswith(".") or segment.endswith(".lock") for segment in segments)
    ):
        raise GitHubRepositoryError(422, "Branch name is invalid")


def _validate_solvyn_branch(branch: str) -> None:
    _validate_git_branch(branch)
    if not branch.startswith("solvyn/"):
        raise GitHubRepositoryError(422, "Only Solvyn branches can receive generated changes")


def validate_full_name(full_name: str) -> tuple[str, str]:
    value = full_name.strip()
    if not _OWNER_REPO.fullmatch(value):
        raise GitHubRepositoryError(422, "Repository must use owner/repository format")
    owner, repo = value.split("/", 1)
    return owner, repo


class GitHubClient(RepositoryProvider):
    """Small GitHub REST client; supports httpx transport injection for tests."""

    def __init__(self, client: httpx.AsyncClient | None = None, *, token: str | None = None) -> None:
        self._owns_client = client is None
        self._token = token
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "Solvyn-Repository-Inspector/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers=headers,
        )
        # Inject credentials per request when the caller owns the client. This
        # avoids leaving an organization token on a shared AsyncClient.
        self._request_headers = {"Authorization": f"Bearer {token}"} if token and client is not None else None

    def git_auth_token(self) -> str | None:
        """Return the configured token to the isolated Git transport only."""
        return self._token

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def _get_json_payload(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        try:
            response = await self.http.get(path, params=params, headers=self._request_headers)
        except httpx.TimeoutException as exc:
            raise GitHubRepositoryError(504, "GitHub request timed out") from exc
        except httpx.HTTPError as exc:
            raise GitHubRepositoryError(502, "GitHub request failed") from exc

        if response.status_code == 404:
            raise GitHubRepositoryError(404, "Public GitHub repository or resource not found")
        if response.status_code == 429:
            raise GitHubRepositoryError(429, "GitHub rate limit reached")
        if response.status_code == 403:
            raise GitHubRepositoryError(429, "GitHub rate limit or access restriction reached")
        if response.status_code == 401:
            raise GitHubRepositoryError(401, "GitHub rejected the credential")
        if response.status_code >= 500:
            raise GitHubRepositoryError(502, "GitHub is temporarily unavailable")
        if not response.is_success:
            raise GitHubRepositoryError(502, "GitHub rejected the repository request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubRepositoryError(502, "GitHub returned invalid JSON") from exc
        return payload

    async def _get_json(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        payload = await self._get_json_payload(path, params=params)
        if not isinstance(payload, dict):
            raise GitHubRepositoryError(502, "GitHub returned an unexpected response")
        return payload

    async def _write_json(self, method: str, path: str, *, body: dict[str, Any]) -> dict[str, Any]:
        """Make a bounded GitHub JSON write request with safe error messages."""
        try:
            response = await self.http.request(method, path, json=body, headers=self._request_headers)
        except httpx.TimeoutException as exc:
            raise GitHubRepositoryError(504, "GitHub request timed out") from exc
        except httpx.HTTPError as exc:
            raise GitHubRepositoryError(502, "GitHub request failed") from exc
        if response.status_code == 401:
            raise GitHubRepositoryError(401, "GitHub rejected the credential")
        if response.status_code == 403:
            raise GitHubRepositoryError(403, "GitHub denied the requested repository write")
        if response.status_code == 404:
            raise GitHubRepositoryError(404, "GitHub repository or reference not found")
        if response.status_code in {409, 422}:
            raise GitHubRepositoryError(response.status_code, "GitHub rejected the repository write")
        if response.status_code == 429:
            raise GitHubRepositoryError(429, "GitHub rate limit reached")
        if response.status_code >= 500:
            raise GitHubRepositoryError(502, "GitHub is temporarily unavailable")
        if not response.is_success:
            raise GitHubRepositoryError(502, "GitHub rejected the repository write")
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubRepositoryError(502, "GitHub returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise GitHubRepositoryError(502, "GitHub returned an unexpected response")
        return payload

    async def _branch_ref(self, owner: str, repo: str, branch: str) -> dict[str, Any]:
        encoded_branch = quote(branch, safe="/")
        return await self._get_json(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/ref/heads/{encoded_branch}")

    async def create_branch(self, full_name: str, branch: str, from_branch: str) -> dict[str, str]:
        owner, repo = validate_full_name(full_name)
        _validate_solvyn_branch(branch)
        if not from_branch or len(from_branch) > 255:
            raise GitHubRepositoryError(422, "Source branch is invalid")
        _validate_git_branch(from_branch)
        source = await self._branch_ref(owner, repo, from_branch)
        object_value = source.get("object")
        source_sha = object_value.get("sha") if isinstance(object_value, dict) else None
        if not isinstance(source_sha, str) or not source_sha:
            raise GitHubRepositoryError(502, "GitHub returned an invalid source branch")
        created = await self._write_json(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": source_sha},
        )
        object_created = created.get("object")
        branch_sha = object_created.get("sha") if isinstance(object_created, dict) else source_sha
        return {"name": branch, "sha": str(branch_sha)}

    async def create_branch_at_commit(self, full_name: str, branch: str, commit_sha: str) -> dict[str, str]:
        """Create a Solvyn branch from an exact, validated commit revision."""
        owner, repo = validate_full_name(full_name)
        _validate_solvyn_branch(branch)
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", commit_sha):
            raise GitHubRepositoryError(422, "Source commit revision is invalid")
        created = await self._write_json(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/refs",
            body={"ref": f"refs/heads/{branch}", "sha": commit_sha.lower()},
        )
        object_created = created.get("object")
        branch_sha = object_created.get("sha") if isinstance(object_created, dict) else commit_sha.lower()
        return {"name": branch, "sha": str(branch_sha)}

    @staticmethod
    def _validate_commit_files(files: list[dict[str, str]], deleted_paths: list[str] | None = None) -> None:
        removals = deleted_paths or []
        if not 1 <= len(files) + len(removals) <= _MAX_COMMIT_FILES:
            raise GitHubRepositoryError(422, "A commit must include between 1 and 50 files")
        total_bytes = 0
        seen: set[str] = set()
        for path in [item.get("path", "") for item in files] + removals:
            segments = path.split("/")
            if (
                not path
                or path.startswith("/")
                or "\\" in path
                or any(segment in {"", ".", ".."} for segment in segments)
                or any(segment.casefold() == ".git" for segment in segments)
                or any(ord(character) < 32 or ord(character) == 127 for character in path)
                or len(path) > 512
                or path in seen
            ):
                raise GitHubRepositoryError(422, "Commit contains an invalid or duplicate file path")
            seen.add(path)
        for item in files:
            content = item.get("content", "")
            try:
                total_bytes += len(content.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise GitHubRepositoryError(422, "Commit content must be valid UTF-8 text") from exc
        if total_bytes > _MAX_COMMIT_BYTES:
            raise GitHubRepositoryError(413, "Commit content exceeds the 500 KB limit")

    async def commit_files(
        self,
        full_name: str,
        branch: str,
        *,
        expected_head_sha: str,
        message: str,
        files: list[dict[str, str]],
        deleted_paths: list[str] | None = None,
    ) -> dict[str, str]:
        owner, repo = validate_full_name(full_name)
        _validate_solvyn_branch(branch)
        if not expected_head_sha or len(expected_head_sha) > 64:
            raise GitHubRepositoryError(422, "Expected branch head is invalid")
        if not message.strip() or len(message) > 2000:
            raise GitHubRepositoryError(422, "Commit message must contain 1 to 2000 characters")
        removals = deleted_paths or []
        self._validate_commit_files(files, removals)
        current_ref = await self._branch_ref(owner, repo, branch)
        current_object = current_ref.get("object")
        current_sha = current_object.get("sha") if isinstance(current_object, dict) else None
        if current_sha != expected_head_sha:
            raise GitHubRepositoryError(409, "Branch head changed; fetch the latest head before committing")
        current_commit = await self._get_json(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/commits/{quote(expected_head_sha, safe='')}"
        )
        tree_value = current_commit.get("tree")
        base_tree = tree_value.get("sha") if isinstance(tree_value, dict) else None
        if not isinstance(base_tree, str):
            raise GitHubRepositoryError(502, "GitHub returned an invalid base tree")

        tree_items: list[dict[str, Any]] = [
            {"path": item["path"], "mode": "100644", "type": "blob", "content": item["content"]}
            for item in files
        ]
        tree_items.extend(
            {"path": path, "mode": "100644", "type": "blob", "sha": None} for path in removals
        )
        tree = await self._write_json(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/trees",
            body={"base_tree": base_tree, "tree": tree_items},
        )
        tree_sha = tree.get("sha")
        if not isinstance(tree_sha, str):
            raise GitHubRepositoryError(502, "GitHub returned an invalid commit tree")
        commit = await self._write_json(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/commits",
            body={"message": message, "tree": tree_sha, "parents": [expected_head_sha]},
        )
        commit_sha = commit.get("sha")
        if not isinstance(commit_sha, str):
            raise GitHubRepositoryError(502, "GitHub returned an invalid commit")
        await self._write_json(
            "PATCH",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/refs/heads/{quote(branch, safe='/')}",
            body={"sha": commit_sha, "force": False},
        )
        return {"branch": branch, "sha": commit_sha}

    async def create_pull_request(
        self, full_name: str, *, title: str, body: str, head: str, base: str
    ) -> dict[str, Any]:
        owner, repo = validate_full_name(full_name)
        _validate_solvyn_branch(head)
        if head == base:
            raise GitHubRepositoryError(422, "Pull requests must come from a Solvyn branch")
        if not title.strip() or len(title) > 256 or len(body) > 20_000:
            raise GitHubRepositoryError(422, "Pull request title or description is outside supported bounds")
        result = await self._write_json(
            "POST",
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls",
            body={"title": title, "body": body, "head": head, "base": base, "draft": True},
        )
        return {
            "number": result.get("number"),
            "title": str(result.get("title") or "")[:512],
            "url": str(result.get("html_url") or "")[:512],
            "state": str(result.get("state") or "unknown"),
            "draft": bool(result.get("draft", True)),
        }

    async def _get_text(self, path: str, *, accept: str, max_bytes: int) -> str:
        try:
            headers = {"Accept": accept}
            if self._request_headers:
                headers.update(self._request_headers)
            async with self.http.stream("GET", path, headers=headers) as response:
                if response.status_code == 404:
                    raise GitHubRepositoryError(404, "Public GitHub repository or resource not found")
                if response.status_code == 429:
                    raise GitHubRepositoryError(429, "GitHub rate limit reached")
                if response.status_code == 403:
                    raise GitHubRepositoryError(429, "GitHub rate limit or access restriction reached")
                if response.status_code >= 500:
                    raise GitHubRepositoryError(502, "GitHub is temporarily unavailable")
                if not response.is_success:
                    raise GitHubRepositoryError(502, "GitHub rejected the repository request")
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_bytes:
                    raise GitHubRepositoryError(413, "Pull request diff is too large to inspect safely")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise GitHubRepositoryError(413, "Pull request diff is too large to inspect safely")
        except GitHubRepositoryError:
            raise
        except httpx.TimeoutException as exc:
            raise GitHubRepositoryError(504, "GitHub request timed out") from exc
        except httpx.HTTPError as exc:
            raise GitHubRepositoryError(502, "GitHub request failed") from exc
        except ValueError as exc:
            raise GitHubRepositoryError(502, "GitHub returned an invalid diff response") from exc
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubRepositoryError(502, "GitHub returned a non-text pull request diff") from exc

    async def repository_metadata(self, full_name: str) -> dict[str, Any]:
        owner, repo = validate_full_name(full_name)
        data = await self._get_json(f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}")
        try:
            repo_id = int(data["id"])
            canonical_name = str(data["full_name"])
            default_branch = str(data["default_branch"])
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubRepositoryError(502, "GitHub repository metadata is incomplete") from exc
        return {
            "id": repo_id,
            "full_name": canonical_name,
            "description": str(data.get("description") or "")[:1000],
            "default_branch": default_branch[:255],
            "url": str(data.get("html_url") or f"https://github.com/{canonical_name}")[:512],
            "private": bool(data.get("private", False)),
        }

    async def authenticated_identity(self) -> dict[str, str]:
        """Validate the configured token without returning or logging it."""
        data = await self._get_json("/user")
        try:
            login = str(data["login"])
            account_id = str(int(data["id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubRepositoryError(502, "GitHub returned incomplete account information") from exc
        return {"login": login[:255], "account_id": account_id}

    async def repository_tree(self, full_name: str, branch: str) -> tuple[str, list[dict[str, Any]]]:
        owner, repo = validate_full_name(full_name)
        if not branch or len(branch) > 255:
            raise GitHubRepositoryError(422, "Repository default branch is invalid")
        encoded_branch = quote(branch, safe="")
        data = await self._get_json(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/git/trees/{encoded_branch}",
            params={"recursive": "1"},
        )
        entries = data.get("tree")
        if not isinstance(entries, list):
            raise GitHubRepositoryError(502, "GitHub returned an invalid repository tree")
        if data.get("truncated") or len(entries) > _MAX_TREE_ENTRIES:
            raise GitHubRepositoryError(413, "Repository tree is too large to inspect safely")
        paths: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = item["path"]
            if path.startswith("/") or ".." in path.split("/"):
                continue
            paths.append(
                {"path": path, "type": item.get("type", ""), "mode": item.get("mode"), "size": item.get("size")}
            )
        return str(data.get("sha") or ""), paths

    async def branch_commit(self, full_name: str, branch: str) -> str:
        owner, repo = validate_full_name(full_name)
        if not branch or len(branch) > 255 or "\x00" in branch:
            raise GitHubRepositoryError(422, "Repository branch is invalid")
        data = await self._get_json(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/branches/{quote(branch, safe='')}"
        )
        commit = data.get("commit")
        sha = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", sha):
            raise GitHubRepositoryError(502, "GitHub returned an invalid branch revision")
        return sha.lower()

    async def text_file(
        self,
        full_name: str,
        path: str,
        branch: str,
        *,
        max_bytes: int = _MAX_TEXT_FILE_BYTES,
    ) -> str | None:
        owner, repo = validate_full_name(full_name)
        if path.startswith("/") or ".." in path.split("/"):
            return None
        if not 1 <= max_bytes <= _MAX_WORKSPACE_FILE_BYTES:
            raise ValueError("max_bytes is outside the supported text file range")
        encoded_path = quote(path, safe="/")
        data = await self._get_json(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{encoded_path}",
            params={"ref": branch},
        )
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            return None
        encoded = data["content"].replace("\n", "")
        if len(encoded) > ((max_bytes + 2) // 3) * 4 + 8:
            return None
        try:
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) > max_bytes:
                return None
            return raw.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    async def inspect(self, full_name: str) -> dict[str, Any]:
        metadata = await self.repository_metadata(full_name)
        tree_sha, tree = await self.repository_tree(metadata["full_name"], metadata["default_branch"])
        files = {entry["path"] for entry in tree if entry["type"] == "blob"}
        extension_counts: Counter[str] = Counter()
        for path in files:
            ext = "." + path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
            language = _EXTENSIONS.get(ext)
            if language:
                extension_counts[language] += 1

        manifest_paths = [path for path in _MANIFESTS if path in files][: _MAX_MANIFESTS]
        manifest_content: dict[str, str] = {}
        for path in manifest_paths:
            content = await self.text_file(metadata["full_name"], path, metadata["default_branch"])
            if content is not None:
                manifest_content[path] = content

        return {
            "repository": metadata,
            "revision": tree_sha,
            "file_count": len(files),
            "top_level_entries": sorted({path.split("/", 1)[0] for path in files})[:100],
            "languages": dict(extension_counts.most_common(12)),
            "manifests": manifest_content,
            "content_trust": "untrusted_repository_data",
        }

    async def search_paths(self, full_name: str, branch: str, query: str) -> list[str]:
        if not query.strip():
            raise GitHubRepositoryError(422, "Search query must not be empty")
        _, paths = await self.repository_tree(full_name, branch)
        needle = query.strip().casefold()
        return [entry["path"] for entry in paths if needle in entry["path"].casefold()][:100]

    async def search_content(self, full_name: str, branch: str, query: str) -> dict[str, Any]:
        """Search bounded text blobs at one commit, with a total request deadline."""
        try:
            async with asyncio.timeout(_MAX_CONTENT_SEARCH_SECONDS):
                return await self._search_content_at_revision(full_name, branch, query)
        except TimeoutError as exc:
            raise GitHubRepositoryError(504, "Repository content search timed out") from exc

    async def _search_content_at_revision(self, full_name: str, branch: str, query: str) -> dict[str, Any]:
        needle_text = query.strip()
        if not needle_text:
            raise GitHubRepositoryError(422, "Search query must not be empty")
        if len(needle_text) > 200:
            raise GitHubRepositoryError(422, "Search query must be at most 200 characters")
        needle = needle_text.casefold()
        revision = await self.branch_commit(full_name, branch)
        tree_sha, tree = await self.repository_tree(full_name, revision)
        eligible_blobs = [
            entry
            for entry in tree
            if entry.get("type") == "blob"
            and entry.get("mode") in {"100644", "100755"}
            and isinstance(entry.get("path"), str)
            and "." + entry["path"].rsplit("/", 1)[-1].rsplit(".", 1)[-1].lower()
            in _CONTENT_SEARCH_EXTENSIONS
            and isinstance(entry.get("size"), int)
            and entry["size"] >= 0
        ]
        oversized_count = sum(entry["size"] > _MAX_CONTENT_SEARCH_FILE_BYTES for entry in eligible_blobs)
        candidates = sorted(
            (entry for entry in eligible_blobs if entry["size"] <= _MAX_CONTENT_SEARCH_FILE_BYTES),
            key=lambda entry: entry["path"],
        )
        truncated = oversized_count > 0 or len(candidates) > _MAX_CONTENT_SEARCH_CANDIDATES
        results: list[dict[str, Any]] = []
        fetched_bytes = 0
        files_scanned = 0
        for entry in candidates[:_MAX_CONTENT_SEARCH_CANDIDATES]:
            remaining = _MAX_CONTENT_SEARCH_TOTAL_BYTES - fetched_bytes
            if remaining <= 0 or entry["size"] > remaining:
                truncated = True
                break
            # Reserve the tree-reported blob size before fetching so binary or
            # invalid UTF-8 files still consume the aggregate network budget.
            fetched_bytes += entry["size"]
            content = await self.text_file(
                full_name,
                entry["path"],
                revision,
                max_bytes=min(_MAX_CONTENT_SEARCH_FILE_BYTES, remaining),
            )
            files_scanned += 1
            if content is None:
                # A selected candidate that cannot be decoded safely makes the scan incomplete.
                truncated = True
                continue
            if "\x00" in content:
                # UTF-8 can still contain NUL bytes; treat those blobs as binary.
                truncated = True
                continue
            for line_number, line in enumerate(content.splitlines(), start=1):
                if needle not in line.casefold():
                    continue
                if len(results) >= _MAX_CONTENT_SEARCH_MATCHES:
                    truncated = True
                    break
                excerpt = line.strip()
                if len(excerpt) > _MAX_CONTENT_SEARCH_SNIPPET_CHARS:
                    excerpt = excerpt[: _MAX_CONTENT_SEARCH_SNIPPET_CHARS - 1] + "…"
                results.append(
                    {
                        "path": entry["path"][:_MAX_CONTENT_SEARCH_PATH_CHARS],
                        "line": line_number,
                        "snippet": excerpt,
                    }
                )
            if len(results) >= _MAX_CONTENT_SEARCH_MATCHES:
                truncated = True
                break

        response = {
            "query": needle_text,
            "revision": revision,
            "tree_sha": tree_sha,
            "matches": results,
            "match_count": len(results),
            "candidate_count": len(candidates),
            "files_scanned": files_scanned,
            "fetched_bytes": fetched_bytes,
            "truncated": truncated,
            "content_trust": "untrusted_repository_data",
        }
        while len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > (
            _MAX_CONTENT_SEARCH_OUTPUT_BYTES
        ) and response["matches"]:
            response["matches"].pop()
            response["match_count"] = len(response["matches"])
            response["truncated"] = True
        return response

    async def branches(self, full_name: str) -> list[dict[str, Any]]:
        owner, repo = validate_full_name(full_name)
        data = await self._get_json_payload(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/branches",
            params={"per_page": "100", "page": "1"},
        )
        if not isinstance(data, list):
            raise GitHubRepositoryError(502, "GitHub returned an invalid branches list")
        branches: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                continue
            commit = item.get("commit")
            sha = commit.get("sha") if isinstance(commit, dict) else None
            branches.append(
                {
                    "name": item["name"][:255],
                    "commit_sha": str(sha)[:64] if sha else None,
                    "protected": bool(item.get("protected", False)),
                }
            )
        return branches

    async def pull_requests(
        self,
        full_name: str,
        *,
        state: str = "open",
        limit: int = 50,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        owner, repo = validate_full_name(full_name)
        if state not in {"open", "closed", "all"}:
            raise GitHubRepositoryError(422, "Pull request state must be open, closed, or all")
        if not 1 <= limit <= 100 or not 1 <= page <= 100:
            raise GitHubRepositoryError(422, "Pull request pagination is outside supported bounds")
        payload = await self._get_json_payload(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls",
            params={"state": state, "per_page": str(limit), "page": str(page), "sort": "updated", "direction": "desc"},
        )
        if not isinstance(payload, list):
            raise GitHubRepositoryError(502, "GitHub returned an invalid pull request list")

        items: list[dict[str, Any]] = []
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            head_value = item.get("head")
            base_value = item.get("base")
            user_value = item.get("user")
            head: dict[str, Any] = head_value if isinstance(head_value, dict) else {}
            base: dict[str, Any] = base_value if isinstance(base_value, dict) else {}
            user: dict[str, Any] = user_value if isinstance(user_value, dict) else {}
            items.append(
                {
                    "number": item.get("number"),
                    "title": str(item.get("title") or "")[:512],
                    "state": str(item.get("state") or "unknown"),
                    "draft": bool(item.get("draft", False)),
                    "merged": bool(item.get("merged_at")),
                    "url": str(item.get("html_url") or "")[:512],
                    "author": str(user.get("login") or "")[:255],
                    "head_ref": str(head.get("ref") or "")[:255],
                    "head_sha": str(head.get("sha") or "")[:64],
                    "base_ref": str(base.get("ref") or "")[:255],
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
        return items

    async def pull_request_detail(self, full_name: str, number: int) -> dict[str, Any]:
        owner, repo = validate_full_name(full_name)
        if number < 1:
            raise GitHubRepositoryError(422, "Pull request number must be positive")
        prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}"
        metadata = await self._get_json(prefix)
        review_status = await self._pull_request_review_status(
            prefix, metadata, max_pages=_MAX_REVIEW_PAGES, check_next_page=True
        )
        diff = await self._get_text(prefix, accept="application/vnd.github.diff", max_bytes=_MAX_DIFF_BYTES)
        head_value = metadata.get("head")
        base_value = metadata.get("base")
        head: dict[str, Any] = head_value if isinstance(head_value, dict) else {}
        base: dict[str, Any] = base_value if isinstance(base_value, dict) else {}
        return {
            "number": metadata.get("number"),
            "title": str(metadata.get("title") or "")[:512],
            "body": str(metadata.get("body") or "")[:20_000],
            "state": str(metadata.get("state") or "unknown"),
            "draft": bool(metadata.get("draft", False)),
            "merged": bool(metadata.get("merged_at")),
            "url": str(metadata.get("html_url") or "")[:512],
            "head_ref": str(head.get("ref") or "")[:255],
            "head_sha": str(head.get("sha") or "")[:64],
            "base_ref": str(base.get("ref") or "")[:255],
            "review_status": review_status,
            "diff": diff,
            "content_trust": "untrusted_repository_data",
        }

    async def pull_request_status(self, full_name: str, number: int) -> dict[str, Any]:
        """Fetch current PR metadata and bounded review state without downloading its diff."""
        owner, repo = validate_full_name(full_name)
        if number < 1:
            raise GitHubRepositoryError(422, "Pull request number must be positive")
        prefix = f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/pulls/{number}"
        metadata = await self._get_json(prefix)
        head_value = metadata.get("head")
        base_value = metadata.get("base")
        head: dict[str, Any] = head_value if isinstance(head_value, dict) else {}
        base: dict[str, Any] = base_value if isinstance(base_value, dict) else {}
        return {
            "number": metadata.get("number"),
            "title": str(metadata.get("title") or "")[:512],
            "state": str(metadata.get("state") or "unknown"),
            "draft": bool(metadata.get("draft", False)),
            "merged": bool(metadata.get("merged_at")),
            "url": str(metadata.get("html_url") or "")[:512],
            "head_ref": str(head.get("ref") or "")[:255],
            "head_sha": str(head.get("sha") or "")[:64],
            "base_ref": str(base.get("ref") or "")[:255],
            "review_status": await self._pull_request_review_status(prefix, metadata, max_pages=1),
            "content_trust": "untrusted_repository_data",
        }

    async def _pull_request_review_status(
        self,
        prefix: str,
        metadata: dict[str, Any],
        *,
        max_pages: int,
        check_next_page: bool = False,
    ) -> dict[str, Any]:
        raw_reviews: list[Any] = []
        review_history_truncated = False
        final_page = max_pages + int(check_next_page)
        for page in range(1, final_page + 1):
            page_reviews = await self._get_json_payload(
                f"{prefix}/reviews",
                params={"per_page": str(_REVIEWS_PER_PAGE), "page": str(page)},
            )
            if not isinstance(page_reviews, list):
                raise GitHubRepositoryError(502, "GitHub returned an invalid pull request review list")
            raw_reviews.extend(page_reviews[:_REVIEWS_PER_PAGE])
            if len(page_reviews) < _REVIEWS_PER_PAGE:
                break
            if page == final_page and check_next_page:
                next_page = await self._get_json_payload(
                    f"{prefix}/reviews",
                    params={"per_page": str(_REVIEWS_PER_PAGE), "page": str(page + 1)},
                )
                if not isinstance(next_page, list):
                    raise GitHubRepositoryError(502, "GitHub returned an invalid pull request review list")
                review_history_truncated = bool(next_page)
                break
            if page == max_pages and not check_next_page:
                review_history_truncated = True

        latest_by_reviewer: dict[str, tuple[str, str]] = {}
        for review in raw_reviews:
            if not isinstance(review, dict):
                continue
            user_value = review.get("user")
            user: dict[str, Any] = user_value if isinstance(user_value, dict) else {}
            login = str(user.get("login") or "")
            state = str(review.get("state") or "").upper()
            if not login or state == "PENDING":
                continue
            submitted = str(review.get("submitted_at") or review.get("created_at") or "")
            previous = latest_by_reviewer.get(login)
            if previous is None or submitted >= previous[0]:
                latest_by_reviewer[login] = (submitted, state)

        latest_states = [state for _, state in latest_by_reviewer.values()]
        reviewer_decisions = [
            {"login": login, "state": state, "submitted_at": submitted_at or None}
            for login, (submitted_at, state) in sorted(latest_by_reviewer.items())
        ]
        requested = metadata.get("requested_reviewers")
        requested_reviewers = [
            str(reviewer.get("login") or "")[:255]
            for reviewer in requested[:100]
            if isinstance(reviewer, dict) and reviewer.get("login")
        ] if isinstance(requested, list) else []
        if review_history_truncated:
            review_state = "incomplete_history"
        elif "CHANGES_REQUESTED" in latest_states:
            review_state = "changes_requested"
        elif "APPROVED" in latest_states:
            review_state = "approved"
        elif requested_reviewers:
            review_state = "review_required"
        elif latest_states:
            review_state = "reviewed_without_approval"
        else:
            review_state = "no_reviews"

        return {
            "state": review_state,
            "approvals": latest_states.count("APPROVED"),
            "changes_requested": latest_states.count("CHANGES_REQUESTED"),
            "requested_reviewers": requested_reviewers,
            "reviewer_decisions": reviewer_decisions,
            "history_truncated": review_history_truncated,
        }

    @staticmethod
    def parse_dependencies(manifests: dict[str, str]) -> dict[str, list[str]]:
        """Extract direct dependency names from common manifest files."""
        found: dict[str, list[str]] = {}
        if raw := manifests.get("package.json"):
            try:
                package = json.loads(raw)
                names = set(package.get("dependencies", {})) | set(package.get("devDependencies", {}))
                found["package.json"] = sorted(str(name) for name in names)[:200]
            except (ValueError, AttributeError, TypeError):
                found["package.json"] = []
        if raw := manifests.get("pyproject.toml"):
            try:
                data = tomllib.loads(raw)
                dependencies = data.get("project", {}).get("dependencies", [])
                found["pyproject.toml"] = [str(dep).split(";")[0].strip() for dep in dependencies[:200]]
            except (tomllib.TOMLDecodeError, AttributeError, TypeError):
                found["pyproject.toml"] = []
        if raw := manifests.get("requirements.txt"):
            found["requirements.txt"] = [
                line.split("#", 1)[0].strip()
                for line in raw.splitlines()
                if line.strip() and not line.lstrip().startswith(("#", "-"))
            ][:200]
        return found

    @staticmethod
    def detect_frameworks(dependencies: dict[str, list[str]]) -> list[str]:
        names = {
            item.split(";")[0].split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip().lower()
            for entries in dependencies.values()
            for item in entries
        }
        known = {
            "fastapi": "FastAPI",
            "django": "Django",
            "flask": "Flask",
            "next": "Next.js",
            "react": "React",
            "vue": "Vue",
            "svelte": "Svelte",
            "express": "Express",
            "nestjs": "NestJS",
            "langchain": "LangChain",
            "sqlalchemy": "SQLAlchemy",
        }
        return sorted({label for name, label in known.items() if name in names})
