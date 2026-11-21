from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from solvyn.api.v1 import repositories as repository_routes


def _result(items: list[object]) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = items
    return result


class _AppContext:
    def __init__(self, candidates: list[dict[str, object]]) -> None:
        self.candidates = candidates

    async def __aenter__(self) -> _AppContext:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def list_installation_repositories(self, installation_id: int) -> list[dict[str, object]]:
        assert installation_id == 812
        return self.candidates


@pytest.mark.asyncio
async def test_repository_discovery_uses_installation_and_marks_project_duplicates() -> None:
    tenant_id, organization_id, project_id = (uuid.uuid4() for _ in range(3))
    project = SimpleNamespace(id=project_id, tenant_id=tenant_id, organization_id=organization_id, is_active=True)
    integration = SimpleNamespace(github_installation_id=812)
    connected_result = _result(["acme/private-app"])
    db = AsyncMock()
    db.get.return_value = project
    db.execute.side_effect = [_result([integration]), connected_result]
    candidates = [
        {"id": 41, "full_name": "acme/private-app", "default_branch": "main", "html_url": "https://github.com/acme/private-app", "private": True},
        {"id": 42, "full_name": "acme/empty-app", "default_branch": "", "html_url": "https://github.com/acme/empty-app", "private": False},
    ]
    with (
        patch.object(repository_routes, "require_tenant", return_value=tenant_id),
        patch.object(repository_routes, "require_org_membership", new_callable=AsyncMock) as membership,
        patch.object(repository_routes, "GitHubAppClient", lambda: _AppContext(candidates)),
    ):
        result = await repository_routes.list_repository_candidates(project_id, page=1, db=db)

    membership.assert_awaited_once_with(db, organization_id, roles=repository_routes.OWNER_ADMIN)
    assert result["page"] == 1
    assert result["has_more"] is False
    assert result["possibly_truncated"] is False
    assert result["repositories"] == [
        {"id": 41, "full_name": "acme/private-app", "default_branch": "main", "private": True,
         "url": "https://github.com/acme/private-app", "installation_id": 812, "is_connected": True, "connectable": True},
        {"id": 42, "full_name": "acme/empty-app", "default_branch": "", "private": False,
         "url": "https://github.com/acme/empty-app", "installation_id": 812, "is_connected": False, "connectable": False},
    ]


@pytest.mark.asyncio
async def test_repository_discovery_hides_projects_from_other_tenants() -> None:
    project_id = uuid.uuid4()
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(
        id=project_id,
        tenant_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        is_active=True,
    )
    with (
        patch.object(repository_routes, "require_tenant", return_value=uuid.uuid4()),
        pytest.raises(HTTPException) as error,
    ):
        await repository_routes.list_repository_candidates(project_id, db=db)

    assert error.value.status_code == 404
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_discovery_does_not_offer_page_beyond_cap() -> None:
    tenant_id, organization_id, project_id = (uuid.uuid4() for _ in range(3))
    project = SimpleNamespace(id=project_id, tenant_id=tenant_id, organization_id=organization_id, is_active=True)
    db = AsyncMock()
    db.get.return_value = project
    db.execute.side_effect = [_result([SimpleNamespace(github_installation_id=812)]), _result([])]
    candidates = [
        {"id": index + 1, "full_name": f"acme/repository-{index}", "default_branch": "main", "html_url": "", "private": False}
        for index in range(1001)
    ]
    with (
        patch.object(repository_routes, "require_tenant", return_value=tenant_id),
        patch.object(repository_routes, "require_org_membership", new_callable=AsyncMock),
        patch.object(repository_routes, "GitHubAppClient", lambda: _AppContext(candidates)),
    ):
        result = await repository_routes.list_repository_candidates(project_id, page=20, db=db)

    assert result["page"] == 20
    assert result["per_page"] == 50
    assert len(result["repositories"]) == 50
    assert result["has_more"] is False
    assert result["possibly_truncated"] is True
