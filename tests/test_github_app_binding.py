from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from solvyn.api.v1 import verification as verification_routes
from solvyn.api.v1.integrations import (
    GitHubInstallationInput,
    connect_github_installation,
    disconnect_github_integration,
)
from solvyn.db.models import Integration
from solvyn.repositories.credentials import GitHubCredentialNotFoundError
from solvyn.repositories.providers import require_github_app_repository_binding


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
async def test_binding_removes_legacy_pat_ciphertext_and_reports_github_revocation() -> None:
    org_id = uuid.uuid4()
    legacy_integration = Integration(
        id=uuid.uuid4(), organization_id=org_id, provider="github", external_id="organization-token", is_active=True,
    )
    credential = SimpleNamespace(integration_id=legacy_integration.id, ciphertext=b"encrypted")

    class MigrationDB(_DB):
        def __init__(self) -> None:
            super().__init__()
            self.queries = 0
            self.deleted: list[object] = []

        async def execute(self, _query: object) -> SimpleNamespace:
            self.queries += 1
            value = legacy_integration if self.queries == 2 else credential if self.queries == 3 else None
            return SimpleNamespace(scalar_one_or_none=lambda: value)

        async def delete(self, value: object) -> None:
            self.deleted.append(value)

    db = MigrationDB()
    metadata = {"id": 812, "account": {"id": 456, "login": "Acme", "type": "Organization"}}
    with (
        patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock),
        patch("solvyn.api.v1.integrations.GitHubAppClient", lambda: _AppContext(metadata)),
    ):
        response = await connect_github_installation(org_id, GitHubInstallationInput(installation_id=812), db)  # type: ignore[arg-type]

    assert not legacy_integration.is_active
    assert db.deleted == [credential]
    assert response["legacy_token_removed"] is True
    assert response["revoke_previous_personal_access_token_in_github"] is True


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


@pytest.mark.parametrize("failure", ["disconnected", "cross_org", "wrong_provider"])
def test_repository_installation_binding_fails_closed_for_invalid_integration(failure: str) -> None:
    org_id = uuid.uuid4()
    integration = Integration(
        id=uuid.uuid4(),
        organization_id=org_id,
        provider="github",
        external_id="installation:812",
        github_installation_id=812,
        github_account_id=456,
        is_active=True,
    )
    repository = SimpleNamespace(github_repository_id=41)
    if failure == "disconnected":
        integration.is_active = False
    elif failure == "cross_org":
        integration.organization_id = uuid.uuid4()
    else:
        integration.provider = "gitlab"

    with pytest.raises(GitHubCredentialNotFoundError):
        require_github_app_repository_binding(integration, repository, org_id)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_disconnect_preserves_public_api_and_deactivates_only_secret_integrations() -> None:
    org_id = uuid.uuid4()
    app_integration = Integration(
        id=uuid.uuid4(), organization_id=org_id, provider="github", external_id="installation:812",
        github_installation_id=812, github_account_id=456, is_active=True,
    )
    legacy_integration = Integration(
        id=uuid.uuid4(), organization_id=org_id, provider="github", external_id="organization-token", is_active=True,
    )
    public_integration = Integration(
        id=uuid.uuid4(), organization_id=org_id, provider="github", external_id="public-api", is_active=True,
    )
    credential = SimpleNamespace(integration_id=legacy_integration.id)

    class DisconnectDB:
        def __init__(self) -> None:
            self.deleted: list[object] = []
            self.queries = 0

        async def execute(self, _query: object) -> SimpleNamespace:
            self.queries += 1
            if self.queries == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [app_integration, legacy_integration]))
            if self.queries == 2:
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            return SimpleNamespace(scalar_one_or_none=lambda: credential)

        async def delete(self, row: object) -> None:
            self.deleted.append(row)

        async def commit(self) -> None:
            return None

    db = DisconnectDB()
    with patch("solvyn.api.v1.integrations.require_org_membership", new_callable=AsyncMock):
        await disconnect_github_integration(org_id, db)  # type: ignore[arg-type]

    assert not app_integration.is_active
    assert not legacy_integration.is_active
    assert public_integration.is_active
    assert db.deleted == [credential]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disconnected", "cross_org"])
async def test_task_delivery_refuses_inactive_or_cross_org_installations(failure: str) -> None:
    org_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    integration = Integration(
        id=uuid.uuid4(), organization_id=org_id, provider="github", external_id="installation:812",
        github_installation_id=812, github_account_id=456, is_active=True,
    )
    if failure == "disconnected":
        integration.is_active = False
    else:
        integration.organization_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id, tenant_id=uuid.uuid4())
    workspace = SimpleNamespace(repository_id=uuid.uuid4(), source_commit_sha="source-sha")
    handoff = SimpleNamespace(status="completed", project_id=project_id)
    repository = SimpleNamespace(integration_id=integration.id, github_repository_id=41)
    project = SimpleNamespace(id=project_id, organization_id=org_id)
    db = AsyncMock()
    db.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: workspace),
        SimpleNamespace(scalar_one_or_none=lambda: handoff),
    ]
    db.get.return_value = integration
    with (
        patch.object(verification_routes, "_owned_task", new_callable=AsyncMock, return_value=task),
        patch.object(verification_routes, "_load_authorized_repository", new_callable=AsyncMock, return_value=(repository, project)),
        patch.object(verification_routes, "GitHubAppClient") as app_client,
        pytest.raises(HTTPException) as error,
    ):
        await verification_routes._task_delivery_context(uuid.uuid4(), task_id, db)

    assert error.value.status_code == 409
    app_client.assert_not_called()
