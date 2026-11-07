"""PostgreSQL-only checks for the initial tenant RLS boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request

from solvyn.db.models import (
    ApiKey,
    Approval,
    ExecutionGraph,
    Integration,
    IntegrationCredential,
    MaintenanceSchedule,
    Memory,
    OrchestrationEvent,
    Organization,
    OrganizationMembership,
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
    TeammatePermission,
    TeammateProfile,
    TeammateProjectAssignment,
    TeammateToolInvocation,
    TenantRole,
    TokenUsage,
    VerificationAttemptEvent,
    VerificationRun,
    Workspace,
)
from solvyn.multi_tenant.middleware import tenant_context


async def _seed_organizations(
    connection: AsyncConnection,
    rows: list[tuple[uuid.UUID, uuid.UUID, str, str]],
) -> None:
    for organization_id, tenant_id, slug, name in rows:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        await connection.execute(
            text("INSERT INTO organizations (id, name, slug, tenant_id) VALUES (:id, :name, :slug, :tenant_id)"),
            {"id": organization_id, "name": name, "slug": slug, "tenant_id": tenant_id},
        )


async def _delete_organizations(
    connection: AsyncConnection,
    rows: list[tuple[uuid.UUID, uuid.UUID]],
) -> None:
    for tenant_id, organization_id in rows:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )
        await connection.execute(
            text("DELETE FROM organizations WHERE id = :id"),
            {"id": organization_id},
        )


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
                        "'teammate_tool_invocations', 'task_dependencies', 'verification_runs', 'verification_attempt_events', "
                        "'task_handoffs', 'teammate_memory', 'sessions', 'execution_graphs', 'memory', 'token_usage', "
                        "'approvals', 'proactive_events', 'maintenance_schedules', 'integrations', "
                        "'integration_credentials', 'projects', 'repositories', 'project_knowledge', "
                        "'teammate_profiles', 'teammate_permissions', 'teammate_project_assignments', "
                        "'organizations', 'workspaces')"
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
                "verification_attempt_events",
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
                "teammate_profiles",
                "teammate_permissions",
                "teammate_project_assignments",
                "organizations",
                "workspaces",
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
            attempt_history_protected = (
                await connection.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'verification_attempt_events'"
                    )
                )
            ).one()
            assert attempt_history_protected == (True, True)

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
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-approval-{organization_a}", "RLS approvals"),
                    (organization_a_other, tenant_a, f"rls-approval-{organization_a_other}", "RLS approvals"),
                    (organization_b, tenant_b, f"rls-approval-{organization_b}", "RLS approvals"),
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
            await _delete_organizations(
                connection,
                [(tenant_a, organization_a), (tenant_a, organization_a_other), (tenant_b, organization_b)],
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
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-proactive-{organization_a}", "RLS proactive"),
                    (organization_a_other, tenant_a, f"rls-proactive-{organization_a_other}", "RLS proactive"),
                    (organization_b, tenant_b, f"rls-proactive-{organization_b}", "RLS proactive"),
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
            await _delete_organizations(
                connection,
                [(tenant_a, organization_a), (tenant_a, organization_a_other), (tenant_b, organization_b)],
            )
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_background_worker_webhook_and_maintenance_writes_are_tenant_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise worker discovery and writes using the non-bypass PostgreSQL runtime role."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.api.v1.webhooks import receive_github_webhook
    from solvyn.workflows import scheduler
    from solvyn.workflows.scheduler import _queue_due_maintenance_events

    secret = "postgres-worker-rls-test-secret"
    monkeypatch.setattr("solvyn.api.v1.webhooks.settings.github_webhook_secret", secret)
    monkeypatch.setattr(scheduler, "_BATCH_SIZE", 1000)
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    repository_a, repository_b = uuid.uuid4(), uuid.uuid4()
    schedule_a, schedule_b = uuid.uuid4(), uuid.uuid4()
    delivery_id = str(uuid.uuid4())
    body = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "Shared/Worker-RLS"},
            "workflow_run": {
                "id": 8142,
                "run_attempt": 1,
                "conclusion": "failure",
                "name": "Tenant isolation",
                "head_branch": "main",
                "html_url": "https://github.com/Shared/Worker-RLS/actions/runs/8142",
            },
        }
    ).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/webhooks/github",
            "raw_path": b"/api/v1/webhooks/github",
            "query_string": b"",
            "headers": [
                (b"content-length", str(len(body)).encode()),
                (b"x-github-event", b"workflow_run"),
                (b"x-github-delivery", delivery_id.encode()),
                (b"x-hub-signature-256", signature.encode()),
            ],
            "server": ("example.test", 443),
            "client": ("127.0.0.1", 1234),
        },
        receive,
    )

    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()
            assert role == (False, False)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Worker RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"worker-rls-{tenant_a}"},
                    {"id": tenant_b, "slug": f"worker-rls-{tenant_b}"},
                ],
            )
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"worker-rls-{organization_a}", "Worker RLS"),
                    (organization_b, tenant_b, f"worker-rls-{organization_b}", "Worker RLS"),
                ],
            )
            for tenant_id, organization_id, project_id, repository_id, schedule_id in (
                (tenant_a, organization_a, project_a, repository_a, schedule_a),
                (tenant_b, organization_b, project_b, repository_b, schedule_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO projects (id, organization_id, tenant_id, name, slug) "
                        "VALUES (:id, :organization_id, :tenant_id, 'Worker RLS', :slug)"
                    ),
                    {
                        "id": project_id,
                        "organization_id": organization_id,
                        "tenant_id": tenant_id,
                        "slug": f"worker-rls-{project_id}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO repositories (id, project_id, full_name, provider) "
                        "VALUES (:id, :project_id, 'Shared/Worker-RLS', 'github')"
                    ),
                    {"id": repository_id, "project_id": project_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO maintenance_schedules "
                        "(id, organization_id, project_id, maintenance_type, title, interval_days, next_run_at) "
                        "VALUES (:id, :organization_id, :project_id, 'code_quality', 'Worker RLS', 7, "
                        "now() - interval '1 day')"
                    ),
                    {"id": schedule_id, "organization_id": organization_id, "project_id": project_id},
                )

        tenant_context.clear()
        async with sessions() as session:
            response = await receive_github_webhook(request, db=session)
            assert response["created_count"] == 2
            assert await _queue_due_maintenance_events(session) >= 2

        for tenant_id, project_id, schedule_id in (
            (tenant_a, project_a, schedule_a),
            (tenant_b, project_b, schedule_b),
        ):
            tenant_context.set(tenant_id, f"worker-rls-{tenant_id}")
            async with sessions() as session:
                events = (
                    await session.execute(select(ProactiveEvent).order_by(ProactiveEvent.source_event_id))
                ).scalars().all()
                assert len(events) == 2
                assert {event.project_id for event in events} == {project_id}
                schedule = await session.get(MaintenanceSchedule, schedule_id)
                assert schedule is not None
                assert schedule.next_run_at > datetime.now(UTC) - timedelta(minutes=1)
        tenant_context.clear()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id in (tenant_a, tenant_b):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("DELETE FROM proactive_events WHERE project_id IN (:a, :b)"),
                    {"a": project_a, "b": project_b},
                )
                await connection.execute(
                    text("DELETE FROM maintenance_schedules WHERE id IN (:a, :b)"),
                    {"a": schedule_a, "b": schedule_b},
                )
                await connection.execute(
                    text("DELETE FROM repositories WHERE id IN (:a, :b)"),
                    {"a": repository_a, "b": repository_b},
                )
                await connection.execute(
                    text("DELETE FROM projects WHERE id IN (:a, :b)"),
                    {"a": project_a, "b": project_b},
                )
                await connection.execute(
                    text("DELETE FROM organizations WHERE id IN (:a, :b)"),
                    {"a": organization_a, "b": organization_b},
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
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-integration-{organization_a}", "RLS integrations"),
                    (organization_b, tenant_b, f"rls-integration-{organization_b}", "RLS integrations"),
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
            await _delete_organizations(
                connection,
                [(tenant_a, organization_a), (tenant_b, organization_b)],
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
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-project-{organization_a}", "RLS projects"),
                    (organization_b, tenant_b, f"rls-project-{organization_b}", "RLS projects"),
                    (organization_a_other, tenant_a, f"rls-project-{organization_a_other}", "RLS projects"),
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
            await _delete_organizations(
                connection,
                [(tenant_a, organization_a), (tenant_b, organization_b), (tenant_a, organization_a_other)],
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
    execution_a, execution_b, execution_expired = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

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
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text(
                    "INSERT INTO execution_graphs "
                    "(id, session_id, graph_data, status, lease_owner, lease_expires_at) "
                    "VALUES (:id, :session_id, '{}', 'resuming', :lease_owner, now() - interval '1 minute')"
                ),
                {"id": execution_expired, "session_id": session_a, "lease_owner": uuid.uuid4()},
            )

        tenant_context.clear()
        async with sessions() as session:
            due = await _fetch_due_scheduled_workflows(session)
            assert set(due) >= {
                (execution_a, tenant_a),
                (execution_b, tenant_b),
                (execution_expired, tenant_a),
            }
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": execution_expired})
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
            await _seed_organizations(
                connection,
                [
                    (org_a, tenant_a, f"rls-memory-org-{org_a}", "Memory RLS"),
                    (org_b, tenant_b, f"rls-memory-org-{org_b}", "Memory RLS"),
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
            for tenant_id, organization_id, teammate_id, project_id in (
                (tenant_a, org_a, teammate_a, project_a),
                (tenant_b, org_b, teammate_b, project_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO teammate_profiles (id, organization_id, tenant_id, name, role, agent_type) VALUES (:id, :organization_id, :tenant_id, 'Memory RLS', 'developer', 'developer')"),
                    {"id": teammate_id, "organization_id": organization_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text("INSERT INTO teammate_project_assignments (id, teammate_id, project_id) VALUES (:id, :teammate_id, :project_id)"),
                    {"id": uuid.uuid4(), "teammate_id": teammate_id, "project_id": project_id},
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
            for tenant_id, teammate_id in ((tenant_a, teammate_a), (tenant_b, teammate_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM teammate_project_assignments WHERE teammate_id = :id"), {"id": teammate_id})
                await connection.execute(text("DELETE FROM teammate_profiles WHERE id = :id"), {"id": teammate_id})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM projects WHERE id IN (:a, :b, :unassigned)"), {"a": project_a, "b": project_b, "unassigned": project_unassigned})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_b})
            await _delete_organizations(connection, [(tenant_a, org_a), (tenant_b, org_b)])
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
    approval_a, approval_b = uuid.uuid4(), uuid.uuid4()

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
                        "SELECT relation_row.relname, relation_row.relrowsecurity, relation_row.relforcerowsecurity "
                        "FROM pg_class relation_row JOIN pg_namespace namespace_row "
                        "ON namespace_row.oid = relation_row.relnamespace "
                        "WHERE namespace_row.nspname = 'public' "
                        "AND relation_row.relname IN ('task_handoffs', 'orchestration_events')"
                    )
                )
            ).all()
            assert {row[0] for row in protected} == {"task_handoffs", "orchestration_events"}
            assert all(row[1:] == (True, True) for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Handoff RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-handoff-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-handoff-{tenant_b}"},
                ],
            )
            await _seed_organizations(
                connection,
                [
                    (org_a, tenant_a, f"rls-handoff-org-{org_a}", "Handoff RLS"),
                    (org_b, tenant_b, f"rls-handoff-org-{org_b}", "Handoff RLS"),
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
            for tenant_id, organization_id, teammate_id, project_id in (
                (tenant_a, org_a, teammate_a, project_a),
                (tenant_b, org_b, teammate_b, project_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO teammate_profiles (id, organization_id, tenant_id, name, role, agent_type) VALUES (:id, :organization_id, :tenant_id, 'Handoff RLS', 'developer', 'developer')"),
                    {"id": teammate_id, "organization_id": organization_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text("INSERT INTO teammate_project_assignments (id, teammate_id, project_id) VALUES (:id, :teammate_id, :project_id)"),
                    {"id": uuid.uuid4(), "teammate_id": teammate_id, "project_id": project_id},
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
            await connection.execute(
                text(
                    "INSERT INTO approvals "
                    "(id, organization_id, project_id, teammate_id, task_handoff_id, kind, summary, requested_by) "
                    "VALUES (:id, :organization_id, :project_id, :teammate_id, :handoff_id, "
                    "'handoff', 'Review handoff', 'developer')"
                ),
                {
                    "id": approval_b, "organization_id": org_b, "project_id": project_b,
                    "teammate_id": teammate_b, "handoff_id": handoff_b,
                },
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(
                text(
                    "INSERT INTO approvals "
                    "(id, organization_id, project_id, teammate_id, task_handoff_id, kind, summary, requested_by) "
                    "VALUES (:id, :organization_id, :project_id, :teammate_id, :handoff_id, "
                    "'handoff', 'Review handoff', 'developer')"
                ),
                {
                    "id": approval_a, "organization_id": org_a, "project_id": project_a,
                    "teammate_id": teammate_a, "handoff_id": handoff_a,
                },
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TaskHandoff.id))).scalars().all() == []
            assert (await session.execute(select(OrchestrationEvent.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-handoff-a")
        async with sessions() as session:
            assert (await session.execute(select(TaskHandoff.id))).scalars().all() == [handoff_a]
            initial_history = (
                await session.execute(select(OrchestrationEvent).order_by(OrchestrationEvent.entity_type))
            ).scalars().all()
            assert {(item.entity_type, item.entity_id, item.status) for item in initial_history} == {
                ("approval", approval_a, "pending"),
                ("handoff", handoff_a, "pending"),
            }
            assert {item.task_id for item in initial_history} == {child_a}
            actor_id = uuid.uuid4()
            await session.execute(
                text("SELECT set_config('app.task_event_actor_user_id', :actor, true)"),
                {"actor": str(actor_id)},
            )
            await session.execute(
                text("UPDATE task_handoffs SET status = 'running' WHERE id = :id"),
                {"id": handoff_a},
            )
            await session.execute(
                text("UPDATE approvals SET status = 'approved' WHERE id = :id"),
                {"id": approval_a},
            )
            transitions = (
                await session.execute(
                    select(OrchestrationEvent)
                    .where(OrchestrationEvent.event_type == "status_changed")
                    .order_by(OrchestrationEvent.entity_type)
                )
            ).scalars().all()
            assert {(item.entity_type, item.previous_status, item.status) for item in transitions} == {
                ("approval", "pending", "approved"),
                ("handoff", "pending", "running"),
            }
            assert {item.task_id for item in transitions} == {child_a}
            assert all(item.actor_user_id == actor_id for item in transitions)
            await session.commit()
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("INSERT INTO orchestration_events "
                         "(tenant_id, entity_type, entity_id, event_type, status, source) "
                         "VALUES (:tenant_id, 'approval', :entity_id, 'created', 'pending', 'test')"),
                    {"tenant_id": tenant_a, "entity_id": uuid.uuid4()},
                )
            await session.rollback()
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE orchestration_events SET status = 'tampered' WHERE entity_id = :entity_id"),
                    {"entity_id": handoff_a},
                )
            await session.rollback()
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

        tenant_context.set(tenant_b, "rls-handoff-b")
        async with sessions() as session:
            tenant_b_history = (await session.execute(select(OrchestrationEvent))).scalars().all()
            assert {(item.entity_type, item.entity_id, item.status) for item in tenant_b_history} == {
                ("approval", approval_b, "pending"),
                ("handoff", handoff_b, "pending"),
            }
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_a})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_b})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_b})
            for tenant_id, teammate_id in ((tenant_a, teammate_a), (tenant_b, teammate_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM teammate_project_assignments WHERE teammate_id = :id"), {"id": teammate_id})
            for tenant_id, parent_id, child_id in ((tenant_a, parent_a, child_a), (tenant_b, parent_b, child_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": child_id})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": parent_id})
            for tenant_id, teammate_id in ((tenant_a, teammate_a), (tenant_b, teammate_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM teammate_profiles WHERE id = :id"), {"id": teammate_id})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_b})
            await _delete_organizations(connection, [(tenant_a, org_a), (tenant_b, org_b)])
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
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    teammate_a, teammate_b = uuid.uuid4(), uuid.uuid4()
    parent_a, parent_b = uuid.uuid4(), uuid.uuid4()
    task_a, task_b = uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_b = uuid.uuid4(), uuid.uuid4()
    run_a, run_b, run_exhausted, run_approved_a, run_approved_b = (uuid.uuid4() for _ in range(5))
    handoff_a, handoff_b = uuid.uuid4(), uuid.uuid4()
    approval_a, approval_b, approval_approved_a, approval_approved_b = (uuid.uuid4() for _ in range(4))
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
            await _seed_organizations(
                connection,
                [
                    (org_a, tenant_a, f"rls-runs-org-{org_a}", "Verification RLS"),
                    (org_b, tenant_b, f"rls-runs-org-{org_b}", "Verification RLS"),
                ],
            )
            for tenant_id, organization_id, project_id, teammate_id in (
                (tenant_a, org_a, project_a, teammate_a),
                (tenant_b, org_b, project_b, teammate_b),
            ):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(
                    text(
                        "INSERT INTO projects (id, organization_id, tenant_id, name, slug) "
                        "VALUES (:id, :organization_id, :tenant_id, 'Verification RLS', :slug)"
                    ),
                    {"id": project_id, "organization_id": organization_id, "tenant_id": tenant_id, "slug": f"rls-runs-project-{project_id}"},
                )
                await connection.execute(
                    text(
                        "INSERT INTO teammate_profiles "
                        "(id, organization_id, tenant_id, name, role, agent_type) "
                        "VALUES (:id, :organization_id, :tenant_id, 'Verification RLS', 'developer', 'developer')"
                    ),
                    {"id": teammate_id, "organization_id": organization_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO teammate_project_assignments (id, teammate_id, project_id) "
                        "VALUES (:id, :teammate_id, :project_id)"
                    ),
                    {"id": uuid.uuid4(), "teammate_id": teammate_id, "project_id": project_id},
                )
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                parent_id = parent_a if tenant_id == tenant_a else parent_b
                await connection.execute(
                    text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS verification parent')"),
                    {"id": parent_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO tasks (id, tenant_id, title, parent_task_id) "
                        "VALUES (:id, :tenant_id, 'RLS verification task', :parent_id)"
                    ),
                    {"id": task_id, "tenant_id": tenant_id, "parent_id": parent_id},
                )
            for tenant_id, parent_id, child_id, teammate_id, project_id, handoff_id in (
                (tenant_a, parent_a, task_a, teammate_a, project_a, handoff_a),
                (tenant_b, parent_b, task_b, teammate_b, project_b, handoff_b),
            ):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(
                    text(
                        "INSERT INTO task_handoffs "
                        "(id, tenant_id, parent_task_id, child_task_id, idempotency_key, teammate_id, project_id, "
                        "delegated_from, delegated_to, reason, input_data) "
                        "VALUES (:id, :tenant_id, :parent_id, :child_id, :key, :teammate_id, :project_id, "
                        "'workflow', 'developer', 'verification RLS', '{}')"
                    ),
                    {
                        "id": handoff_id,
                        "tenant_id": tenant_id,
                        "parent_id": parent_id,
                        "child_id": child_id,
                        "key": uuid.uuid4(),
                        "teammate_id": teammate_id,
                        "project_id": project_id,
                    },
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
            for tenant_id, task_id, workspace_id, run_id, attempt_count in (
                (tenant_a, task_a, workspace_a, run_a, 1),
                (tenant_b, task_b, workspace_b, run_b, 1),
                (tenant_a, task_a, workspace_a, run_exhausted, 3),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO verification_runs "
                        "(id, tenant_id, task_id, workspace_id, kind, command, status, runner_version, started_at, attempt_count, source_digest) "
                        "VALUES (:id, :tenant_id, :task_id, :workspace_id, 'test', '[]', 'approved', 'test', :started_at, 0, :source_digest)"
                    ),
                    {
                        "id": run_id,
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "started_at": stale_started_at,
                        "source_digest": "a" * 64,
                    },
                )
                await connection.execute(
                    text(
                        "UPDATE verification_runs SET status = 'running', attempt_count = :attempt_count, "
                        "attempt_started_at = :started_at, lease_expires_at = :started_at WHERE id = :id"
                    ),
                    {"id": run_id, "attempt_count": attempt_count, "started_at": stale_started_at},
                )
                if run_id in {run_a, run_b}:
                    organization_id, project_id, teammate_id, handoff_id = (
                        (org_a, project_a, teammate_a, handoff_a)
                        if tenant_id == tenant_a
                        else (org_b, project_b, teammate_b, handoff_b)
                    )
                    approval_id = approval_a if tenant_id == tenant_a else approval_b
                    await connection.execute(
                        text(
                            "INSERT INTO approvals "
                            "(id, organization_id, project_id, teammate_id, task_handoff_id, verification_run_id, "
                            "kind, summary, status, requested_by) "
                            "VALUES (:id, :organization_id, :project_id, :teammate_id, :handoff_id, :run_id, "
                            "'verification', 'Run fixed verification', 'approved', 'owner')"
                        ),
                        {
                            "id": approval_id,
                            "organization_id": organization_id,
                            "project_id": project_id,
                            "teammate_id": teammate_id,
                            "handoff_id": handoff_id,
                            "run_id": run_id,
                        },
                    )
                if run_id == run_a:
                    await connection.execute(
                        text("UPDATE verification_runs SET stdout = repeat('x', 40000) WHERE id = :id"),
                        {"id": run_id},
                    )
            for tenant_id, task_id, workspace_id, run_id, approval_id in (
                (tenant_a, task_a, workspace_a, run_approved_a, approval_approved_a),
                (tenant_b, task_b, workspace_b, run_approved_b, approval_approved_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO verification_runs "
                        "(id, tenant_id, task_id, workspace_id, kind, command, status, runner_version, source_digest) "
                        "VALUES (:id, :tenant_id, :task_id, :workspace_id, 'test', '[]', 'approved', 'test', :source_digest)"
                    ),
                    {
                        "id": run_id,
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "workspace_id": workspace_id,
                        "source_digest": "a" * 64,
                    },
                )
                organization_id, project_id, teammate_id = (
                    (org_a, project_a, teammate_a)
                    if tenant_id == tenant_a
                    else (org_b, project_b, teammate_b)
                )
                await connection.execute(
                    text(
                        "INSERT INTO approvals "
                        "(id, organization_id, project_id, teammate_id, verification_run_id, kind, summary, status, requested_by) "
                        "VALUES (:id, :organization_id, :project_id, :teammate_id, :run_id, "
                        "'verification', 'Run fixed verification', 'approved', 'owner')"
                    ),
                    {
                        "id": approval_id,
                        "organization_id": organization_id,
                        "project_id": project_id,
                        "teammate_id": teammate_id,
                        "run_id": run_id,
                    },
                )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(VerificationRun.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            assert set((await session.execute(select(VerificationRun.id))).scalars().all()) == {
                run_a,
                run_exhausted,
                run_approved_a,
            }
            started_events = (
                await session.execute(
                    select(VerificationAttemptEvent.verification_run_id, VerificationAttemptEvent.status)
                    .where(VerificationAttemptEvent.verification_run_id.in_([run_a, run_exhausted]))
                )
            ).all()
            assert set(started_events) == {(run_a, "running"), (run_exhausted, "running")}
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

            session.add(
                VerificationAttemptEvent(
                    tenant_id=tenant_a,
                    task_id=task_a,
                    verification_run_id=run_a,
                    verification_kind="test",
                    attempt_number=1,
                    event_type="snapshot",
                    status="running",
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
            exhausted = (
                await session.execute(
                    select(VerificationRun.status, VerificationRun.lease_owner, VerificationRun.lease_expires_at)
                    .where(VerificationRun.id == run_exhausted)
                )
            ).one()
            assert exhausted == ("dead_letter", None, None)
            attempt_events_a = (
                await session.execute(
                    select(VerificationAttemptEvent.verification_run_id, VerificationAttemptEvent.event_type, VerificationAttemptEvent.status)
                    .where(VerificationAttemptEvent.verification_run_id.in_([run_a, run_exhausted]))
                    .order_by(VerificationAttemptEvent.verification_run_id)
                )
            ).all()
            assert set(attempt_events_a) == {
                (run_a, "started", "running"),
                (run_a, "finished", "interrupted"),
                (run_exhausted, "started", "running"),
                (run_exhausted, "finished", "dead_letter"),
            }
            retained_output = (
                await session.execute(
                    select(
                        VerificationAttemptEvent.stdout,
                        VerificationAttemptEvent.output_truncated,
                    ).where(
                        VerificationAttemptEvent.verification_run_id == run_a,
                        VerificationAttemptEvent.event_type == "finished",
                    )
                )
            ).one()
            assert len(retained_output.stdout) == 32768
            assert retained_output.output_truncated is True
            with pytest.raises(DBAPIError):
                await session.execute(
                    update(VerificationAttemptEvent)
                    .where(VerificationAttemptEvent.verification_run_id == run_a)
                    .values(status="passed")
                )
            await session.rollback()
        tenant_context.set(tenant_b, "rls-b")
        async with sessions() as session:
            status_b = (await session.execute(select(VerificationRun.status).where(VerificationRun.id == run_b))).scalar_one()
            assert status_b == "interrupted"
            events_b = (
                await session.execute(
                    select(VerificationAttemptEvent.verification_run_id, VerificationAttemptEvent.status)
                    .where(VerificationAttemptEvent.verification_run_id == run_b)
                )
            ).all()
            assert set(events_b) == {(run_b, "running"), (run_b, "interrupted")}

        tenant_context.clear()
        monkeypatch.setattr(scheduler, "_VERIFICATION_BATCH_SIZE", 100)
        async with sessions() as session:
            due_runs = await scheduler._fetch_due_verification_runs(session)
        assert {
            (run_a, tenant_a),
            (run_b, tenant_b),
            (run_approved_a, tenant_a),
            (run_approved_b, tenant_b),
        } <= set(due_runs)

        class FakeWorkspaceManager:
            def get(self, workspace_id: uuid.UUID) -> object:
                return workspace_id

            def fingerprint(self, workspace: object) -> str:
                return "a" * 64

        class FakeSandboxRunner:
            async def run(self, workspace: object, command: list[str]) -> object:
                from solvyn.sandbox.runtime import SandboxResult

                return SandboxResult(
                    uuid.uuid4(), tuple(command), 0, False, "ok", "", False, 0.01
                )

        from solvyn.api.v1 import verification

        monkeypatch.setattr(verification, "_workspace_manager", lambda: FakeWorkspaceManager())
        monkeypatch.setattr(verification, "_sandbox_runner", lambda: FakeSandboxRunner())
        await scheduler._execute_due_verification(run_a, tenant_b)
        tenant_context.set(tenant_a, "rls-a")
        async with sessions() as session:
            assert (
                await session.execute(select(VerificationRun.status).where(VerificationRun.id == run_a))
            ).scalar_one() == "interrupted"
        tenant_context.clear()
        await scheduler._execute_due_verification(run_a, tenant_a)
        await scheduler._execute_due_verification(run_b, tenant_b)
        await scheduler._execute_due_verification(run_approved_a, tenant_a)
        await scheduler._execute_due_verification(run_approved_b, tenant_b)
        for tenant_id, run_id, approval_id in ((tenant_a, run_a, approval_a), (tenant_b, run_b, approval_b)):
            tenant_context.set(tenant_id, "verification-claim")
            async with sessions() as session:
                run_state = (
                    await session.execute(
                        select(
                            VerificationRun.status,
                            VerificationRun.attempt_count,
                            VerificationRun.lease_owner,
                            VerificationRun.lease_expires_at,
                        ).where(VerificationRun.id == run_id)
                    )
                ).one()
                approval_status = (
                    await session.execute(select(Approval.status).where(Approval.id == approval_id))
                ).scalar_one()
                attempt_statuses = (
                    await session.execute(
                        select(VerificationAttemptEvent.status).where(
                            VerificationAttemptEvent.verification_run_id == run_id,
                            VerificationAttemptEvent.event_type == "finished",
                        )
                    )
                ).scalars().all()
                assert run_state == ("passed", 2, None, None)
                assert approval_status == "consumed"
                assert set(attempt_statuses) == {"interrupted", "passed"}
        for tenant_id, run_id, approval_id in (
            (tenant_a, run_approved_a, approval_approved_a),
            (tenant_b, run_approved_b, approval_approved_b),
        ):
            tenant_context.set(tenant_id, "verification-approved-claim")
            async with sessions() as session:
                run_state = (
                    await session.execute(
                        select(
                            VerificationRun.status,
                            VerificationRun.attempt_count,
                            VerificationRun.lease_owner,
                            VerificationRun.lease_expires_at,
                        ).where(VerificationRun.id == run_id)
                    )
                ).one()
                approval_status = (
                    await session.execute(select(Approval.status).where(Approval.id == approval_id))
                ).scalar_one()
                attempt_statuses = (
                    await session.execute(
                        select(VerificationAttemptEvent.status).where(
                            VerificationAttemptEvent.verification_run_id == run_id,
                            VerificationAttemptEvent.event_type == "finished",
                        )
                    )
                ).scalars().all()
                assert run_state == ("passed", 1, None, None)
                assert approval_status == "consumed"
                assert attempt_statuses == ["passed"]
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_a)})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_a})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_approved_a})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_a})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_exhausted})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_approved_a})
            await connection.execute(text("DELETE FROM task_workspaces WHERE id = :id"), {"id": workspace_a})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_a})
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_b)})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_b})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_approved_b})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_b})
            await connection.execute(text("DELETE FROM verification_runs WHERE id = :id"), {"id": run_approved_b})
            await connection.execute(text("DELETE FROM task_workspaces WHERE id = :id"), {"id": workspace_b})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_b})
            for tenant_id, teammate_id in ((tenant_a, teammate_a), (tenant_b, teammate_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM teammate_project_assignments WHERE teammate_id = :id"), {"id": teammate_id})
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
                parent_id = parent_a if tenant_id == tenant_a else parent_b
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": parent_id})
            for tenant_id, teammate_id in ((tenant_a, teammate_a), (tenant_b, teammate_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM teammate_profiles WHERE id = :id"), {"id": teammate_id})
            for tenant_id, project_id in ((tenant_a, project_a), (tenant_b, project_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
            await _delete_organizations(connection, [(tenant_a, org_a), (tenant_b, org_b)])
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
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-org-{organization_a}", "RLS org"),
                    (organization_b, tenant_b, f"rls-org-{organization_b}", "RLS org"),
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
            for tenant_id, organization_id, teammate_id in (
                (tenant_a, organization_a, teammate_a),
                (tenant_b, organization_b, teammate_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO teammate_profiles "
                        "(id, organization_id, tenant_id, name, role, agent_type) "
                        "VALUES (:id, :organization_id, :tenant_id, 'RLS teammate', 'software_engineer', 'engineer')"
                    ),
                    {"id": teammate_id, "organization_id": organization_id, "tenant_id": tenant_id},
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
            for tenant_id, teammate_id in ((tenant_a, teammate_a), (tenant_b, teammate_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM teammate_profiles WHERE id = :id"), {"id": teammate_id})
            for tenant_id, task_id in ((tenant_a, task_a), (tenant_b, task_b)):
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
                await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task_id})
            await _delete_organizations(connection, [(tenant_a, organization_a), (tenant_b, organization_b)])
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()


@pytest.mark.asyncio
async def test_teammate_authorization_rls_is_tenant_and_organization_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    org_a, org_a_other, org_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_a, project_a_other_org, project_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    teammate_a, teammate_b = uuid.uuid4(), uuid.uuid4()
    permission_a, permission_b = uuid.uuid4(), uuid.uuid4()
    assignment_a, assignment_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('teammate_profiles', 'teammate_permissions', "
                        "'teammate_project_assignments') ORDER BY relname"
                    )
                )
            ).all()
            assert [row[0] for row in protected] == [
                "teammate_permissions",
                "teammate_profiles",
                "teammate_project_assignments",
            ]
            assert all(row[1] and row[2] for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Teammate RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-teammate-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-teammate-{tenant_b}"},
                ],
            )
            await _seed_organizations(
                connection,
                [
                    (org_a, tenant_a, f"rls-teammate-org-{org_a}", "Teammate RLS"),
                    (org_a_other, tenant_a, f"rls-teammate-org-{org_a_other}", "Teammate RLS"),
                    (org_b, tenant_b, f"rls-teammate-org-{org_b}", "Teammate RLS"),
                ],
            )
            for tenant_id, organization_id, project_id in (
                (tenant_a, org_a, project_a),
                (tenant_a, org_a_other, project_a_other_org),
                (tenant_b, org_b, project_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO projects (id, organization_id, tenant_id, name, slug) "
                        "VALUES (:id, :organization_id, :tenant_id, 'Teammate RLS', :slug)"
                    ),
                    {
                        "id": project_id,
                        "organization_id": organization_id,
                        "tenant_id": tenant_id,
                        "slug": f"rls-teammate-project-{project_id}",
                    },
                )
                if project_id in (project_a, project_b):
                    teammate_id = teammate_a if tenant_id == tenant_a else teammate_b
                    permission_id = permission_a if tenant_id == tenant_a else permission_b
                    assignment_id = assignment_a if tenant_id == tenant_a else assignment_b
                    await connection.execute(
                        text(
                            "INSERT INTO teammate_profiles "
                            "(id, organization_id, tenant_id, name, role, agent_type) "
                            "VALUES (:id, :organization_id, :tenant_id, 'Teammate RLS', 'developer', 'developer')"
                        ),
                        {"id": teammate_id, "organization_id": organization_id, "tenant_id": tenant_id},
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO teammate_permissions (id, teammate_id, permission) "
                            "VALUES (:id, :teammate_id, 'READ_ONLY')"
                        ),
                        {"id": permission_id, "teammate_id": teammate_id},
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO teammate_project_assignments (id, teammate_id, project_id) "
                            "VALUES (:id, :teammate_id, :project_id)"
                        ),
                        {"id": assignment_id, "teammate_id": teammate_id, "project_id": project_id},
                    )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(TeammateProfile.id))).scalars().all() == []
            assert (await session.execute(select(TeammatePermission.id))).scalars().all() == []
            assert (await session.execute(select(TeammateProjectAssignment.id))).scalars().all() == []
            with pytest.raises(DBAPIError):
                session.add(
                    TeammateProfile(
                        id=uuid.uuid4(), organization_id=org_a, tenant_id=tenant_a,
                        name="Unscoped", role="developer", agent_type="developer",
                    )
                )
                await session.flush()
            await session.rollback()

        tenant_context.set(tenant_a, "rls-teammate-a")
        async with sessions() as session:
            assert (await session.execute(select(TeammateProfile.id))).scalars().all() == [teammate_a]
            assert (await session.execute(select(TeammatePermission.id))).scalars().all() == [permission_a]
            assert (await session.execute(select(TeammateProjectAssignment.id))).scalars().all() == [assignment_a]
            await session.execute(
                text("DELETE FROM teammate_profiles WHERE id = :id"), {"id": teammate_b}
            )
            await session.execute(
                text("DELETE FROM teammate_permissions WHERE id = :id"), {"id": permission_b}
            )
            await session.execute(
                text("DELETE FROM teammate_project_assignments WHERE id = :id"), {"id": assignment_b}
            )

            invalid_profiles = (
                {"id": uuid.uuid4(), "organization_id": org_b, "tenant_id": tenant_b},
                {"id": uuid.uuid4(), "organization_id": org_a, "tenant_id": tenant_b},
            )
            for values in invalid_profiles:
                session.add(
                    TeammateProfile(
                        **values, name="Invalid", role="developer", agent_type="developer"
                    )
                )
                with pytest.raises(DBAPIError):
                    await session.flush()
                await session.rollback()

            session.add(
                TeammatePermission(
                    id=uuid.uuid4(), teammate_id=teammate_b, permission="MODIFY_FILES"
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

            for project_id in (project_b, project_a_other_org):
                session.add(
                    TeammateProjectAssignment(
                        id=uuid.uuid4(), teammate_id=teammate_a, project_id=project_id
                    )
                )
                with pytest.raises(DBAPIError):
                    await session.flush()
                await session.rollback()
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, teammate_id, permission_id, assignment_id in (
                (tenant_a, teammate_a, permission_a, assignment_a),
                (tenant_b, teammate_b, permission_b, assignment_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("DELETE FROM teammate_project_assignments WHERE id = :id"),
                    {"id": assignment_id},
                )
                await connection.execute(
                    text("DELETE FROM teammate_permissions WHERE id = :id"),
                    {"id": permission_id},
                )
                await connection.execute(
                    text("DELETE FROM teammate_profiles WHERE id = :id"),
                    {"id": teammate_id},
                )
                for project_id in (
                    (project_a, project_a_other_org) if tenant_id == tenant_a else (project_b,)
                ):
                    await connection.execute(
                        text("DELETE FROM projects WHERE id = :id"), {"id": project_id}
                    )
            await _delete_organizations(
                connection,
                [(tenant_a, org_a), (tenant_a, org_a_other), (tenant_b, org_b)],
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_organization_workspace_rls_is_tenant_scoped_and_preserves_project_links() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_a_other, organization_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    workspace_a, workspace_a_other, workspace_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname IN ('organizations', 'workspaces')"
                    )
                )
            ).all()
            assert {row[0] for row in protected} == {"organizations", "workspaces"}
            assert all(row[1] and row[2] for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Organization RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-organization-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-organization-{tenant_b}"},
                ],
            )
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-organization-org-{organization_a}", "Organization RLS"),
                    (organization_a_other, tenant_a, f"rls-organization-org-{organization_a_other}", "Organization RLS"),
                    (organization_b, tenant_b, f"rls-organization-org-{organization_b}", "Organization RLS"),
                ],
            )
            for tenant_id, organization_id, workspace_id, project_id in (
                (tenant_a, organization_a, workspace_a, project_a),
                (tenant_b, organization_b, workspace_b, project_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO workspaces (id, organization_id, name, slug) VALUES (:id, :organization_id, 'RLS workspace', :slug)"),
                    {
                        "id": workspace_id,
                        "organization_id": organization_id,
                        "slug": f"rls-workspace-{workspace_id}",
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO projects (id, organization_id, workspace_id, tenant_id, name, slug) "
                        "VALUES (:id, :organization_id, :workspace_id, :tenant_id, 'Organization RLS', :slug)"
                    ),
                    {
                        "id": project_id,
                        "organization_id": organization_id,
                        "workspace_id": workspace_id,
                        "tenant_id": tenant_id,
                        "slug": f"rls-organization-project-{project_id}",
                    },
                )
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_a)},
            )
            await connection.execute(
                text("INSERT INTO workspaces (id, organization_id, name, slug) VALUES (:id, :organization_id, 'RLS workspace', :slug)"),
                {
                    "id": workspace_a_other,
                    "organization_id": organization_a_other,
                    "slug": f"rls-workspace-{workspace_a_other}",
                },
            )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(Organization.id))).scalars().all() == []
            assert (await session.execute(select(Workspace.id))).scalars().all() == []
            assert (await session.execute(select(Project.id))).scalars().all() == []

        tenant_context.set(tenant_a, "rls-organization-a")
        async with sessions() as session:
            assert set((await session.execute(select(Organization.id))).scalars().all()) == {
                organization_a,
                organization_a_other,
            }
            assert set((await session.execute(select(Workspace.id))).scalars().all()) == {
                workspace_a,
                workspace_a_other,
            }
            assert (await session.execute(select(Project.id))).scalars().all() == [project_a]
            await session.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": organization_b})
            await session.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})

            session.add(
                Organization(
                    id=uuid.uuid4(), tenant_id=tenant_b, name="Cross tenant", slug=f"cross-{uuid.uuid4()}"
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

            session.add(
                Workspace(
                    id=uuid.uuid4(), organization_id=organization_b,
                    name="Cross tenant", slug=f"cross-{uuid.uuid4()}",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

            for workspace_id in (workspace_b, workspace_a_other):
                session.add(
                    Project(
                        id=uuid.uuid4(), organization_id=organization_a, workspace_id=workspace_id,
                        tenant_id=tenant_a, name="Mismatched workspace", slug=f"mismatch-{uuid.uuid4()}",
                    )
                )
                with pytest.raises(DBAPIError):
                    await session.flush()
                await session.rollback()

        tenant_context.set(tenant_b, "rls-organization-b")
        async with sessions() as session:
            assert (await session.execute(select(Organization.id))).scalars().all() == [organization_b]
            assert (await session.execute(select(Workspace.id))).scalars().all() == [workspace_b]
            assert (await session.execute(select(Project.id))).scalars().all() == [project_b]
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, project_id, workspace_id, organization_id in (
                (tenant_a, project_a, workspace_a, organization_a),
                (tenant_b, project_b, workspace_b, organization_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
                await connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
                await connection.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id})
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_a)},
            )
            await connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_a_other})
            await connection.execute(
                text("DELETE FROM organizations WHERE id = :id"), {"id": organization_a_other}
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_organization_membership_rls_preserves_first_membership_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.multi_tenant import auth as tenant_auth

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b = uuid.uuid4(), uuid.uuid4()
    membership_a, membership_b = uuid.uuid4(), uuid.uuid4()
    user_id = uuid.uuid4()
    monkeypatch.setattr(tenant_auth, "async_session_factory", sessions)

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'organization_memberships'"
                    )
                )
            ).all()
            assert {row[0] for row in protected} == {"organization_memberships"}
            assert all(row[1] and row[2] for row in protected)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Membership RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-membership-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-membership-{tenant_b}"},
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO users (id, email, password_hash, display_name) "
                    "VALUES (:id, :email, 'test-hash', 'Membership RLS')"
                ),
                {"id": user_id, "email": f"membership-{user_id}@example.com"},
            )
            await _seed_organizations(
                connection,
                [
                    (organization_a, tenant_a, f"rls-membership-org-{organization_a}", "Membership RLS"),
                    (organization_b, tenant_b, f"rls-membership-org-{organization_b}", "Membership RLS"),
                ],
            )
            for tenant_id, membership_id, organization_id, role in (
                (tenant_a, membership_a, organization_a, "owner"),
                (tenant_b, membership_b, organization_b, "admin"),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO organization_memberships (id, user_id, organization_id, role) "
                        "VALUES (:id, :user_id, :organization_id, :role)"
                    ),
                    {
                        "id": membership_id,
                        "user_id": user_id,
                        "organization_id": organization_id,
                        "role": role,
                    },
                )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(OrganizationMembership.id))).scalars().all() == []
            session.add(
                OrganizationMembership(
                    id=uuid.uuid4(), user_id=user_id, organization_id=organization_a, role="owner"
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.set(tenant_a, "rls-membership-a")
        async with sessions() as session:
            assert (await session.execute(select(OrganizationMembership.id))).scalars().all() == [membership_a]
            assert (
                await session.execute(
                    update(OrganizationMembership)
                    .where(OrganizationMembership.id == membership_b)
                    .values(role="owner")
                )
            ).rowcount == 0
            assert (
                await session.execute(
                    text("DELETE FROM organization_memberships WHERE id = :id"), {"id": membership_b}
                )
            ).rowcount == 0
            session.add(
                OrganizationMembership(
                    id=uuid.uuid4(), user_id=user_id, organization_id=organization_b, role="owner"
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.clear()
        membership = await tenant_auth._first_membership(user_id)
        assert membership in (
            (tenant_a, f"rls-membership-org-{organization_a}", "owner"),
            (tenant_b, f"rls-membership-org-{organization_b}", "admin"),
        )

        tenant_context.set(tenant_b, "rls-membership-b")
        async with sessions() as session:
            assert (await session.execute(select(OrganizationMembership.id))).scalars().all() == [membership_b]
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id, membership_id, organization_id in (
                (tenant_a, membership_a, organization_a),
                (tenant_b, membership_b, organization_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("DELETE FROM organization_memberships WHERE id = :id"), {"id": membership_id}
                )
                await connection.execute(
                    text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id}
                )
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
            )
        await engine.dispose()


@pytest.mark.asyncio
async def test_api_key_rls_preserves_unscoped_key_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.api.v1 import admin
    from solvyn.multi_tenant import auth as tenant_auth

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    api_key_a, api_key_b = uuid.uuid4(), uuid.uuid4()
    created_tenant_id: uuid.UUID | None = None
    secret_a, secret_b = f"rls-api-key-{tenant_a}", f"rls-api-key-{tenant_b}"
    monkeypatch.setattr(tenant_auth, "async_session_factory", sessions)

    try:
        async with engine.connect() as connection:
            protected = (
                await connection.execute(
                    text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'api_keys'")
                )
            ).one()
            assert protected == (True, True)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'API Key RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-api-key-tenant-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-api-key-tenant-{tenant_b}"},
                ],
            )
            for tenant_id, api_key_id, secret in (
                (tenant_a, api_key_a, secret_a),
                (tenant_b, api_key_b, secret_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO api_keys (id, tenant_id, name, key_hash, key_prefix) "
                        "VALUES (:id, :tenant_id, 'RLS test key', :key_hash, :key_prefix)"
                    ),
                    {
                        "id": api_key_id,
                        "tenant_id": tenant_id,
                        "key_hash": hashlib.sha256(secret.encode()).hexdigest(),
                        "key_prefix": secret[:12],
                    },
                )

        tenant_context.clear()
        async with sessions() as session:
            assert (await session.execute(select(ApiKey.id))).scalars().all() == []
            session.add(
                ApiKey(
                    id=uuid.uuid4(),
                    tenant_id=tenant_a,
                    name="Unscoped insert",
                    key_hash=hashlib.sha256(b"unscoped").hexdigest(),
                    key_prefix="unscoped",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.set(tenant_a, "rls-api-key-a")
        async with sessions() as session:
            assert (await session.execute(select(ApiKey.id))).scalars().all() == [api_key_a]
            assert (
                await session.execute(
                    update(ApiKey).where(ApiKey.id == api_key_b).values(name="cross-tenant update")
                )
            ).rowcount == 0
            assert (
                await session.execute(text("DELETE FROM api_keys WHERE id = :id"), {"id": api_key_b})
            ).rowcount == 0
            session.add(
                ApiKey(
                    id=uuid.uuid4(),
                    tenant_id=tenant_b,
                    name="Cross tenant insert",
                    key_hash=hashlib.sha256(b"cross-tenant").hexdigest(),
                    key_prefix="cross-tenant",
                )
            )
            with pytest.raises(DBAPIError):
                await session.flush()
            await session.rollback()

        tenant_context.clear()
        authenticated_tenant = await tenant_auth._validate_tenant_api_key(secret_b)
        assert authenticated_tenant is not None
        assert authenticated_tenant.id == tenant_b

        tenant_context.set(tenant_b, "rls-api-key-b")
        async with sessions() as session:
            assert (await session.execute(select(ApiKey.id))).scalars().all() == [api_key_b]

        tenant_context.set(tenant_a, "rls-api-key-admin", TenantRole.ADMIN)
        async with sessions() as session:
            tenant_list = await admin.list_tenants(session)
            tenant_items = {uuid.UUID(item["id"]): item for item in tenant_list["tenants"]}
            assert tenant_items[tenant_a]["api_keys_count"] == 1
            assert tenant_items[tenant_b]["api_keys_count"] == 1

            created_tenant = await admin.create_tenant(
                admin.TenantCreate(name="Admin-created", slug=f"rls-admin-created-{uuid.uuid4().hex}"),
                session,
            )
            created_tenant_id = uuid.UUID(created_tenant["id"])
            created_key = await admin.create_api_key(
                tenant_b,
                admin.ApiKeyCreate(name="Cross-tenant admin key"),
                session,
            )
            assert uuid.UUID(created_key["tenant_id"]) == tenant_b
            listed_keys = await admin.list_api_keys(tenant_b, session)
            assert listed_keys["count"] == 2
            revoked = await admin.revoke_api_key(uuid.UUID(created_key["id"]), session)
            assert revoked["revoked"] is True
            assert uuid.UUID(revoked["tenant_id"]) == tenant_b

            tenant_list = await admin.list_tenants(session)
            tenant_items = {uuid.UUID(item["id"]): item for item in tenant_list["tenants"]}
            assert tenant_items[tenant_a]["api_keys_count"] == 1
            assert tenant_items[tenant_b]["api_keys_count"] == 1
            assert tenant_items[created_tenant_id]["api_keys_count"] == 1
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            for tenant_id in (tenant_a, tenant_b, created_tenant_id):
                if tenant_id is None:
                    continue
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM api_keys WHERE tenant_id = :id"), {"id": tenant_id})
            await connection.execute(
                text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b}
            )
            if created_tenant_id is not None:
                await connection.execute(
                    text("DELETE FROM tenants WHERE id = :id"), {"id": created_tenant_id}
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_event_history_is_atomic_immutable_and_tree_scoped() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.api.v1.verification import _activity_task_tree_ids

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    root_id, child_id, foreign_task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    actor_id = uuid.uuid4()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                protected = (
                    await connection.execute(
                        text(
                            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname = 'task_events'"
                        )
                    )
                ).one()
                assert protected == (True, True)
                await connection.execute(
                    text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Task Events', :slug, '')"),
                    [
                        {"id": tenant_a, "slug": f"rls-task-events-{tenant_a}"},
                        {"id": tenant_b, "slug": f"rls-task-events-{tenant_b}"},
                    ],
                )
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_a)},
                )
                await connection.execute(
                    text("SELECT set_config('app.task_event_source', 'user_request', true)")
                )
                await connection.execute(
                    text("SELECT set_config('app.task_event_actor_user_id', :actor_id, true)"),
                    {"actor_id": str(actor_id)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO tasks (id, tenant_id, title, status, execution_id) "
                        "VALUES (:id, :tenant_id, 'History root', 'pending', NULL)"
                    ),
                    {"id": root_id, "tenant_id": tenant_a},
                )
                await connection.execute(
                    text(
                        "INSERT INTO tasks (id, tenant_id, title, status, parent_task_id, execution_id) "
                        "VALUES (:id, :tenant_id, 'History child', 'pending', :parent_id, NULL)"
                    ),
                    {"id": child_id, "tenant_id": tenant_a, "parent_id": root_id},
                )
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_b)},
                )
                await connection.execute(
                    text(
                        "INSERT INTO tasks (id, tenant_id, title, status, execution_id) "
                        "VALUES (:id, :tenant_id, 'Foreign history', 'pending', NULL)"
                    ),
                    {"id": foreign_task_id, "tenant_id": tenant_b},
                )
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_a)},
                )
                await connection.execute(
                    text("UPDATE tasks SET status = 'running' WHERE id = :id"), {"id": root_id}
                )
                await connection.execute(
                    text("UPDATE tasks SET status = 'running' WHERE id = :id"), {"id": root_id}
                )
                await connection.execute(
                    text("UPDATE tasks SET status = 'completed' WHERE id = :id"), {"id": child_id}
                )

                events = (
                    await connection.execute(
                        text(
                            "SELECT task_id, event_type, previous_status, status, source, actor_user_id "
                            "FROM task_events WHERE tenant_id = :tenant_id ORDER BY occurred_at, id"
                        ),
                        {"tenant_id": tenant_a},
                    )
                ).all()
                assert [(row.event_type, row.previous_status, row.status) for row in events] == [
                    ("created", None, "pending"),
                    ("created", None, "pending"),
                    ("status_changed", "pending", "running"),
                    ("status_changed", "pending", "completed"),
                ]
                assert {row.task_id for row in events} == {root_id, child_id}
                assert all(row.source == "user_request" and row.actor_user_id == actor_id for row in events)

                tenant_context.set(tenant_a, "rls-task-events-a", user_id=actor_id)
                async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                    root = Task(id=root_id, tenant_id=tenant_a, execution_id=None, title="History root")
                    tree_ids, truncated = await _activity_task_tree_ids(session, root)
                    assert set(tree_ids) == {root_id, child_id}
                    assert truncated is False

                savepoint = await connection.begin_nested()
                await connection.execute(
                    text("UPDATE tasks SET status = 'failed' WHERE id = :id"), {"id": root_id}
                )
                assert (
                    await connection.execute(
                        text("SELECT count(*) FROM task_events WHERE tenant_id = :tenant_id"),
                        {"tenant_id": tenant_a},
                    )
                ).scalar_one() == 5
                await savepoint.rollback()
                assert (
                    await connection.execute(
                        text("SELECT count(*) FROM task_events WHERE tenant_id = :tenant_id"),
                        {"tenant_id": tenant_a},
                    )
                ).scalar_one() == 4

                assert (
                    await connection.execute(
                        text("SELECT count(*) FROM task_events WHERE tenant_id = :tenant_id"),
                        {"tenant_id": tenant_b},
                    )
                ).scalar_one() == 0
                assert (
                    await connection.execute(
                        text("UPDATE task_events SET status = 'forged' WHERE task_id = :task_id"),
                        {"task_id": foreign_task_id},
                    )
                ).rowcount == 0
                assert (
                    await connection.execute(
                        text("DELETE FROM task_events WHERE task_id = :task_id"),
                        {"task_id": foreign_task_id},
                    )
                ).rowcount == 0

                event_task_id = events[0].task_id
                immutable_savepoint = await connection.begin_nested()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text("UPDATE task_events SET status = 'forged' WHERE task_id = :task_id"),
                        {"task_id": event_task_id},
                    )
                await immutable_savepoint.rollback()
                immutable_savepoint = await connection.begin_nested()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text("DELETE FROM task_events WHERE task_id = :task_id"), {"task_id": event_task_id}
                    )
                await immutable_savepoint.rollback()

                direct_insert_savepoint = await connection.begin_nested()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "INSERT INTO task_events (tenant_id, task_id, event_type, status, source) "
                            "VALUES (:tenant_id, :task_id, 'forged', 'completed', 'forged_source')"
                        ),
                        {"tenant_id": tenant_a, "task_id": root_id},
                    )
                await direct_insert_savepoint.rollback()

                await connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
                tenant_context.clear()
                assert (
                    await connection.execute(text("SELECT count(*) FROM task_events"))
                ).scalar_one() == 0
                denied_savepoint = await connection.begin_nested()
                with pytest.raises(DBAPIError):
                    await connection.execute(
                        text(
                            "INSERT INTO task_events (tenant_id, task_id, event_type, status, source) "
                            "VALUES (:tenant_id, :task_id, 'forged', 'completed', 'test')"
                        ),
                        {"tenant_id": tenant_a, "task_id": root_id},
                    )
                await denied_savepoint.rollback()
            finally:
                tenant_context.clear()
                if transaction.is_active:
                    await transaction.rollback()
        await engine.dispose()
    except Exception:
        await engine.dispose()
        raise


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


@pytest.mark.asyncio
async def test_legacy_table_rls_is_tenant_scoped_and_checks_parent_links() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    tenant_a, tenant_b, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b = uuid.uuid4(), uuid.uuid4()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    agent_a, agent_b = uuid.uuid4(), uuid.uuid4()
    artifact_a, artifact_b = uuid.uuid4(), uuid.uuid4()
    message_a, message_b = uuid.uuid4(), uuid.uuid4()
    membership_a, membership_b = uuid.uuid4(), uuid.uuid4()

    async def set_tenant(connection: AsyncConnection, tenant_id: uuid.UUID) -> None:
        await connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
            {"tenant_id": str(tenant_id)},
        )

    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "INSERT INTO tenants (id, name, slug, api_key_hash) "
                        "VALUES (:id, :name, :slug, '')"
                    ),
                    [
                        {"id": tenant_a, "name": "Legacy A", "slug": f"rls-legacy-{tenant_a}"},
                        {"id": tenant_b, "name": "Legacy B", "slug": f"rls-legacy-{tenant_b}"},
                    ],
                )
                await connection.execute(
                    text(
                        "INSERT INTO users (id, email, password_hash) "
                        "VALUES (:id, :email, 'test-hash')"
                    ),
                    {"id": user_id, "email": f"{user_id}@example.test"},
                )
                for tenant_id, session_id, organization_id, project_id, suffix in (
                    (tenant_a, session_a, organization_a, project_a, "a"),
                    (tenant_b, session_b, organization_b, project_b, "b"),
                ):
                    await set_tenant(connection, tenant_id)
                    await connection.execute(
                        text("INSERT INTO sessions (id, tenant_id, title) VALUES (:id, :tenant_id, :title)"),
                        {"id": session_id, "tenant_id": tenant_id, "title": f"Session {suffix}"},
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO agent_instances (id, tenant_id, agent_type, name) "
                            "VALUES (:id, :tenant_id, 'engineer', :name)"
                        ),
                        {
                            "id": agent_a if suffix == "a" else agent_b,
                            "tenant_id": tenant_id,
                            "name": f"Agent {suffix}",
                        },
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO artifacts (id, tenant_id, session_id, agent_type, artifact_type, name, content) "
                            "VALUES (:id, :tenant_id, :session_id, 'engineer', 'note', :name, 'body')"
                        ),
                        {
                            "id": artifact_a if suffix == "a" else artifact_b,
                            "tenant_id": tenant_id,
                            "session_id": session_id,
                            "name": f"Artifact {suffix}",
                        },
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO messages (id, session_id, role, content) "
                            "VALUES (:id, :session_id, 'user', :content)"
                        ),
                        {
                            "id": message_a if suffix == "a" else message_b,
                            "session_id": session_id,
                            "content": f"Message {suffix}",
                        },
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO organizations (id, name, slug, tenant_id) "
                            "VALUES (:id, :name, :slug, :tenant_id)"
                        ),
                        {
                            "id": organization_id,
                            "name": f"Organization {suffix}",
                            "slug": f"rls-legacy-org-{suffix}-{organization_id}",
                            "tenant_id": tenant_id,
                        },
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO projects (id, organization_id, tenant_id, name, slug) "
                            "VALUES (:id, :organization_id, :tenant_id, :name, :slug)"
                        ),
                        {
                            "id": project_id,
                            "organization_id": organization_id,
                            "tenant_id": tenant_id,
                            "name": f"Project {suffix}",
                            "slug": f"project-{suffix}",
                        },
                    )
                    await connection.execute(
                        text(
                            "INSERT INTO project_memberships (id, user_id, project_id, role) "
                            "VALUES (:id, :user_id, :project_id, 'member')"
                        ),
                        {
                            "id": membership_a if suffix == "a" else membership_b,
                            "user_id": user_id,
                            "project_id": project_id,
                        },
                    )

                await set_tenant(connection, tenant_a)
                protected = (
                    await connection.execute(
                        text(
                            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
                            "WHERE relname = ANY(:tables) ORDER BY relname"
                        ),
                        {"tables": ["agent_instances", "artifacts", "messages", "project_memberships"]},
                    )
                ).all()
                assert protected == [
                    ("agent_instances", True, True),
                    ("artifacts", True, True),
                    ("messages", True, True),
                    ("project_memberships", True, True),
                ]
                for table, row_id in (
                    ("agent_instances", agent_a),
                    ("artifacts", artifact_a),
                    ("messages", message_a),
                    ("project_memberships", membership_a),
                ):
                    assert (
                        await connection.execute(
                            text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": row_id}
                        )
                    ).scalar_one() == 1
                for table, row_id in (
                    ("agent_instances", agent_b),
                    ("artifacts", artifact_b),
                    ("messages", message_b),
                    ("project_memberships", membership_b),
                ):
                    assert (
                        await connection.execute(
                            text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": row_id}
                        )
                    ).scalar_one() == 0

                await connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
                for table in ("agent_instances", "artifacts", "messages", "project_memberships"):
                    assert (await connection.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0

                await set_tenant(connection, tenant_a)
                hidden_mutations = (
                    ("UPDATE agent_instances SET name = 'hidden' WHERE id = :id", agent_b),
                    ("DELETE FROM agent_instances WHERE id = :id", agent_b),
                    ("UPDATE artifacts SET content = 'hidden' WHERE id = :id", artifact_b),
                    ("DELETE FROM artifacts WHERE id = :id", artifact_b),
                    ("UPDATE messages SET content = 'hidden' WHERE id = :id", message_b),
                    ("DELETE FROM messages WHERE id = :id", message_b),
                    ("UPDATE project_memberships SET role = 'admin' WHERE id = :id", membership_b),
                    ("DELETE FROM project_memberships WHERE id = :id", membership_b),
                )
                for statement, row_id in hidden_mutations:
                    result = await connection.execute(text(statement), {"id": row_id})
                    assert result.rowcount == 0

                invalid_inserts = (
                    (
                        "INSERT INTO agent_instances (id, tenant_id, agent_type, name) "
                        "VALUES (:id, :tenant_id, 'engineer', 'forged')",
                        {"id": uuid.uuid4(), "tenant_id": tenant_b},
                    ),
                    (
                        "INSERT INTO artifacts (id, tenant_id, session_id, agent_type, artifact_type, name, content) "
                        "VALUES (:id, :tenant_id, :session_id, 'engineer', 'note', 'forged', 'body')",
                        {"id": uuid.uuid4(), "tenant_id": tenant_a, "session_id": session_b},
                    ),
                    (
                        "INSERT INTO messages (id, session_id, role, content) "
                        "VALUES (:id, :session_id, 'user', 'forged')",
                        {"id": uuid.uuid4(), "session_id": session_b},
                    ),
                    (
                        "INSERT INTO project_memberships (id, user_id, project_id, role) "
                        "VALUES (:id, :user_id, :project_id, 'member')",
                        {"id": uuid.uuid4(), "user_id": user_id, "project_id": project_b},
                    ),
                )
                for statement, params in invalid_inserts:
                    savepoint = await connection.begin_nested()
                    with pytest.raises(DBAPIError):
                        await connection.execute(text(statement), params)
                    await savepoint.rollback()

                await set_tenant(connection, tenant_b)
                assert (
                    await connection.execute(
                        text("SELECT count(*) FROM messages WHERE session_id = :session_id"),
                        {"session_id": session_b},
                    )
                ).scalar_one() == 1
            finally:
                if transaction.is_active:
                    await transaction.rollback()
        await engine.dispose()
    except Exception:
        await engine.dispose()
        raise
