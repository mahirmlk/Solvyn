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

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.http = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Solvyn-Repository-Inspector/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def _get_json_payload(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        try:
            response = await self.http.get(path, params=params)
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
