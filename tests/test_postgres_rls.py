"""PostgreSQL-only checks for the initial tenant RLS boundary."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from solvyn.db.models import TaskAcceptanceCriterion, TaskWorkspace, TeammateToolInvocation
from solvyn.multi_tenant.middleware import tenant_context


@pytest.mark.asyncio
async def test_acceptance_criteria_rls_is_tenant_scoped_and_transaction_local() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    criterion_a, criterion_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert role == (False, False)
            protected_tables = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('task_acceptance_criteria', 'task_deliveries', 'task_workspaces', "
                        "'teammate_tool_invocations')"
                    )
                )
            ).all()
            assert {row[0] for row in protected_tables} == {
                "task_acceptance_criteria",
                "task_deliveries",
                "task_workspaces",
                "teammate_tool_invocations",
            }
            assert all(row[1] and row[2] for row in protected_tables)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS A', :slug, '')"),
                {"id": tenant_a, "slug": f"rls-{tenant_a}"},
            )
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS B', :slug, '')"),
                {"id": tenant_b, "slug": f"rls-{tenant_b}"},
            )
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS task')"),
                [{"id": task_a, "tenant_id": tenant_a}, {"id": task_b, "tenant_id": tenant_b}],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO task_acceptance_criteria (id, tenant_id, task_id, text) VALUES (:id, :tenant_id, :task_id, 'criterion')"),
                {"id": criterion_a, "tenant_id": tenant_a, "task_id": task_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO task_acceptance_criteria (id, tenant_id, task_id, text) VALUES (:id, :tenant_id, :task_id, 'criterion')"),
                {"id": criterion_b, "tenant_id": tenant_b, "task_id": task_b},
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskAcceptanceCriterion.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            visible = (await session.execute(select(TaskAcceptanceCriterion.id))).scalars().all()
            assert visible == [criterion_a]
            session.add(
                TaskAcceptanceCriterion(
                    id=uuid.uuid4(), tenant_id=tenant_b, task_id=task_b, text="cross-tenant insert"
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        # The same pooled connection must not retain tenant A after its
        # transaction ends and context is cleared.
        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskAcceptanceCriterion.id))).scalars().all() == []
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM task_acceptance_criteria WHERE id = :id"), {"id": criterion_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM task_acceptance_criteria WHERE id = :id"), {"id": criterion_b})
            await connection.execute(text("DELETE FROM tasks WHERE id IN (:task_a, :task_b)"), {"task_a": task_a, "task_b": task_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:tenant_a, :tenant_b)"), {"tenant_a": tenant_a, "tenant_b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_teammate_tool_invocation_rls_is_tenant_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    teammate_a, teammate_b = uuid.uuid4(), uuid.uuid4()
    invocation_a, invocation_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS A', :slug, '')"),
                [{"id": tenant_a, "slug": f"rls-{tenant_a}"}, {"id": tenant_b, "slug": f"rls-{tenant_b}"}],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'RLS org', :slug, :tenant_id)"),
                [
                    {"id": organization_a, "slug": f"rls-org-{organization_a}", "tenant_id": tenant_a},
                    {"id": organization_b, "slug": f"rls-org-{organization_b}", "tenant_id": tenant_b},
                ],
            )
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS task')"),
                [{"id": task_a, "tenant_id": tenant_a}, {"id": task_b, "tenant_id": tenant_b}],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO task_workspaces (id, tenant_id, task_id) VALUES (:id, :tenant_id, :task_id)"),
                {"id": workspace_a, "tenant_id": tenant_a, "task_id": task_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO task_workspaces (id, tenant_id, task_id) VALUES (:id, :tenant_id, :task_id)"),
                {"id": workspace_b, "tenant_id": tenant_b, "task_id": task_b},
            )
            await connection.execute(
                text(
                    "INSERT INTO teammate_profiles "
                    "(id, organization_id, tenant_id, name, role, agent_type) "
                    "VALUES (:id, :organization_id, :tenant_id, 'RLS teammate', 'software_engineer', 'engineer')"
                ),
                [
                    {"id": teammate_a, "organization_id": organization_a, "tenant_id": tenant_a},
                    {"id": teammate_b, "organization_id": organization_b, "tenant_id": tenant_b},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text(
                    "INSERT INTO teammate_tool_invocations "
                    "(id, tenant_id, task_id, workspace_id, teammate_id, tool_name) "
                    "VALUES (:id, :tenant_id, :task_id, :workspace_id, :teammate_id, 'workspace_read_file')"
                ),
                {
                    "id": invocation_a,
                    "tenant_id": tenant_a,
                    "task_id": task_a,
                    "workspace_id": workspace_a,
                    "teammate_id": teammate_a,
                },
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text(
                    "INSERT INTO teammate_tool_invocations "
                    "(id, tenant_id, task_id, workspace_id, teammate_id, tool_name) "
                    "VALUES (:id, :tenant_id, :task_id, :workspace_id, :teammate_id, 'workspace_read_file')"
                ),
                {
                    "id": invocation_b,
                    "tenant_id": tenant_b,
                    "task_id": task_b,
                    "workspace_id": workspace_b,
                    "teammate_id": teammate_b,
                },
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TeammateToolInvocation.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            visible = (await session.execute(select(TeammateToolInvocation.id))).scalars().all()
            assert visible == [invocation_a]
            session.add(
                TeammateToolInvocation(
                    id=uuid.uuid4(),
                    tenant_id=tenant_b,
                    task_id=task_b,
                    workspace_id=workspace_b,
                    teammate_id=teammate_b,
                    tool_name="workspace_read_file",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TeammateToolInvocation.id))).scalars().all() == []
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM teammate_tool_invocations WHERE id = :id"), {"id": invocation_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM teammate_tool_invocations WHERE id = :id"), {"id": invocation_b})
            await connection.execute(text("DELETE FROM teammate_profiles WHERE id IN (:a, :b)"), {"a": teammate_a, "b": teammate_b})
            await connection.execute(text("DELETE FROM tasks WHERE id IN (:a, :b)"), {"a": task_a, "b": task_b})
            await connection.execute(text("DELETE FROM organizations WHERE id IN (:a, :b)"), {"a": organization_a, "b": organization_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_workspace_rls_is_tenant_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS tenant', :slug, '')"),
                [{"id": tenant_a, "slug": f"rls-{tenant_a}"}, {"id": tenant_b, "slug": f"rls-{tenant_b}"}],
            )
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS workspace task')"),
                [{"id": task_a, "tenant_id": tenant_a}, {"id": task_b, "tenant_id": tenant_b}],
            )
            for tenant_id, task_id, workspace_id in (
                (tenant_a, task_a, workspace_a),
                (tenant_b, task_b, workspace_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
                )
                await connection.execute(
                    text("INSERT INTO task_workspaces (id, tenant_id, task_id) VALUES (:id, :tenant_id, :task_id)"),
                    {"id": workspace_id, "tenant_id": tenant_id, "task_id": task_id},
                )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskWorkspace.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            visible = (await session.execute(select(TaskWorkspace.id))).scalars().all()
            assert visible == [workspace_a]
            updated = await session.execute(
                text("UPDATE task_workspaces SET source_commit_sha = 'hidden' WHERE id = :id"), {"id": workspace_b}
            )
            assert updated.rowcount == 0
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("INSERT INTO task_workspaces (id, tenant_id, task_id) VALUES (:id, :tenant_id, :task_id)"),
                    {"id": uuid.uuid4(), "tenant_id": tenant_b, "task_id": task_b},
                )
            await session.rollback()

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskWorkspace.id))).scalars().all() == []
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, workspace_id in ((tenant_a, workspace_a), (tenant_b, workspace_b)):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
                )
                await connection.execute(text("DELETE FROM task_workspaces WHERE id = :id"), {"id": workspace_id})
            await connection.execute(text("DELETE FROM tasks WHERE id IN (:a, :b)"), {"a": task_a, "b": task_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()
