from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from solvyn.api.v1 import repositories as repository_routes


@pytest.mark.asyncio
async def test_repository_discovery_is_admin_scoped_and_marks_project_duplicates() -> None:
    tenant_id, organization_id, project_id = (uuid.uuid4() for _ in range(3))
    project = SimpleNamespace(
        id=project_id,
        tenant_id=tenant_id,
        organization_id=organization_id,
        is_active=True,
    )
    connected_result = MagicMock()
    connected_result.scalars.return_value.all.return_value = ["acme/private-app"]
    db = AsyncMock()
    db.get.return_value = project
    db.execute.return_value = connected_result
    token = "never-return-this-token"
    candidates = [
        {
            "id": 41,
            "full_name": "acme/private-app",
            "default_branch": "main",
            "url": "https://github.com/acme/private-app",
            "private": True,
        },
        {
            "id": 42,
            "full_name": "acme/empty-app",
            "default_branch": "",
            "url": "https://github.com/acme/empty-app",
            "private": False,
        },
    ]
    github = SimpleNamespace(accessible_repositories=AsyncMock(return_value=candidates))

    class GitHubContext:
        def __init__(self, *, token: str) -> None:
            assert token == "never-return-this-token"

        async def __aenter__(self) -> SimpleNamespace:
            return github

        async def __aexit__(self, *_args: object) -> None:
            return None

    with (
        patch.object(repository_routes, "require_tenant", return_value=tenant_id),
        patch.object(repository_routes, "require_org_membership", new_callable=AsyncMock) as membership,
        patch.object(
            repository_routes,
            "load_organization_github_connection",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(id=uuid.uuid4()), token),
        ),
        patch.object(repository_routes, "GitHubClient", GitHubContext),
    ):
        result = await repository_routes.list_repository_candidates(project_id, page=2, db=db)

    membership.assert_awaited_once_with(db, organization_id, roles=repository_routes.OWNER_ADMIN)
    github.accessible_repositories.assert_awaited_once_with(page=2)
    assert result["page"] == 2
    assert result["has_more"] is False
    assert result["repositories"] == [
        {**candidates[0], "is_connected": True, "connectable": True},
        {**candidates[1], "is_connected": False, "connectable": False},
    ]
    assert token not in repr(result)


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
        patch.object(repository_routes, "load_organization_github_connection", new_callable=AsyncMock) as credentials,
        pytest.raises(HTTPException) as error,
    ):
        await repository_routes.list_repository_candidates(project_id, db=db)

    assert error.value.status_code == 404
    credentials.assert_not_awaited()


@pytest.mark.asyncio
async def test_repository_discovery_does_not_offer_a_page_beyond_the_cap() -> None:
    tenant_id, organization_id, project_id = (uuid.uuid4() for _ in range(3))
    project = SimpleNamespace(id=project_id, tenant_id=tenant_id, organization_id=organization_id, is_active=True)
    db = AsyncMock()
    db.get.return_value = project
    connected_result = MagicMock()
    connected_result.scalars.return_value.all.return_value = []
    db.execute.return_value = connected_result
    token = "credential-stays-private"
    candidates = [
        {
            "id": index,
            "full_name": f"acme/repository-{index}",
            "default_branch": "main",
            "url": f"https://github.com/acme/repository-{index}",
            "private": False,
        }
        for index in range(50)
    ]
    github = SimpleNamespace(accessible_repositories=AsyncMock(return_value=candidates))

    class GitHubContext:
        def __init__(self, *, token: str) -> None:
            assert token == "credential-stays-private"

        async def __aenter__(self) -> SimpleNamespace:
            return github

        async def __aexit__(self, *_args: object) -> None:
            return None

    with (
        patch.object(repository_routes, "require_tenant", return_value=tenant_id),
        patch.object(repository_routes, "require_org_membership", new_callable=AsyncMock),
        patch.object(
            repository_routes,
            "load_organization_github_connection",
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(id=uuid.uuid4()), token),
        ),
        patch.object(repository_routes, "GitHubClient", GitHubContext),
    ):
        result = await repository_routes.list_repository_candidates(project_id, page=20, db=db)

    assert result["page"] == 20
    assert result["per_page"] == 50
    assert result["has_more"] is False
