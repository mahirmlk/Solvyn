from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from solvyn.repositories import github_app
from solvyn.repositories.github_app import GitHubAppClient, InstallationTokenCache


@pytest.mark.asyncio
async def test_installation_token_is_scoped_to_exact_numeric_repositories(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            201,
            json={
                "token": "installation-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=50)).isoformat(),
                "permissions": {"contents": "read", "metadata": "read"},
            },
        )

    monkeypatch.setattr(github_app, "app_jwt", lambda: "signed-app-jwt")
    monkeypatch.setattr(github_app.settings, "github_app_id", "12345")
    monkeypatch.setattr(github_app.settings, "github_app_private_key", "fake-pem")
    monkeypatch.setattr(github_app, "token_cache", InstallationTokenCache())
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as http:
        async with GitHubAppClient(http) as app:
            token = await app.installation_token(
                812,
                (41, 42, 41),
                {"contents": "read", "metadata": "read"},
            )
            cached = await app.installation_token(
                812,
                (42, 41),
                {"metadata": "read", "contents": "read"},
            )

    assert token == cached == "installation-token"
    assert len(calls) == 1
    assert calls[0].url.path == "/app/installations/812/access_tokens"
    assert calls[0].headers["Authorization"] == "Bearer signed-app-jwt"
    assert json.loads(calls[0].content) == {
        "repository_ids": [41, 42],
        "permissions": {"contents": "read", "metadata": "read"},
    }


@pytest.mark.asyncio
async def test_installation_token_rejects_unscoped_requests() -> None:
    async with GitHubAppClient(httpx.AsyncClient(base_url="https://api.github.com")) as app:
        with pytest.raises(ValueError, match="repository IDs"):
            await app.installation_token(812, ())


@pytest.mark.asyncio
async def test_unexpected_token_permissions_are_never_cached_or_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            201,
            json={
                "token": "overbroad-token",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=50)).isoformat(),
                "permissions": {"contents": "write", "metadata": "read"},
            },
        )

    monkeypatch.setattr(github_app, "app_jwt", lambda: "signed-app-jwt")
    monkeypatch.setattr(github_app.settings, "github_app_id", "12345")
    monkeypatch.setattr(github_app.settings, "github_app_private_key", "fake-pem")
    monkeypatch.setattr(github_app, "token_cache", InstallationTokenCache())
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as http:
        async with GitHubAppClient(http) as app:
            with pytest.raises(github_app.GitHubRepositoryError, match="unexpected permission scope"):
                await app.installation_token(812, (41,), {"contents": "read", "metadata": "read"})


@pytest.mark.asyncio
async def test_app_id_and_private_key_rotation_invalidate_token_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            201,
            json={
                "token": f"installation-token-{calls}",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=50)).isoformat(),
                "permissions": {"contents": "read", "metadata": "read"},
            },
        )

    monkeypatch.setattr(github_app, "app_jwt", lambda: "signed-app-jwt")
    monkeypatch.setattr(github_app.settings, "github_app_id", "12345")
    monkeypatch.setattr(github_app.settings, "github_app_private_key", "first-key")
    monkeypatch.setattr(github_app, "token_cache", InstallationTokenCache())
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as http:
        async with GitHubAppClient(http) as app:
            first = await app.installation_token(812, (41,), {"contents": "read", "metadata": "read"})
            same = await app.installation_token(812, (41,), {"contents": "read", "metadata": "read"})
            monkeypatch.setattr(github_app.settings, "github_app_private_key", "rotated-key")
            rotated_key = await app.installation_token(812, (41,), {"contents": "read", "metadata": "read"})
            monkeypatch.setattr(github_app.settings, "github_app_id", "67890")
            rotated_id = await app.installation_token(812, (41,), {"contents": "read", "metadata": "read"})

    assert (first, same, rotated_key, rotated_id) == (
        "installation-token-1",
        "installation-token-1",
        "installation-token-2",
        "installation-token-3",
    )
    assert calls == 3


@pytest.mark.asyncio
async def test_repository_discovery_token_has_only_metadata_read(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"token": "discovery-token", "permissions": {"metadata": "read"}})
        return httpx.Response(200, json={"repositories": []})

    monkeypatch.setattr(github_app, "app_jwt", lambda: "signed-app-jwt")
    async with httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler)) as http:
        async with GitHubAppClient(http) as app:
            assert await app.list_installation_repositories(812) == []

    assert requests[0].url.path == "/app/installations/812/access_tokens"
    assert json.loads(requests[0].content) == {"permissions": {"metadata": "read"}}
    assert requests[1].url.path == "/installation/repositories"
    assert requests[1].headers["Authorization"] == "Bearer discovery-token"


def test_app_jwt_uses_short_lived_rs256_claims(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
    token = github_app.app_jwt(app_id="12345", private_key=pem, now=1_800_000_000)
    header, claims, signature = token.split(".")
    decoded_header = json.loads(github_app.base64.urlsafe_b64decode(header + "=="))
    decoded_claims = json.loads(github_app.base64.urlsafe_b64decode(claims + "=="))
    assert decoded_header == {"alg": "RS256", "typ": "JWT"}
    assert decoded_claims == {"iat": 1_799_999_970, "exp": 1_800_000_480, "iss": "12345"}
    assert signature
