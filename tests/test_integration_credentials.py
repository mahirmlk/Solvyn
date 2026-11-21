"""Tests for encrypted integration credential handling."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from solvyn.api.v1.integrations import connect_github_integration
from solvyn.db.models import Integration
from solvyn.repositories.credentials import CredentialEncryptionUnavailableError, IntegrationCredentialVault
from solvyn.repositories.github import GitHubClient


@pytest.mark.asyncio
async def test_repository_provider_rejects_legacy_organization_tokens() -> None:
    from solvyn.repositories.providers import get_authorized_repository_provider

    organization_id = uuid.uuid4()
    integration_id = uuid.uuid4()
    integration = Integration(
        id=integration_id,
        organization_id=organization_id,
        provider="github",
        external_id="organization-token",
        is_active=True,
    )

    class StubDB:
        async def get(self, model, _integration_id):
            assert model is Integration
            return integration

    from solvyn.repositories.credentials import GitHubCredentialNotFoundError

    with pytest.raises(GitHubCredentialNotFoundError):
        await get_authorized_repository_provider(
            "github",
            StubDB(),  # type: ignore[arg-type]
            SimpleNamespace(integration_id=integration_id, github_repository_id=42),  # type: ignore[arg-type]
            organization_id,
        )



@pytest.mark.asyncio
async def test_repository_provider_rejects_a_credential_linked_to_another_organization() -> None:
    from solvyn.repositories.credentials import GitHubCredentialNotFoundError
    from solvyn.repositories.providers import get_authorized_repository_provider

    integration = Integration(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="github",
        external_id="organization-token",
        is_active=True,
    )

    class StubDB:
        async def get(self, _model, _integration_id):
            return integration

    with pytest.raises(GitHubCredentialNotFoundError):
        await get_authorized_repository_provider(
            "github",
            StubDB(),  # type: ignore[arg-type]
            SimpleNamespace(integration_id=integration.id),  # type: ignore[arg-type]
            uuid.uuid4(),
        )



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
async def test_connect_github_pat_endpoint_is_retired() -> None:
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc,
    ):
        await connect_github_integration(
            organization_id=uuid.uuid4(),
            db=AsyncMock(),
        )

    assert exc.value.status_code == 410


@pytest.mark.asyncio
async def test_connect_github_pat_route_requires_owner_even_when_retired() -> None:
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock) as membership,
        pytest.raises(HTTPException) as exc,
    ):
        await connect_github_integration(organization_id=uuid.uuid4(), db=AsyncMock())

    assert exc.value.status_code == 410
    assert membership.await_args.kwargs["roles"]
