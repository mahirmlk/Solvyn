"""PostgreSQL-only checks for the initial tenant RLS boundary."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from solvyn.db.models import (
    TaskAcceptanceCriterion,
    TaskDependency,
    TaskHandoff,
    TaskWorkspace,
    TeammateMemory,
    TeammateToolInvocation,
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
                        "('task_acceptance_criteria', 'task_deliveries', 'task_workspaces', "
                        "'teammate_tool_invocations', 'task_dependencies', 'verification_runs', 'task_handoffs', 'teammate_memory')"
                    )
                )
            ).all()
            assert {row[0] for row in protected_tables} == {
                "task_acceptance_criteria",
                "task_deliveries",
                "task_workspaces",
                "teammate_tool_invocations",
                "task_dependencies",
                "verification_runs",
                "task_handoffs",
                "teammate_memory",
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
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'Memory RLS', :slug)"),
                [
                    {"id": project_a, "organization_id": org_a, "tenant_id": tenant_a, "slug": f"rls-memory-project-{project_a}"},
                    {"id": project_b, "organization_id": org_b, "tenant_id": tenant_b, "slug": f"rls-memory-project-{project_b}"},
                    {"id": project_unassigned, "organization_id": org_a, "tenant_id": tenant_a, "slug": f"rls-memory-project-{project_unassigned}"},
                ],
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
            await connection.execute(text("DELETE FROM projects WHERE id IN (:a, :b, :unassigned)"), {"a": project_a, "b": project_b, "unassigned": project_unassigned})
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
            await connection.execute(
                text("INSERT INTO projects (id, organization_id, tenant_id, name, slug) VALUES (:id, :organization_id, :tenant_id, 'Handoff RLS', :slug)"),
                [
                    {"id": project_a, "organization_id": org_a, "tenant_id": tenant_a, "slug": f"rls-handoff-project-{project_a}"},
                    {"id": project_b, "organization_id": org_b, "tenant_id": tenant_b, "slug": f"rls-handoff-project-{project_b}"},
                ],
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
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title, parent_task_id) VALUES (:id, :tenant_id, 'Handoff task', :parent_task_id)"),
                [
                    {"id": parent_a, "tenant_id": tenant_a, "parent_task_id": None},
                    {"id": child_a, "tenant_id": tenant_a, "parent_task_id": parent_a},
                    {"id": parent_b, "tenant_id": tenant_b, "parent_task_id": None},
                    {"id": child_b, "tenant_id": tenant_b, "parent_task_id": parent_b},
                ],
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
            await connection.execute(text("DELETE FROM tasks WHERE id IN (:pa, :ca, :pb, :cb)"), {"pa": parent_a, "ca": child_a, "pb": parent_b, "cb": child_b})
            await connection.execute(text("DELETE FROM teammate_profiles WHERE id IN (:a, :b)"), {"a": teammate_a, "b": teammate_b})
            await connection.execute(text("DELETE FROM projects WHERE id IN (:a, :b)"), {"a": project_a, "b": project_b})
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
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS dependency task')"),
                [{"id": task_a, "tenant_id": tenant_a}, {"id": task_b, "tenant_id": tenant_b}],
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
            await connection.execute(text("DELETE FROM tasks WHERE id IN (:a, :b)"), {"a": task_a, "b": task_b})
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
            await connection.execute(
                text("INSERT INTO tasks (id, tenant_id, title) VALUES (:id, :tenant_id, 'RLS verification task')"),
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
            await connection.execute(text("DELETE FROM tasks WHERE id IN (:a, :b)"), {"a": task_a, "b": task_b})
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
