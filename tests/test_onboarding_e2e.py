"""HTTP onboarding acceptance coverage with a mocked public repository provider.

The test exercises the application routes and database persistence for account
registration/login, organization setup, project provisioning, teammate
assignment, and repository connection. It does not call a live GitHub API or
exercise a model, sandbox, agent execution, or pull request delivery.
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.e2e


class FakePublicRepositoryProvider:
    """Deterministic public-repository metadata; no GitHub network calls."""

    def __init__(self) -> None:
        self.requested_repositories: list[str] = []

    async def __aenter__(self) -> FakePublicRepositoryProvider:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def repository_metadata(self, full_name: str) -> dict[str, Any]:
        self.requested_repositories.append(full_name)
        return {
            "id": int(uuid.uuid4().hex[:15], 16),
            "full_name": "octocat/hello-world",
            "description": "Public onboarding fixture repository",
            "default_branch": "main",
            "url": "https://github.com/octocat/hello-world",
            "private": False,
        }


@pytest.mark.asyncio
async def test_http_user_onboarding_persists_workspace_project_teammate_and_public_repository(
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run HTTP onboarding with fake GitHub metadata and no fake execution result."""
    from solvyn.api.v1 import repositories as repositories_api
    from solvyn.config import settings
    from solvyn.db import session as db_session
    from solvyn.db.session import engine as application_engine
    from solvyn.db.session import get_session

    if application_engine.dialect.name != "postgresql":
        pytest.skip("onboarding HTTP E2E requires PostgreSQL and tenant RLS")

    database_url = application_engine.url
    try:
        with socket.create_connection((database_url.host or "localhost", database_url.port or 5432), timeout=2):
            pass
    except OSError as exc:
        message = f"PostgreSQL test database is not reachable: {type(exc).__name__}"
        if os.environ.get("CI", "").lower() in {"true", "1"}:
            pytest.fail(message)
        pytest.skip(message)

    # Use a private engine with a bounded connect timeout. This connection is
    # opened before the app lifespan so an unavailable local database skips
    # without hanging during startup.
    test_engine = create_async_engine(
        database_url,
        connect_args={"timeout": 3},
        pool_pre_ping=False,
    )
    try:
        connection = await test_engine.connect()
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        await test_engine.dispose()
        message = f"PostgreSQL test database is not reachable: {type(exc).__name__}"
        if os.environ.get("CI", "").lower() in {"true", "1"}:
            pytest.fail(message)
        pytest.skip(message)

    # Run middleware queries and route dependencies on one connection inside
    # an outer transaction. Route commits become savepoints and are rolled back
    # at the end, so the test leaves no user, tenant, or onboarding rows behind.
    outer_transaction = await connection.begin()
    transactional_sessions = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )

    monkeypatch.setattr(db_session, "async_session_factory", transactional_sessions)
    from solvyn.multi_tenant import auth as tenant_auth
    from solvyn.multi_tenant import middleware as tenant_middleware

    monkeypatch.setattr(tenant_auth, "async_session_factory", transactional_sessions)
    monkeypatch.setattr(tenant_middleware, "async_session_factory", transactional_sessions)

    async def transactional_get_session() -> AsyncIterator[AsyncSession]:
        async with transactional_sessions() as session:
            yield session

    app.dependency_overrides[get_session] = transactional_get_session
    lifespan = LifespanManager(app)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    lifespan_started = False
    client_started = False

    monkeypatch.setattr(settings, "allow_public_registration", True)
    provider = FakePublicRepositoryProvider()
    monkeypatch.setattr(
        repositories_api,
        "get_repository_provider",
        lambda _name, *, token=None: provider,
    )

    email = f"onboarding-{uuid.uuid4().hex}@example.com"
    password = "correct-horse-battery"
    try:
        # Start the app only after the PostgreSQL connection has been checked
        # and all database session paths have been redirected to its savepoint.
        await lifespan.__aenter__()
        lifespan_started = True
        await client.__aenter__()
        client_started = True

        registered = await client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": "Onboarding Test"},
        )
        assert registered.status_code == 201, registered.text
        user_id = uuid.UUID(registered.json()["id"])
        tenant_slug = f"user-{user_id.hex}"
        expected_tenant_id = await connection.scalar(
            text("SELECT id FROM tenants WHERE slug = :tenant_slug"), {"tenant_slug": tenant_slug}
        )
        assert expected_tenant_id is not None

        # Registration binds its newly created tenant while writing the initial
        # organization. Clear that transaction-local value before the following
        # public requests on this deliberately shared connection.
        await connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))

        other_email = f"onboarding-other-{uuid.uuid4().hex}@example.com"
        other_registered = await client.post(
            "/api/v1/auth/register",
            json={"email": other_email, "password": password, "display_name": "Other Onboarding Test"},
        )
        assert other_registered.status_code == 201, other_registered.text
        other_user_id = uuid.UUID(other_registered.json()["id"])
        other_tenant_slug = f"user-{other_user_id.hex}"
        other_tenant_id = await connection.scalar(
            text("SELECT id FROM tenants WHERE slug = :tenant_slug"), {"tenant_slug": other_tenant_slug}
        )
        assert other_tenant_id is not None
        await connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))

        login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        assert login.status_code == 200, login.text
        login_scope = await connection.scalar(text("SELECT current_setting('app.tenant_id', true)"))
        assert login_scope == ""

        access_token = login.json()["access_token"]
        client.headers["Authorization"] = f"Bearer {access_token}"
        client.headers["X-Tenant-Slug"] = tenant_slug

        # A second, real tenant slug is rejected for this user. The next request
        # must rebind the reused connection to the user's own tenant.
        wrong_tenant = await client.get(
            "/api/v1/organizations", headers={"X-Tenant-Slug": other_tenant_slug}
        )
        assert wrong_tenant.status_code == 403, wrong_tenant.text
        wrong_tenant_scope = await connection.scalar(text("SELECT current_setting('app.tenant_id', true)"))
        assert wrong_tenant_scope == ""

        initial_organizations = await client.get("/api/v1/organizations")
        assert initial_organizations.status_code == 200, initial_organizations.text
        assert initial_organizations.json()["count"] == 1

        organization = await client.post(
            "/api/v1/organizations",
            json={"name": "Onboarding Workspace Org", "slug": f"onboarding-{uuid.uuid4().hex[:12]}"},
        )
        assert organization.status_code == 201, organization.text
        organization_id = organization.json()["id"]
        committed_request_scope = await connection.scalar(text("SELECT current_setting('app.tenant_id', true)"))
        assert committed_request_scope == str(expected_tenant_id)

        workspace = await client.post(
            "/api/v1/workspaces",
            json={
                "organization_id": organization_id,
                "name": "Product Engineering",
                "slug": f"engineering-{uuid.uuid4().hex[:10]}",
            },
        )
        assert workspace.status_code == 201, workspace.text
        workspace_id = workspace.json()["id"]
        assert workspace.json()["organization_id"] == organization_id

        project = await client.post(
            "/api/v1/projects",
            json={
                "organization_id": organization_id,
                "workspace_id": workspace_id,
                "name": "Hello World Service",
                "slug": f"hello-world-{uuid.uuid4().hex[:10]}",
                "description": "Onboarding route coverage",
            },
        )
        assert project.status_code == 201, project.text
        project_id = project.json()["id"]
        assert project.json()["workspace_id"] == workspace_id

        teammate = await client.post(
            "/api/v1/teammates",
            json={
                "organization_id": organization_id,
                "name": "Repository Engineer",
                "role": "Software Engineer",
                "description": "Works on assigned project tasks.",
                "project_ids": [project_id],
                "permissions": ["READ_ONLY", "MODIFY_FILES", "EXECUTE_COMMANDS"],
            },
        )
        assert teammate.status_code == 201, teammate.text
        teammate_id = teammate.json()["id"]
        assert teammate.json()["agent_type"] == "backend-engineer"

        organization_teammates = await client.get(f"/api/v1/organizations/{organization_id}/teammates")
        assert organization_teammates.status_code == 200, organization_teammates.text
        assert [entry["id"] for entry in organization_teammates.json()["teammates"]] == [teammate_id]

        repository = await client.post(
            f"/api/v1/projects/{project_id}/repositories",
            json={"full_name": "octocat/hello-world"},
        )
        assert repository.status_code == 201, repository.text
        repository_id = repository.json()["id"]
        assert repository.json()["project_id"] == project_id
        assert repository.json()["full_name"] == "octocat/hello-world"
        assert repository.json()["default_branch"] == "main"
        assert provider.requested_repositories == ["octocat/hello-world"]

        repositories = await client.get(f"/api/v1/projects/{project_id}/repositories")
        assert repositories.status_code == 200, repositories.text
        assert repositories.json()["count"] == 1
        assert repositories.json()["repositories"][0]["id"] == repository_id
    finally:
        if client_started:
            await client.__aexit__(None, None, None)
        if lifespan_started:
            await lifespan.__aexit__(None, None, None)
        app.dependency_overrides.pop(get_session, None)
        if outer_transaction.is_active:
            await outer_transaction.rollback()
        await connection.close()
        await test_engine.dispose()


