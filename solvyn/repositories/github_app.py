"""GitHub App JWT and narrowly scoped installation-token support."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections import OrderedDict
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from solvyn.config import settings
from solvyn.repositories.github import GitHubRepositoryError

_MAX_TOKEN_CACHE = 256
_TOKEN_EARLY_RENEW_SECONDS = 60
_ALLOWED_PERMISSIONS = {
    "contents", "metadata", "pull_requests", "vulnerability_alerts",
    "security_events", "secret_scanning_alerts",
}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def app_jwt(*, app_id: str | None = None, private_key: str | None = None, now: int | None = None) -> str:
    """Create a short-lived GitHub App JWT using configured or supplied credentials."""
    configured_id = app_id or settings.github_app_id
    configured_key = (private_key or settings.github_app_private_key).replace("\\n", "\n")
    if not configured_id or not configured_key:
        raise GitHubRepositoryError(503, "GitHub App is not configured")
    issued = int(time.time()) if now is None else now
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = _b64url(json.dumps({"iat": issued - 30, "exp": issued + 8 * 60, "iss": configured_id}, separators=(",", ":")).encode())
    signing_input = f"{header}.{claims}".encode("ascii")
    try:
        key = load_pem_private_key(configured_key.encode("utf-8"), password=None)
    except (TypeError, ValueError) as exc:
        raise GitHubRepositoryError(503, "GitHub App private key is invalid") from exc
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubRepositoryError(503, "GitHub App private key must use RSA")
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64url(signature)}"


class InstallationTokenCache:
    """Process-local LRU of short-lived tokens keyed by exact installation scope."""

    def __init__(self, *, max_entries: int = _MAX_TOKEN_CACHE) -> None:
        self.max_entries = max_entries
        self._items: OrderedDict[tuple[str, str, int, tuple[int, ...], tuple[tuple[str, str], ...]], tuple[str, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: tuple[str, str, int, tuple[int, ...], tuple[tuple[str, str], ...]]) -> str | None:
        async with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            token, expires_at = item
            if expires_at <= time.monotonic() + _TOKEN_EARLY_RENEW_SECONDS:
                del self._items[key]
                return None
            self._items.move_to_end(key)
            return token

    async def put(self, key: tuple[str, str, int, tuple[int, ...], tuple[tuple[str, str], ...]], token: str, lifetime: int) -> None:
        async with self._lock:
            self._items[key] = (token, time.monotonic() + max(0, lifetime))
            self._items.move_to_end(key)
            while len(self._items) > self.max_entries:
                self._items.popitem(last=False)


token_cache = InstallationTokenCache()


def _app_cache_identity() -> tuple[str, str]:
    """Return App ID plus a one-way key fingerprint for safe cache invalidation."""
    app_id = settings.github_app_id
    private_key = settings.github_app_private_key.replace("\\n", "\n")
    if not app_id or not private_key:
        raise GitHubRepositoryError(503, "GitHub App is not configured")
    fingerprint = hashlib.sha256(private_key.encode("utf-8")).hexdigest()
    return app_id, fingerprint


class GitHubAppClient:
    """GitHub App API client. Repository data calls still use GitHubClient."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self.http = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Solvyn/1.0", "X-GitHub-Api-Version": "2022-11-28"},
        )

    async def __aenter__(self) -> GitHubAppClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        try:
            response = await self.http.request(method, path, json=payload, headers={"Authorization": f"Bearer {app_jwt()}"})
        except httpx.HTTPError as exc:
            raise GitHubRepositoryError(502, "GitHub App request failed") from exc
        if response.status_code in (401, 403, 404):
            raise GitHubRepositoryError(response.status_code, "GitHub App authorization failed")
        if response.status_code == 422:
            raise GitHubRepositoryError(409, "GitHub App installation lacks a requested repository permission")
        if response.status_code == 429:
            raise GitHubRepositoryError(429, "GitHub rate limit reached")
        if response.status_code >= 500:
            raise GitHubRepositoryError(502, "GitHub is temporarily unavailable")
        if not response.is_success:
            raise GitHubRepositoryError(502, "GitHub rejected the App request")
        try:
            return response.json(), response.headers
        except ValueError as exc:
            raise GitHubRepositoryError(502, "GitHub returned invalid JSON") from exc

    async def installation_metadata(self, installation_id: int) -> dict[str, Any]:
        payload, _ = await self._request("GET", f"/app/installations/{installation_id}")
        if not isinstance(payload, dict) or payload.get("id") != installation_id:
            raise GitHubRepositoryError(502, "GitHub returned invalid installation metadata")
        return payload

    async def installation_token(
        self,
        installation_id: int,
        repository_ids: tuple[int, ...],
        permissions: dict[str, str] | None = None,
    ) -> str:
        """Issue/cache an installation token scoped to exact numeric repository IDs."""
        repo_ids = tuple(sorted(set(repository_ids)))
        if installation_id < 1 or not repo_ids or any(repo_id < 1 for repo_id in repo_ids):
            raise ValueError("A positive installation ID and repository IDs are required")
        permission_items = tuple(sorted((permissions or {"contents": "read", "metadata": "read", "pull_requests": "read"}).items()))
        if any(key not in _ALLOWED_PERMISSIONS for key, _ in permission_items):
            raise ValueError("Unsupported GitHub permission")
        if any(value not in {"read", "write"} for _, value in permission_items):
            raise ValueError("Unsupported GitHub permission level")
        if any(level == "write" and permission not in {"contents", "pull_requests"} for permission, level in permission_items):
            raise ValueError("GitHub App write access is not used for this permission")
        app_id, key_fingerprint = _app_cache_identity()
        cache_key = app_id, key_fingerprint, installation_id, repo_ids, permission_items
        cached = await token_cache.get(cache_key)
        if cached:
            return cached
        body, _ = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            payload={"repository_ids": list(repo_ids), "permissions": dict(permission_items)},
        )
        token = body.get("token") if isinstance(body, dict) else None
        expires_at = body.get("expires_at") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token or not isinstance(expires_at, str):
            raise GitHubRepositoryError(502, "GitHub returned an invalid installation token")
        actual_permissions = body.get("permissions") if isinstance(body, dict) else None
        if not isinstance(actual_permissions, dict) or actual_permissions != dict(permission_items):
            raise GitHubRepositoryError(502, "GitHub returned a token with an unexpected permission scope")
        from datetime import datetime
        try:
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        except ValueError as exc:
            raise GitHubRepositoryError(502, "GitHub returned an invalid token expiry") from exc
        await token_cache.put(cache_key, token, int(expiry - time.time()))
        return token

    async def list_installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        """List visible repositories for owner/admin discovery with a temporary broad token."""
        payload, _ = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            payload={"permissions": {"metadata": "read"}},
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        permissions = payload.get("permissions") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise GitHubRepositoryError(502, "GitHub returned an invalid installation token")
        if permissions != {"metadata": "read"}:
            raise GitHubRepositoryError(502, "GitHub returned a discovery token with an unexpected permission scope")
        repos: list[dict[str, Any]] = []
        for page in range(1, 21):
            try:
                response = await self.http.get(
                    "/installation/repositories",
                    params={"per_page": 100, "page": page},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise GitHubRepositoryError(502, "GitHub App request failed") from exc
            if response.status_code == 401:
                raise GitHubRepositoryError(401, "GitHub rejected the installation token")
            if response.status_code == 403:
                raise GitHubRepositoryError(403, "GitHub App cannot list its installed repositories")
            if response.status_code >= 500:
                raise GitHubRepositoryError(502, "GitHub is temporarily unavailable")
            if not response.is_success:
                raise GitHubRepositoryError(502, "GitHub rejected the repository list request")
            try:
                payload = response.json()
            except ValueError as exc:
                raise GitHubRepositoryError(502, "GitHub returned invalid JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("repositories"), list):
                raise GitHubRepositoryError(502, "GitHub returned an invalid repository list")
            repos.extend(item for item in payload["repositories"] if isinstance(item, dict))
            if 'rel="next"' not in response.headers.get("link", ""):
                return repos
        raise GitHubRepositoryError(413, "GitHub installation repository list exceeds the supported page limit")
