from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from solvyn.api.v1.integrations import GitHubInstallationInput, connect_github_installation
from solvyn.db.models import Integration


class _AppContext:
    def __init__(self, metadata: dict[str, object]) -> None:
        self.metadata = metadata

    async def __aenter__(self) -> _AppContext:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def installation_metadata(self, installation_id: int) -> dict[str, object]:
        assert installation_id == 812
        return self.metadata


class _DB:
    def __init__(self, existing: Integration | None = None) -> None:
        self.existing = existing
        self.added: list[Integration] = []

    async def execute(self, _query: object) -> SimpleNamespace:
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, integration: Integration) -> None:
        integration.id = uuid.uuid4()
        self.added.append(integration)

    async def commit(self) -> None:
        return None

    async def refresh(self, _integration: Integration) -> None:
        return None

    async def rollback(self) -> None:
        return None


@pytest.mark.asyncio
async def test_installation_binding_verifies_and_persists_app_account_identity() -> None:
    org_id = uuid.uuid4()
    db = _DB()
    metadata = {"id": 812, "account": {"id": 456, "login": "Acme", "type": "Organization"}}
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock) as membership,
        patch("solvyn.api.v1.integrations.GitHubAppClient", lambda: _AppContext(metadata)),
    ):
        response = await connect_github_installation(org_id, GitHubInstallationInput(installation_id=812), db)  # type: ignore[arg-type]

    membership.assert_awaited_once_with(db, org_id, roles=("owner", "admin"))
    integration = db.added[0]
    assert response["connected"] is True
    assert integration.organization_id == org_id
    assert integration.github_installation_id == 812
    assert integration.github_account_id == 456
    assert integration.github_account_type == "Organization"


@pytest.mark.asyncio
async def test_installation_binding_rejects_installation_already_owned_by_another_org() -> None:
    org_id = uuid.uuid4()
    existing = Integration(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        provider="github",
        external_id="installation:812",
        github_installation_id=812,
        github_account_id=456,
        is_active=True,
    )
    db = _DB(existing)
    metadata = {"id": 812, "account": {"id": 456, "login": "Acme", "type": "Organization"}}
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock),
        patch("solvyn.api.v1.integrations.GitHubAppClient", lambda: _AppContext(metadata)),
        pytest.raises(HTTPException) as error,
    ):
        await connect_github_installation(org_id, GitHubInstallationInput(installation_id=812), db)  # type: ignore[arg-type]

    assert error.value.status_code == 409
    assert not db.added


@pytest.mark.asyncio
async def test_installation_binding_rejects_mismatched_metadata_id() -> None:
    org_id = uuid.uuid4()
    db = _DB()
    metadata = {"id": 813, "account": {"id": 456, "login": "Acme", "type": "Organization"}}
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock),
        patch("solvyn.api.v1.integrations.GitHubAppClient", lambda: _AppContext(metadata)),
        pytest.raises(HTTPException) as error,
    ):
        await connect_github_installation(org_id, GitHubInstallationInput(installation_id=812), db)  # type: ignore[arg-type]

    assert error.value.status_code == 502
    assert not db.added
