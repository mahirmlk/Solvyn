"""PostgreSQL-only checks for the initial tenant RLS boundary."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from solvyn.db.models import (
    Approval,
    ExecutionGraph,
    Integration,
    IntegrationCredential,
    MaintenanceSchedule,
    Memory,
    ProactiveEvent,
    Project,
    ProjectKnowledge,
    Repository,
    Session,
    Task,
    TaskAcceptanceCriterion,
    TaskDelivery,
    TaskDependency,
    TaskHandoff,
    TaskWorkspace,
    TeammateMemory,
    TeammateToolInvocation,
    TokenUsage,
    VerificationRun,
)
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
                        "('tasks', 'task_acceptance_criteria', 'task_deliveries', 'task_workspaces', "
                        "'teammate_tool_invocations', 'task_dependencies', 'verification_runs', "
                        "'task_handoffs', 'teammate_memory', 'sessions', 'execution_graphs', 'memory', 'token_usage', "
                        "'approvals', 'proactive_events', 'maintenance_schedules', 'integrations', "
                        "'integration_credentials', 'projects', 'repositories', 'project_knowledge')"
                    )
                )
            ).all()
            assert {row[0] for row in protected_tables} == {
                "tasks",
                "task_acceptance_criteria",
                "task_deliveries",
                "task_workspaces",
                "teammate_tool_invocations",
                "task_dependencies",
                "verification_runs",
                "task_handoffs",
                "teammate_memory",
                "sessions",
                "execution_graphs",
                "memory",
                "token_usage",
                "approvals",
                "proactive_events",
                "maintenance_schedules",
                "integrations",
                "integration_credentials",
                "projects",
                "repositories",
                "project_knowledge",
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
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS task')"),
                    {"id": task_id, "tenant_id": tenant_id},
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
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :task_id"), {"task_id": task_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:tenant_a, :tenant_b)"), {"tenant_a": tenant_a, "tenant_b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_rls_is_tenant_scoped_and_validates_ownership_links() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    graph_a, graph_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b, child_a = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'tasks'")
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS tenant', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-tasks-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-tasks-{tenant_b}"},
                ],
            )
            for tenant_id, session_id, graph_id, task_id in (
                (tenant_a, session_a, graph_a, task_a),
                (tenant_b, session_b, graph_b, task_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO sessions (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS task session')"),
                    {"id": session_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text("INSERT INTO execution_graphs (id, session_id, graph_data, status) VALUES (:id, :session_id, '{}', 'running')"),
                    {"id": graph_id, "session_id": session_id},
                )
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, session_id, execution_id, title) VALUES (:id, :tenant_id, :session_id, :execution_id, 'RLS task')"),
                    {"id": task_id, "tenant_id": tenant_id, "session_id": session_id, "execution_id": graph_id},
                )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_a)},
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(id, tenant_id, session_id, execution_id, parent_task_id, title) "
                    "VALUES (:id, :tenant_id, :session_id, :execution_id, :parent_task_id, 'RLS child')"
                ),
                {"id": child_a, "tenant_id": tenant_a, "session_id": session_a, "execution_id": graph_a, "parent_task_id": task_a},
            )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(Task.id))).scalars().all() == []
            db.add(Task(id=uuid.uuid4(), tenant_id=tenant_a, title="unscoped insert"))
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

        tenant_context.set(tenant_a, "rls-tasks-a")
        async with sessions() as db:
            assert set((await db.execute(select(Task.id))).scalars().all()) == {task_a, child_a}
            hidden_update = await db.execute(
                text("UPDATE tasks SET title = 'hidden' WHERE id = :id"), {"id": task_b}
            )
            assert hidden_update.rowcount == 0
            with pytest.raises(DBAPIError):
                await db.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'cross tenant')"),
                    {"id": uuid.uuid4(), "tenant_id": tenant_b},
                )
            await db.rollback()

        async with sessions() as db:
            db.add(Task(id=uuid.uuid4(), tenant_id=tenant_a, session_id=session_b, title="foreign session"))
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

        async with sessions() as db:
            db.add(
                Task(
                    id=uuid.uuid4(), tenant_id=tenant_a, session_id=session_a,
                    execution_id=graph_b, title="foreign execution",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

        async with sessions() as db:
            db.add(Task(id=uuid.uuid4(), tenant_id=tenant_a, parent_task_id=task_b, title="foreign parent"))
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, parent_ids in ((tenant_a, (child_a, task_a)), (tenant_b, (task_b,))):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                for task_id in parent_ids:
                    await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
                graph_id = graph_a if tenant_id == tenant_a else graph_b
                await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": graph_id})
                session_id = session_a if tenant_id == tenant_a else session_b
                await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_memory_rls_is_tenant_scoped_and_validates_session_owner() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    memory_a, memory_b = uuid.uuid4(), uuid.uuid4()
    shared_key = f"memory:rls-{uuid.uuid4()}"

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'memory'")
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS memory', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-memory-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-memory-{tenant_b}"},
                ],
            )
            for tenant_id, session_id, memory_id, value in (
                (tenant_a, session_a, memory_a, "tenant-a"),
                (tenant_b, session_b, memory_b, "tenant-b"),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO sessions (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS memory session')"),
                    {"id": session_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text("INSERT INTO memory (id, tenant_id, session_id, agent_type, key, value) VALUES (:id, :tenant_id, :session_id, 'rls-test', :key, :value)"),
                    {
                        "id": memory_id,
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "key": shared_key,
                        "value": value,
                    },
                )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(Memory.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                await db.execute(
                    text("INSERT INTO memory (id, tenant_id, agent_type, key, value) VALUES (:id, :tenant_id, 'rls-test', :key, 'unscoped')"),
                    {"id": uuid.uuid4(), "tenant_id": tenant_a, "key": shared_key},
                )
            await db.rollback()

        tenant_context.set(tenant_a, "rls-memory-a")
        async with sessions() as db:
            values = (await db.execute(select(Memory.value).where(Memory.key == shared_key))).scalars().all()
            assert values == ["tenant-a"]
            hidden_update = await db.execute(
                text("UPDATE memory SET value = 'hidden' WHERE id = :id"), {"id": memory_b}
            )
            assert hidden_update.rowcount == 0
            await db.rollback()

        async with sessions() as db:
            db.add(
                Memory(
                    id=uuid.uuid4(), tenant_id=tenant_a, session_id=session_b,
                    agent_type="rls-test", key=shared_key, value="foreign session",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, session_id, memory_id in (
                (tenant_a, session_a, memory_a),
                (tenant_b, session_b, memory_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM memory WHERE id = :id"), {"id": memory_id})
                await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_token_usage_rls_is_tenant_scoped_and_validates_session_owner() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    usage_a, usage_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'token_usage'")
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS usage', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-usage-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-usage-{tenant_b}"},
                ],
            )
            for tenant_id, session_id, usage_id, model_name in (
                (tenant_a, session_a, usage_a, "tenant-a"),
                (tenant_b, session_b, usage_b, "tenant-b"),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO sessions (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS usage session')"),
                    {"id": session_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO token_usage "
                        "(id, tenant_id, session_id, agent_type, provider, model, total_tokens, cost) "
                        "VALUES (:id, :tenant_id, :session_id, 'rls-test', 'test', :model, 10, 0.01)"
                    ),
                    {
                        "id": usage_id,
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "model": model_name,
                    },
                )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(TokenUsage.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                await db.execute(
                    text(
                        "INSERT INTO token_usage "
                        "(id, tenant_id, agent_type, provider, model) "
                        "VALUES (:id, :tenant_id, 'rls-test', 'test', 'unscoped')"
                    ),
                    {"id": uuid.uuid4(), "tenant_id": tenant_a},
                )
            await db.rollback()

        tenant_context.set(tenant_a, "rls-usage-a")
        async with sessions() as db:
            assert (await db.execute(select(TokenUsage.id))).scalars().all() == [usage_a]
            hidden_update = await db.execute(
                text("UPDATE token_usage SET total_tokens = 999 WHERE id = :id"), {"id": usage_b}
            )
            assert hidden_update.rowcount == 0
            await db.rollback()

        async with sessions() as db:
            db.add(
                TokenUsage(
                    id=uuid.uuid4(), tenant_id=tenant_a, session_id=session_b,
                    agent_type="rls-test", provider="test", model="foreign-session",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, session_id, usage_id in (
                (tenant_a, session_a, usage_a),
                (tenant_b, session_b, usage_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM token_usage WHERE id = :id"), {"id": usage_id})
                await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_approval_rls_is_tenant_and_organization_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_a_other, organization_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_a, project_a_other = uuid.uuid4(), uuid.uuid4()
    approval_a, approval_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            flags = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'approvals'")
                )
            ).one()
            assert flags == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS approvals', :slug, '')"),
                [{"id": tenant_a, "slug": f"rls-approval-{tenant_a}"}, {"id": tenant_b, "slug": f"rls-approval-{tenant_b}"}],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'RLS approvals', :slug, :tenant_id)"),
                [
                    {"id": organization_a, "slug": f"rls-approval-{organization_a}", "tenant_id": tenant_a},
                    {"id": organization_a_other, "slug": f"rls-approval-{organization_a_other}", "tenant_id": tenant_a},
                    {"id": organization_b, "slug": f"rls-approval-{organization_b}", "tenant_id": tenant_b},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'RLS approvals', :slug)"),
                [
                    {"id": project_a, "organization_id": organization_a, "tenant_id": tenant_a, "slug": f"rls-approval-{project_a}"},
                    {"id": project_a_other, "organization_id": organization_a_other, "tenant_id": tenant_a, "slug": f"rls-approval-{project_a_other}"},
                ],
            )
            for tenant_id, organization_id, approval_id in (
                (tenant_a, organization_a, approval_a),
                (tenant_b, organization_b, approval_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO approvals (id, organization_id, kind, summary) "
                        "VALUES (:id, :organization_id, 'rls-test', 'RLS approval')"
                    ),
                    {"id": approval_id, "organization_id": organization_id},
                )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(Approval.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                await db.execute(
                    text(
                        "INSERT INTO approvals (id, organization_id, kind, summary) "
                        "VALUES (:id, :organization_id, 'rls-test', 'unscoped')"
                    ),
                    {"id": uuid.uuid4(), "organization_id": organization_a},
                )
            await db.rollback()

        tenant_context.set(tenant_a, "rls-approval-a")
        async with sessions() as db:
            assert (await db.execute(select(Approval.id))).scalars().all() == [approval_a]
            hidden_update = await db.execute(
                text("UPDATE approvals SET summary = 'cross-tenant' WHERE id = :id"), {"id": approval_b}
            )
            assert hidden_update.rowcount == 0
            await db.rollback()

        async with sessions() as db:
            db.add(
                Approval(
                    id=uuid.uuid4(),
                    organization_id=organization_a,
                    project_id=project_a_other,
                    kind="rls-test",
                    summary="cross-organization project",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, approval_id in ((tenant_a, approval_a), (tenant_b, approval_b)):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_id})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("DELETE FROM projects WHERE id IN (:a, :other)"), {"a": project_a, "other": project_a_other}
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :other, :b)"),
                {"a": organization_a, "other": organization_a_other, "b": organization_b},
            )
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_proactive_data_rls_is_tenant_and_organization_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_a_other, organization_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_a, project_a_other, project_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    event_a, event_b = uuid.uuid4(), uuid.uuid4()
    schedule_a, schedule_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('proactive_events', 'maintenance_schedules')"
                    )
                )
            ).all()
            assert {row[0] for row in protected} == {"proactive_events", "maintenance_schedules"}
            assert all(row[1] and row[2] for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS proactive', :slug, '')"),
                [{"id": tenant_a, "slug": f"rls-proactive-{tenant_a}"}, {"id": tenant_b, "slug": f"rls-proactive-{tenant_b}"}],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'RLS proactive', :slug, :tenant_id)"),
                [
                    {"id": organization_a, "slug": f"rls-proactive-{organization_a}", "tenant_id": tenant_a},
                    {"id": organization_a_other, "slug": f"rls-proactive-{organization_a_other}", "tenant_id": tenant_a},
                    {"id": organization_b, "slug": f"rls-proactive-{organization_b}", "tenant_id": tenant_b},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'RLS proactive', :slug)"),
                [
                    {"id": project_a, "organization_id": organization_a, "tenant_id": tenant_a, "slug": f"rls-proactive-{project_a}"},
                    {"id": project_a_other, "organization_id": organization_a_other, "tenant_id": tenant_a, "slug": f"rls-proactive-{project_a_other}"},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'RLS proactive', :slug)"),
                {"id": project_b, "organization_id": organization_b, "tenant_id": tenant_b, "slug": f"rls-proactive-{project_b}"},
            )
            for tenant_id, organization_id, project_id, event_id, schedule_id in (
                (tenant_a, organization_a, project_a, event_a, schedule_a),
                (tenant_b, organization_b, project_b, event_b, schedule_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO proactive_events "
                        "(id, organization_id, project_id, delivery_id, source_event_id, event_type, action, "
                        "repository_name, workflow_name, conclusion, status) "
                        "VALUES (:id, :organization_id, :project_id, :delivery_id, :source_event_id, "
                        "'workflow_run', 'completed', 'org/repo', 'CI', 'failure', 'pending')"
                    ),
                    {
                        "id": event_id,
                        "organization_id": organization_id,
                        "project_id": project_id,
                        "delivery_id": str(uuid.uuid4()),
                        "source_event_id": f"rls-{event_id}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO maintenance_schedules "
                        "(id, organization_id, project_id, maintenance_type, title, interval_days, next_run_at) "
                        "VALUES (:id, :organization_id, :project_id, 'code_quality', 'RLS schedule', 7, now())"
                    ),
                    {"id": schedule_id, "organization_id": organization_id, "project_id": project_id},
                )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(ProactiveEvent.id))).scalars().all() == []
            assert (await db.execute(select(MaintenanceSchedule.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                await db.execute(
                    text(
                        "INSERT INTO proactive_events "
                        "(id, organization_id, project_id, delivery_id, source_event_id, event_type, action, "
                        "repository_name, workflow_name, conclusion) "
                        "VALUES (:id, :organization_id, :project_id, :delivery_id, :source_event_id, "
                        "'test', 'test', '', '', 'test')"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "organization_id": organization_a,
                        "project_id": project_a,
                        "delivery_id": str(uuid.uuid4()),
                        "source_event_id": str(uuid.uuid4()),
                    },
                )
            await db.rollback()

        tenant_context.set(tenant_a, "rls-proactive-a")
        async with sessions() as db:
            assert (await db.execute(select(ProactiveEvent.id))).scalars().all() == [event_a]
            assert (await db.execute(select(MaintenanceSchedule.id))).scalars().all() == [schedule_a]
            hidden_event_update = await db.execute(
                text("UPDATE proactive_events SET status = 'acknowledged' WHERE id = :id"), {"id": event_b}
            )
            hidden_schedule_update = await db.execute(
                text("UPDATE maintenance_schedules SET enabled = false WHERE id = :id"), {"id": schedule_b}
            )
            assert hidden_event_update.rowcount == hidden_schedule_update.rowcount == 0
            await db.rollback()

        async with sessions() as db:
            db.add(
                ProactiveEvent(
                    id=uuid.uuid4(),
                    organization_id=organization_a,
                    project_id=project_a_other,
                    delivery_id=str(uuid.uuid4()),
                    source_event_id=str(uuid.uuid4()),
                    event_type="test",
                    action="test",
                    repository_name="",
                    workflow_name="",
                    conclusion="test",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

        async with sessions() as db:
            db.add(
                MaintenanceSchedule(
                    id=uuid.uuid4(),
                    organization_id=organization_a,
                    project_id=project_a_other,
                    maintenance_type="code_quality",
                    title="cross-organization project",
                    interval_days=7,
                    next_run_at=datetime.now(UTC),
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, event_id, schedule_id in ((tenant_a, event_a, schedule_a), (tenant_b, event_b, schedule_b)):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM proactive_events WHERE id = :id"), {"id": event_id})
                await connection.execute(text("DELETE FROM maintenance_schedules WHERE id = :id"), {"id": schedule_id})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("DELETE FROM projects WHERE id IN (:a, :other)"),
                {"a": project_a, "other": project_a_other},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM projects WHERE id = :b"), {"b": project_b})
            await connection.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :other, :b)"),
                {"a": organization_a, "other": organization_a_other, "b": organization_b},
            )
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_integration_credentials_rls_is_tenant_scoped_and_allows_inactive_cleanup() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b = uuid.uuid4(), uuid.uuid4()
    integration_a, integration_b = uuid.uuid4(), uuid.uuid4()
    credential_a, credential_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('integrations', 'integration_credentials')"
                    )
                )
            ).all()
            assert {row[0] for row in protected} == {"integrations", "integration_credentials"}
            assert all(row[1] and row[2] for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS integrations', :slug, '')"),
                [{"id": tenant_a, "slug": f"rls-integration-{tenant_a}"}, {"id": tenant_b, "slug": f"rls-integration-{tenant_b}"}],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'RLS integrations', :slug, :tenant_id)"),
                [
                    {"id": organization_a, "slug": f"rls-integration-{organization_a}", "tenant_id": tenant_a},
                    {"id": organization_b, "slug": f"rls-integration-{organization_b}", "tenant_id": tenant_b},
                ],
            )
            for tenant_id, organization_id, integration_id, credential_id, active in (
                (tenant_a, organization_a, integration_a, credential_a, True),
                (tenant_b, organization_b, integration_b, credential_b, False),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO integrations (id, organization_id, provider, external_id, name, is_active) "
                        "VALUES (:id, :organization_id, 'github', 'organization-token', 'RLS integration', :active)"
                    ),
                    {"id": integration_id, "organization_id": organization_id, "active": active},
                )
                await connection.execute(
                    text(
                        "INSERT INTO integration_credentials (id, integration_id, ciphertext, account_login) "
                        "VALUES (:id, :integration_id, :ciphertext, :account_login)"
                    ),
                    {
                        "id": credential_id,
                        "integration_id": integration_id,
                        "ciphertext": f"sealed-{tenant_id}".encode(),
                        "account_login": f"account-{tenant_id}",
                    },
                )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(Integration.id))).scalars().all() == []
            assert (await db.execute(select(IntegrationCredential.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                await db.execute(
                    text(
                        "INSERT INTO integrations (id, organization_id, provider, external_id) "
                        "VALUES (:id, :organization_id, 'github', 'unscoped')"
                    ),
                    {"id": uuid.uuid4(), "organization_id": organization_a},
                )
            await db.rollback()

        tenant_context.set(tenant_a, "rls-integration-a")
        async with sessions() as db:
            assert (await db.execute(select(Integration.id))).scalars().all() == [integration_a]
            assert (await db.execute(select(IntegrationCredential.id))).scalars().all() == [credential_a]
            hidden_integration_update = await db.execute(
                text("UPDATE integrations SET name = 'cross-tenant' WHERE id = :id"), {"id": integration_b}
            )
            hidden_credential_update = await db.execute(
                text("UPDATE integration_credentials SET account_login = 'cross-tenant' WHERE id = :id"),
                {"id": credential_b},
            )
            assert hidden_integration_update.rowcount == hidden_credential_update.rowcount == 0
            await db.rollback()

        async with sessions() as db:
            db.add(
                IntegrationCredential(
                    id=uuid.uuid4(),
                    integration_id=integration_b,
                    ciphertext=b"sealed-cross-tenant",
                    account_login="cross-tenant",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

        tenant_context.set(tenant_b, "rls-integration-b")
        async with sessions() as db:
            inactive = await db.execute(select(Integration).where(Integration.id == integration_b))
            assert inactive.scalar_one().is_active is False
            removable = await db.execute(
                text("DELETE FROM integration_credentials WHERE id = :id"), {"id": credential_b}
            )
            assert removable.rowcount == 1
            await db.commit()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, credential_id, integration_id in (
                (tenant_a, credential_a, integration_a),
                (tenant_b, credential_b, integration_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("DELETE FROM integration_credentials WHERE id = :id"), {"id": credential_id}
                )
                await connection.execute(text("DELETE FROM integrations WHERE id = :id"), {"id": integration_id})
            await connection.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b)"), {"a": organization_a, "b": organization_b}
            )
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_project_repository_knowledge_rls_is_tenant_and_link_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b, organization_a_other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    integration_a, integration_b, integration_a_other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    repository_a, repository_b = uuid.uuid4(), uuid.uuid4()
    knowledge_a, knowledge_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b, task_a_other = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    delivery_a, delivery_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('projects', 'repositories', 'project_knowledge')")
                )
            ).all()
            assert {row[0] for row in protected} == {"projects", "repositories", "project_knowledge"}
            assert all(row[1] and row[2] for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS projects', :slug, '')"),
                [{"id": tenant_a, "slug": f"rls-project-{tenant_a}"}, {"id": tenant_b, "slug": f"rls-project-{tenant_b}"}],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'RLS projects', :slug, :tenant_id)"),
                [
                    {"id": organization_a, "slug": f"rls-project-{organization_a}", "tenant_id": tenant_a},
                    {"id": organization_b, "slug": f"rls-project-{organization_b}", "tenant_id": tenant_b},
                    {"id": organization_a_other, "slug": f"rls-project-{organization_a_other}", "tenant_id": tenant_a},
                ],
            )
            for tenant_id, organization_id, project_id, integration_id, repository_id, knowledge_id in (
                (tenant_a, organization_a, project_a, integration_a, repository_a, knowledge_a),
                (tenant_b, organization_b, project_b, integration_b, repository_b, knowledge_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO projects (id, organization_id, tenant_id, name, slug) "
                        "VALUES (:id, :organization_id, :tenant_id, 'RLS project', :slug)"
                    ),
                    {
                        "id": project_id,
                        "organization_id": organization_id,
                        "tenant_id": tenant_id,
                        "slug": f"project-{project_id}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO integrations (id, organization_id, provider, external_id, name) "
                        "VALUES (:id, :organization_id, 'github', 'rls-project', 'RLS integration')"
                    ),
                    {"id": integration_id, "organization_id": organization_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO repositories (id, project_id, integration_id, full_name) "
                        "VALUES (:id, :project_id, :integration_id, :full_name)"
                    ),
                    {
                        "id": repository_id,
                        "project_id": project_id,
                        "integration_id": integration_id,
                        "full_name": f"owner/repo-{project_id}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO project_knowledge (id, project_id, category, key, value) "
                        "VALUES (:id, :project_id, 'architecture', 'overview', 'private context')"
                    ),
                    {"id": knowledge_id, "project_id": project_id},
                )
                task_id, workspace_id = (task_a, workspace_a) if tenant_id == tenant_a else (task_b, workspace_b)
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS workspace task')"),
                    {"id": task_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO task_workspaces (id, tenant_id, task_id, repository_id) "
                        "VALUES (:id, :tenant_id, :task_id, :repository_id)"
                    ),
                    {
                        "id": workspace_id,
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "repository_id": repository_id,
                    },
                )
                delivery_id = delivery_a if tenant_id == tenant_a else delivery_b
                await connection.execute(
                    text(
                        "INSERT INTO task_deliveries "
                        "(id, tenant_id, task_id, workspace_id, repository_id, source_commit_sha, base_branch, diff_digest, branch_name) "
                        "VALUES (:id, :tenant_id, :task_id, :workspace_id, :repository_id, 'abc123', 'main', :digest, :branch)"
                    ),
                    {
                        "id": delivery_id,
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "repository_id": repository_id,
                        "digest": f"digest-{delivery_id}",
                        "branch": f"codex/rls-{delivery_id}",
                    },
                )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)}
            )
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS unassigned task')"),
                {"id": task_a_other, "tenant_id": tenant_a},
            )
            await connection.execute(
                text(
                    "INSERT INTO integrations (id, organization_id, provider, external_id, name) "
                    "VALUES (:id, :organization_id, 'github', 'rls-project-other', 'RLS integration')"
                ),
                {"id": integration_a_other, "organization_id": organization_a_other},
            )

        tenant_context.clear()
        async with sessions() as db:
            assert (await db.execute(select(Project.id))).scalars().all() == []
            assert (await db.execute(select(Repository.id))).scalars().all() == []
            assert (await db.execute(select(ProjectKnowledge.id))).scalars().all() == []
            assert (await db.execute(select(TaskDelivery.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                await db.execute(
                    text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :org, :tenant, 'bad', 'bad')"),
                    {"id": uuid.uuid4(), "org": organization_a, "tenant": tenant_a},
                )
            await db.rollback()

        tenant_context.set(tenant_a, "rls-project-a")
        async with sessions() as db:
            assert (await db.execute(select(Project.id))).scalars().all() == [project_a]
            assert (await db.execute(select(Repository.id))).scalars().all() == [repository_a]
            assert (await db.execute(select(ProjectKnowledge.id))).scalars().all() == [knowledge_a]
            assert (await db.execute(select(TaskDelivery.id))).scalars().all() == [delivery_a]
            hidden_repository_update = await db.execute(
                text("UPDATE repositories SET full_name = 'hidden/repo' WHERE id = :id"), {"id": repository_b}
            )
            hidden_knowledge_update = await db.execute(
                text("UPDATE project_knowledge SET value = 'hidden' WHERE id = :id"), {"id": knowledge_b}
            )
            assert hidden_repository_update.rowcount == hidden_knowledge_update.rowcount == 0
            await db.rollback()

            with pytest.raises(DBAPIError):
                await db.execute(
                    text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :org, :tenant, 'bad', 'wrong-org')"),
                    {"id": uuid.uuid4(), "org": organization_b, "tenant": tenant_a},
                )
            await db.rollback()

            db.add(
                Repository(
                    id=uuid.uuid4(),
                    project_id=project_a,
                    integration_id=integration_a_other,
                    full_name="owner/wrong-organization",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

            db.add(
                TaskDelivery(
                    id=uuid.uuid4(),
                    tenant_id=tenant_a,
                    task_id=task_a,
                    workspace_id=workspace_a,
                    repository_id=repository_b,
                    source_commit_sha="abc123",
                    base_branch="main",
                    diff_digest=uuid.uuid4().hex,
                    branch_name=f"codex/rls-invalid-{uuid.uuid4().hex[:8]}",
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()

            db.add(
                TaskWorkspace(
                    id=uuid.uuid4(),
                    tenant_id=tenant_a,
                    task_id=task_a_other,
                    repository_id=repository_b,
                )
            )
            with pytest.raises(DBAPIError):
                await db.flush()
            await db.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, project_id, integration_id in (
                (tenant_a, project_a, integration_a),
                (tenant_b, project_b, integration_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM project_knowledge WHERE project_id = :id"), {"id": project_id})
                await connection.execute(text("DELETE FROM task_deliveries WHERE task_id = :id"), {"id": task_a if tenant_id == tenant_a else task_b})
                await connection.execute(text("DELETE FROM task_workspaces WHERE task_id = :id"), {"id": task_a if tenant_id == tenant_a else task_b})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_a if tenant_id == tenant_a else task_b})
                await connection.execute(text("DELETE FROM repositories WHERE project_id = :id"), {"id": project_id})
                await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
                await connection.execute(text("DELETE FROM integrations WHERE id = :id"), {"id": integration_id})
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)}
            )
            await connection.execute(text("DELETE FROM integrations WHERE id = :id"), {"id": integration_a_other})
            await connection.execute(
                text("DELETE FROM organizations WHERE id IN (:a, :b, :c)"),
                {"a": organization_a, "b": organization_b, "c": organization_a_other},
            )
            await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_a_other})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_session_rls_is_tenant_scoped_and_fails_closed() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'sessions'")
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Session RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-session-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-session-{tenant_b}"},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Private A', 'active')"),
                {"id": session_a, "tenant_id": tenant_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Private B', 'active')"),
                {"id": session_b, "tenant_id": tenant_b},
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(Session.id))).scalars().all() == []
            session.add(Session(id=uuid.uuid4(), tenant_id=tenant_a, title="Unscoped", status="active"))
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.set(tenant_a, "rls-session-a")
        async with sessions() as session:
            assert (await session.execute(select(Session.id))).scalars().all() == [session_a]
            session.add(Session(id=uuid.uuid4(), tenant_id=tenant_b, title="Cross tenant", status="active"))
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.set(tenant_b, "rls-session-b")
        async with sessions() as session:
            assert (await session.execute(select(Session.id))).scalars().all() == [session_b]
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_fetches_due_workflows_under_each_tenant_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.workflows import scheduler
    from solvyn.workflows.scheduler import _fetch_due_scheduled_workflows

    monkeypatch.setattr(scheduler, "_BATCH_SIZE", 100)

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    execution_a, execution_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Scheduled RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-scheduler-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-scheduler-{tenant_b}"},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Scheduled A', 'active')"),
                {"id": session_a, "tenant_id": tenant_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Scheduled B', 'active')"),
                {"id": session_b, "tenant_id": tenant_b},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO execution_graphs (id, session_id, graph_data, status, scheduled_at) VALUES (:id, :session_id, '{}', 'scheduled', now() - interval '1 minute')"),
                {"id": execution_a, "session_id": session_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO execution_graphs (id, session_id, graph_data, status, scheduled_at) VALUES (:id, :session_id, '{}', 'scheduled', now() - interval '1 minute')"),
                {"id": execution_b, "session_id": session_b},
            )

        tenant_context.clear()
        async with sessions() as session:
            due = await _fetch_due_scheduled_workflows(session)
            assert set(due) >= {(execution_a, tenant_a), (execution_b, tenant_b)}
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": execution_a})
            await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": execution_b})
            await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_execution_graph_rls_follows_owning_session_tenant() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    execution_a, execution_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Execution RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-execution-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-execution-{tenant_b}"},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Execution A', 'active')"),
                {"id": session_a, "tenant_id": tenant_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Execution B', 'active')"),
                {"id": session_b, "tenant_id": tenant_b},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO execution_graphs (id, session_id, graph_data, status) VALUES (:id, :session_id, '{}', 'running')"),
                {"id": execution_a, "session_id": session_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO execution_graphs (id, session_id, graph_data, status) VALUES (:id, :session_id, '{}', 'running')"),
                {"id": execution_b, "session_id": session_b},
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(ExecutionGraph.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-execution-a")
        async with sessions() as session:
            assert (await session.execute(select(ExecutionGraph.id))).scalars().all() == [execution_a]
            changed = await session.execute(
                update(ExecutionGraph).where(ExecutionGraph.id == execution_b).values(status="cancelled")
            )
            assert changed.rowcount == 0
            session.add(ExecutionGraph(id=uuid.uuid4(), session_id=session_b, graph_data="{}", status="running"))
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.set(tenant_b, "rls-execution-b")
        async with sessions() as session:
            assert (await session.execute(select(ExecutionGraph.id))).scalars().all() == [execution_b]
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": execution_a})
            await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": execution_b})
            await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_teammate_memory_rls_scopes_tenant_and_assignment_relationships() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b, project_unassigned = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    teammate_a, teammate_b = uuid.uuid4(), uuid.uuid4()
    memory_a, memory_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'teammate_memory'"
                    )
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Memory RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-memory-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-memory-{tenant_b}"},
                ],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'Memory RLS', :slug, :tenant_id)"),
                [
                    {"id": org_a, "slug": f"rls-memory-org-{org_a}", "tenant_id": tenant_a},
                    {"id": org_b, "slug": f"rls-memory-org-{org_b}", "tenant_id": tenant_b},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'Memory RLS', :slug)"),
                [
                    {"id": project_a, "organization_id": org_a, "tenant_id": tenant_a, "slug": f"rls-memory-project-{project_a}"},
                    {"id": project_unassigned, "organization_id": org_a, "tenant_id": tenant_a, "slug": f"rls-memory-project-{project_unassigned}"},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'Memory RLS', :slug)"),
                {"id": project_b, "organization_id": org_b, "tenant_id": tenant_b, "slug": f"rls-memory-project-{project_b}"},
            )
            await connection.execute(
                text("INSERT INTO teammate_profiles (id, organization_id, tenant_id, name, role, agent_type) VALUES (:id, :organization_id, :tenant_id, 'Memory RLS', 'developer', 'developer')"),
                [
                    {"id": teammate_a, "organization_id": org_a, "tenant_id": tenant_a},
                    {"id": teammate_b, "organization_id": org_b, "tenant_id": tenant_b},
                ],
            )
            await connection.execute(
                text("INSERT INTO teammate_project_assignments (id, teammate_id, project_id) VALUES (:id, :teammate_id, :project_id)"),
                [
                    {"id": uuid.uuid4(), "teammate_id": teammate_a, "project_id": project_a},
                    {"id": uuid.uuid4(), "teammate_id": teammate_b, "project_id": project_b},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO teammate_memory (id, tenant_id, teammate_id, project_id, key, value) VALUES (:id, :tenant_id, :teammate_id, :project_id, 'context', 'private')"),
                {"id": memory_a, "tenant_id": tenant_a, "teammate_id": teammate_a, "project_id": project_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO teammate_memory (id, tenant_id, teammate_id, project_id, key, value) VALUES (:id, :tenant_id, :teammate_id, :project_id, 'context', 'private')"),
                {"id": memory_b, "tenant_id": tenant_b, "teammate_id": teammate_b, "project_id": project_b},
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TeammateMemory.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-memory-a")
        async with sessions() as session:
            assert (await session.execute(select(TeammateMemory.id))).scalars().all() == [memory_a]

        async with sessions() as session:
            await session.execute(
                text(
                    "DELETE FROM teammate_project_assignments "
                    "WHERE teammate_id = :teammate_id AND project_id = :project_id"
                ),
                {"teammate_id": teammate_a, "project_id": project_a},
            )
            assert (await session.execute(select(TeammateMemory.id))).scalars().all() == []
            await session.execute(
                text(
                    "INSERT INTO teammate_project_assignments (id, teammate_id, project_id) "
                    "VALUES (:id, :teammate_id, :project_id)"
                ),
                {"id": uuid.uuid4(), "teammate_id": teammate_a, "project_id": project_a},
            )
            assert (await session.execute(select(TeammateMemory.id))).scalars().all() == [memory_a]
            await session.rollback()

        async with sessions() as session:
            session.add(
                TeammateMemory(
                    id=uuid.uuid4(), tenant_id=tenant_b, teammate_id=teammate_b,
                    project_id=project_b, key="cross-tenant", value="private",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        async with sessions() as session:
            session.add(
                TeammateMemory(
                    id=uuid.uuid4(), tenant_id=tenant_a, teammate_id=teammate_a,
                    project_id=project_unassigned, key="wrong-assignment", value="private",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM teammate_memory WHERE id = :id"), {"id": memory_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM teammate_memory WHERE id = :id"), {"id": memory_b})
            await connection.execute(text("DELETE FROM teammate_project_assignments WHERE teammate_id IN (:a, :b)"), {"a": teammate_a, "b": teammate_b})
            await connection.execute(text("DELETE FROM teammate_profiles WHERE id IN (:a, :b)"), {"a": teammate_a, "b": teammate_b})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM projects WHERE id IN (:a, :b, :unassigned)"), {"a": project_a, "b": project_b, "unassigned": project_unassigned})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_b})
            await connection.execute(text("DELETE FROM organizations WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_handoff_rls_is_tenant_scoped_and_validates_relationships() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    teammate_a, teammate_b = uuid.uuid4(), uuid.uuid4()
    parent_a, child_a, parent_b, child_b = (uuid.uuid4() for _ in range(4))
    handoff_a, handoff_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert role == (False, False)
            protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'task_handoffs'"
                    )
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Handoff RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-handoff-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-handoff-{tenant_b}"},
                ],
            )
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, 'Handoff RLS', :slug, :tenant_id)"),
                [
                    {"id": org_a, "slug": f"rls-handoff-org-{org_a}", "tenant_id": tenant_a},
                    {"id": org_b, "slug": f"rls-handoff-org-{org_b}", "tenant_id": tenant_b},
                ],
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'Handoff RLS', :slug)"),
                {"id": project_a, "organization_id": org_a, "tenant_id": tenant_a, "slug": f"rls-handoff-project-{project_a}"},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'Handoff RLS', :slug)"),
                {"id": project_b, "organization_id": org_b, "tenant_id": tenant_b, "slug": f"rls-handoff-project-{project_b}"},
            )
            await connection.execute(
                text("INSERT INTO teammate_profiles (id, organization_id, tenant_id, name, role, agent_type) VALUES (:id, :organization_id, :tenant_id, 'Handoff RLS', 'developer', 'developer')"),
                [
                    {"id": teammate_a, "organization_id": org_a, "tenant_id": tenant_a},
                    {"id": teammate_b, "organization_id": org_b, "tenant_id": tenant_b},
                ],
            )
            await connection.execute(
                text("INSERT INTO teammate_project_assignments (id, teammate_id, project_id) VALUES (:id, :teammate_id, :project_id)"),
                [
                    {"id": uuid.uuid4(), "teammate_id": teammate_a, "project_id": project_a},
                    {"id": uuid.uuid4(), "teammate_id": teammate_b, "project_id": project_b},
                ],
            )
            for tenant_id, parent_id, child_id in (
                (tenant_a, parent_a, child_a),
                (tenant_b, parent_b, child_b),
            ):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title, parent_task_id) VALUES (:id, :tenant_id, 'Handoff task', NULL)"),
                    {"id": parent_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title, parent_task_id) VALUES (:id, :tenant_id, 'Handoff task', :parent_task_id)"),
                    {"id": child_id, "tenant_id": tenant_id, "parent_task_id": parent_id},
                )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text("INSERT INTO task_handoffs (id, tenant_id, parent_task_id, child_task_id, idempotency_key, teammate_id, project_id, delegated_from, delegated_to, reason, input_data) VALUES (:id, :tenant_id, :parent_task_id, :child_task_id, :idempotency_key, :teammate_id, :project_id, 'workflow', 'developer', 'valid', '{}')"),
                {"id": handoff_a, "tenant_id": tenant_a, "parent_task_id": parent_a, "child_task_id": child_a, "idempotency_key": uuid.uuid4(), "teammate_id": teammate_a, "project_id": project_a},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(
                text("INSERT INTO task_handoffs (id, tenant_id, parent_task_id, child_task_id, idempotency_key, teammate_id, project_id, delegated_from, delegated_to, reason, input_data) VALUES (:id, :tenant_id, :parent_task_id, :child_task_id, :idempotency_key, :teammate_id, :project_id, 'workflow', 'developer', 'valid', '{}')"),
                {"id": handoff_b, "tenant_id": tenant_b, "parent_task_id": parent_b, "child_task_id": child_b, "idempotency_key": uuid.uuid4(), "teammate_id": teammate_b, "project_id": project_b},
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskHandoff.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-handoff-a")
        async with sessions() as session:
            assert (await session.execute(select(TaskHandoff.id))).scalars().all() == [handoff_a]
            session.add(
                TaskHandoff(
                    id=uuid.uuid4(), tenant_id=tenant_b, parent_task_id=parent_b, child_task_id=child_b,
                    idempotency_key=uuid.uuid4(), teammate_id=teammate_b, project_id=project_b,
                    delegated_from="workflow", delegated_to="developer", reason="cross tenant", input_data="{}",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        async with sessions() as session:
            session.add(
                TaskHandoff(
                    id=uuid.uuid4(), tenant_id=tenant_a, parent_task_id=parent_a, child_task_id=child_b,
                    idempotency_key=uuid.uuid4(), teammate_id=teammate_a, project_id=project_a,
                    delegated_from="workflow", delegated_to="developer", reason="mismatched child", input_data="{}",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        async with sessions() as session:
            session.add(
                TaskHandoff(
                    id=uuid.uuid4(), tenant_id=tenant_a, parent_task_id=parent_a, child_task_id=child_a,
                    idempotency_key=uuid.uuid4(), teammate_id=teammate_a, project_id=project_b,
                    delegated_from="workflow", delegated_to="developer", reason="mismatched project", input_data="{}",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_b})
            await connection.execute(text("DELETE FROM teammate_project_assignments WHERE teammate_id IN (:a, :b)"), {"a": teammate_a, "b": teammate_b})
            for tenant_id, parent_id, child_id in ((tenant_a, parent_a, child_a), (tenant_b, parent_b, child_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": child_id})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": parent_id})
            await connection.execute(text("DELETE FROM teammate_profiles WHERE id IN (:a, :b)"), {"a": teammate_a, "b": teammate_b})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_b})
            await connection.execute(text("DELETE FROM organizations WHERE id IN (:a, :b)"), {"a": org_a, "b": org_b})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_dependency_rls_is_tenant_scoped_and_rejects_cross_tenant_edges() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    dependency_a, dependency_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert role == (False, False)
            protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'task_dependencies'"
                    )
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS tenant', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-deps-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-deps-{tenant_b}"},
                ],
            )
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS dependency task')"),
                    {"id": task_id, "tenant_id": tenant_id},
                )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_a)},
            )
            await connection.execute(
                text(
                    "INSERT INTO task_dependencies (id, tenant_id, task_id, depends_on_task_id) "
                    "VALUES (:id, :tenant_id, :task_id, :depends_on_task_id)"
                ),
                {
                    "id": dependency_a,
                    "tenant_id": tenant_a,
                    "task_id": task_a,
                    "depends_on_task_id": task_a,
                },
            )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_b)},
            )
            await connection.execute(
                text(
                    "INSERT INTO task_dependencies (id, tenant_id, task_id, depends_on_task_id) "
                    "VALUES (:id, :tenant_id, :task_id, :depends_on_task_id)"
                ),
                {
                    "id": dependency_b,
                    "tenant_id": tenant_b,
                    "task_id": task_b,
                    "depends_on_task_id": task_b,
                },
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskDependency.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            assert (await session.execute(select(TaskDependency.id))).scalars().all() == [dependency_a]
            session.add(
                TaskDependency(
                    id=uuid.uuid4(),
                    tenant_id=tenant_b,
                    task_id=task_b,
                    depends_on_task_id=task_b,
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        async with sessions() as session:
            session.add(
                TaskDependency(
                    id=uuid.uuid4(),
                    tenant_id=tenant_a,
                    task_id=task_a,
                    depends_on_task_id=task_b,
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM task_dependencies WHERE id = :id"), {"id": dependency_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM task_dependencies WHERE id = :id"), {"id": dependency_b})
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_verification_run_rls_and_scheduler_recovery_are_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    run_a, run_b = uuid.uuid4(), uuid.uuid4()
    stale_started_at = datetime.now(UTC) - timedelta(minutes=20)

    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert role == (False, False)
            protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'verification_runs'"
                    )
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'RLS tenant', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-runs-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-runs-{tenant_b}"},
                ],
            )
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS verification task')"),
                    {"id": task_id, "tenant_id": tenant_id},
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
            for tenant_id, task_id, workspace_id, run_id in (
                (tenant_a, task_a, workspace_a, run_a),
                (tenant_b, task_b, workspace_b, run_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO verification_runs "
                        "(id, tenant_id, task_id, workspace_id, kind, command, status, runner_version, started_at) "
                        "VALUES (:id, :tenant_id, :task_id, :workspace_id, 'test', '[]', 'running', 'test', :started_at)"
                    ),
                    {
                        "id": run_id,
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "started_at": stale_started_at,
                    },
                )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(VerificationRun.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            assert (await session.execute(select(VerificationRun.id))).scalars().all() == [run_a]
            session.add(
                VerificationRun(
                    id=uuid.uuid4(),
                    tenant_id=tenant_b,
                    task_id=task_b,
                    workspace_id=workspace_b,
                    kind="test",
                    command="[]",
                    runner_version="test",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        async with sessions() as session:
            session.add(
                VerificationRun(
                    id=uuid.uuid4(),
                    tenant_id=tenant_a,
                    task_id=task_a,
                    workspace_id=workspace_b,
                    kind="test",
                    command="[]",
                    runner_version="test",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.clear()
        from solvyn.workflows import scheduler

        monkeypatch.setattr(scheduler, "async_session_factory", sessions)
        await scheduler._interrupt_stale_verification_runs()

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            status_a = (await session.execute(select(VerificationRun.status).where(VerificationRun.id == run_a))).scalar_one()
            assert status_a == "interrupted"
        tenant_context.set(tenant_b, "rls-b")
        async with sessions() as session:
            status_b = (await session.execute(select(VerificationRun.status).where(VerificationRun.id == run_b))).scalar_one()
            assert status_b == "interrupted"
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_a})
            await connection.execute(text("DELETE FROM task_workspaces WHERE id = :id"), {"id": workspace_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_b})
            await connection.execute(text("DELETE FROM task_workspaces WHERE id = :id"), {"id": workspace_b})
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
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
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS task')"),
                    {"id": task_id, "tenant_id": tenant_id},
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
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
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
            for tenant_id, task_id, workspace_id in (
                (tenant_a, task_a, workspace_a),
                (tenant_b, task_b, workspace_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
                )
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS workspace task')"),
                    {"id": task_id, "tenant_id": tenant_id},
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
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()
