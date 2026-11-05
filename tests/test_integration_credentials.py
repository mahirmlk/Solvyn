"""Tests for encrypted integration credential handling."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from solvyn.api.v1.integrations import GitHubCredentialInput, connect_github_integration
from solvyn.db.models import Integration, IntegrationCredential
from solvyn.repositories.credentials import CredentialEncryptionUnavailableError, IntegrationCredentialVault
from solvyn.repositories.github import GitHubClient


def test_credential_vault_encrypts_and_supports_rotation() -> None:
    old_key = Fernet.generate_key().decode("ascii")
    new_key = Fernet.generate_key().decode("ascii")
    old_vault = IntegrationCredentialVault(old_key)
    old_ciphertext = old_vault.encrypt("ghp-sensitive-token")

    rotating_vault = IntegrationCredentialVault(f"{new_key},{old_key}")
    new_ciphertext = rotating_vault.encrypt("ghp-sensitive-token")

    assert old_ciphertext != b"ghp-sensitive-token"
    assert rotating_vault.decrypt(old_ciphertext) == "ghp-sensitive-token"
    assert rotating_vault.decrypt(new_ciphertext) == "ghp-sensitive-token"
    assert IntegrationCredentialVault(new_key).decrypt(new_ciphertext) == "ghp-sensitive-token"


def test_credential_vault_fails_closed_without_a_key() -> None:
    with pytest.raises(CredentialEncryptionUnavailableError):
        IntegrationCredentialVault("")
    with pytest.raises(CredentialEncryptionUnavailableError):
        IntegrationCredentialVault("not-a-fernet-key")


@pytest.mark.asyncio
async def test_github_client_validates_token_and_returns_only_account_identity() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user"
        assert request.headers["authorization"] == "Bearer ghp-sensitive-token"
        return httpx.Response(200, json={"id": 123, "login": "octocat"})

    http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    async with GitHubClient(http, token="ghp-sensitive-token") as github:
        identity = await github.authenticated_identity()
    assert "authorization" not in http.headers
    await http.aclose()

    assert identity == {"login": "octocat", "account_id": "123"}


@pytest.mark.asyncio
async def test_connect_github_fails_closed_when_encryption_is_not_configured() -> None:
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock),
        patch(
            "solvyn.api.v1.integrations.create_credential_vault",
            side_effect=CredentialEncryptionUnavailableError("missing key"),
        ),
        patch("solvyn.api.v1.integrations.GitHubClient") as github_client,
        pytest.raises(HTTPException) as exc,
    ):
        await connect_github_integration(
            organization_id=uuid.uuid4(),
            payload=GitHubCredentialInput(access_token="ghp-sensitive-token-123"),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 503
    github_client.assert_not_called()


@pytest.mark.asyncio
async def test_connect_github_persists_only_ciphertext_and_never_returns_token() -> None:
    from types import SimpleNamespace

    class StubDB:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def execute(self, _query: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, value: object) -> None:
            if isinstance(value, Integration):
                value.id = uuid.uuid4()
            self.added.append(value)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            return None

        async def refresh(self, _value: object) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class StubGitHubClient:
        def __init__(self, *, token: str) -> None:
            assert token == "ghp-sensitive-token-123"

        async def __aenter__(self) -> StubGitHubClient:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def authenticated_identity(self) -> dict[str, str]:
            return {"login": "octocat", "account_id": "123"}

    database = StubDB()
    vault = SimpleNamespace(
        encrypt=lambda token: b"encrypted-ciphertext" if token == "ghp-sensitive-token-123" else b""
    )
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock),
        patch("solvyn.api.v1.integrations.create_credential_vault", return_value=vault),
        patch("solvyn.api.v1.integrations.GitHubClient", StubGitHubClient),
    ):
        response = await connect_github_integration(
            organization_id=uuid.uuid4(),
            payload=GitHubCredentialInput(access_token="ghp-sensitive-token-123"),
            db=database,  # type: ignore[arg-type]
        )

    stored = next(value for value in database.added if isinstance(value, IntegrationCredential))
    assert stored.ciphertext == b"encrypted-ciphertext"
    assert "ghp-sensitive-token-123" not in repr(response)
    assert response["account_login"] == "octocat"
