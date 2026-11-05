"""Bounded, read-only GitHub repository inspection.

Repository names and file contents are remote user-controlled input. This
module returns data only; callers must preserve the untrusted marker when
passing it into any future agent context.
"""

from __future__ import annotations

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
_MAX_DIFF_BYTES = 1_000_000
_MAX_REVIEW_PAGES = 10
_REVIEWS_PER_PAGE = 100
_MAX_MANIFESTS = 6


class GitHubRepositoryError(Exception):
    """A safe provider error suitable for conversion to an HTTP response."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


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
            paths.append({"path": path, "type": item.get("type", ""), "size": item.get("size")})
        return str(data.get("sha") or ""), paths

    async def text_file(self, full_name: str, path: str, branch: str) -> str | None:
        owner, repo = validate_full_name(full_name)
        if path.startswith("/") or ".." in path.split("/"):
            return None
        encoded_path = quote(path, safe="/")
        data = await self._get_json(
            f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{encoded_path}",
            params={"ref": branch},
        )
        if data.get("encoding") != "base64" or not isinstance(data.get("content"), str):
            return None
        encoded = data["content"].replace("\n", "")
        if len(encoded) > ((_MAX_TEXT_FILE_BYTES + 2) // 3) * 4 + 8:
            return None
        try:
            raw = base64.b64decode(encoded, validate=True)
            return raw.decode("utf-8")[:_MAX_TEXT_FILE_BYTES]
        except (ValueError, UnicodeDecodeError):
            return None

    async def inspect(self, full_name: str) -> dict[str, Any]:
        metadata = await self.repository_metadata(full_name)
        if metadata["private"]:
            raise GitHubRepositoryError(403, "Private repositories are not supported by this provider configuration")
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
        raw_reviews: list[Any] = []
        review_history_truncated = False
        for page in range(1, _MAX_REVIEW_PAGES + 2):
            page_reviews = await self._get_json_payload(
                f"{prefix}/reviews",
                params={"per_page": str(_REVIEWS_PER_PAGE), "page": str(page)},
            )
            if not isinstance(page_reviews, list):
                raise GitHubRepositoryError(502, "GitHub returned an invalid pull request review list")
            raw_reviews.extend(page_reviews[:_REVIEWS_PER_PAGE])
            if len(page_reviews) < _REVIEWS_PER_PAGE:
                break
            if page > _MAX_REVIEW_PAGES:
                next_page = await self._get_json_payload(
                    f"{prefix}/reviews",
                    params={"per_page": str(_REVIEWS_PER_PAGE), "page": str(page + 1)},
                )
                if not isinstance(next_page, list):
                    raise GitHubRepositoryError(502, "GitHub returned an invalid pull request review list")
                review_history_truncated = bool(next_page)
                break

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
            "review_status": {
                "state": review_state,
                "approvals": latest_states.count("APPROVED"),
                "changes_requested": latest_states.count("CHANGES_REQUESTED"),
                "requested_reviewers": requested_reviewers,
                "reviewer_decisions": reviewer_decisions,
                "history_truncated": review_history_truncated,
            },
            "diff": diff,
            "content_trust": "untrusted_repository_data",
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
