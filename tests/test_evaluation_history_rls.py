"""PostgreSQL checks for append-only, tenant-scoped evaluation history."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import delete, insert, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from solvyn.db.models import AgentEvaluationRun


@pytest.mark.asyncio
async def test_agent_evaluation_history_is_tenant_scoped_and_append_only() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("Evaluation history RLS behavior requires PostgreSQL and migrations at head")

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0, connect_args={"timeout": 5})
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    run_a, run_b, forged_run = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                role = (await connection.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
                assert role == (False, False)
                for tenant_id in (tenant_a, tenant_b):
                    await connection.execute(
                        text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Eval RLS', :slug, '')"),
                        {"id": tenant_id, "slug": f"eval-rls-{tenant_id}"},
                    )

                def evaluation(run_id: uuid.UUID, tenant_id: uuid.UUID) -> dict[str, object]:
                    return {
                        "id": run_id,
                        "tenant_id": tenant_id,
                        "agent_type": "qa-engineer",
                        "dataset_id": "engineering-core",
                        "dataset_version": "1.0.0",
                        "provider": "test-provider",
                        "model": "test-model",
                        "summary": {"total_cases": 2, "passed_cases": 1, "pass_rate": 0.5},
                    }

                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_a)})
                await connection.execute(insert(AgentEvaluationRun).values(**evaluation(run_a, tenant_a)))
                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_b)})
                await connection.execute(insert(AgentEvaluationRun).values(**evaluation(run_b, tenant_b)))

                await connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": str(tenant_a)})
                visible_ids = (
                    (
                        await connection.execute(
                            text("SELECT id FROM agent_evaluation_runs WHERE id IN (:run_a, :run_b)"),
                            {"run_a": run_a, "run_b": run_b},
                        )
                    )
                    .scalars()
                    .all()
                )
                assert visible_ids == [run_a]

                for statement in (
                    insert(AgentEvaluationRun).values(**evaluation(forged_run, tenant_b)),
                    update(AgentEvaluationRun).where(AgentEvaluationRun.id == run_a).values(summary={"tampered": True}),
                    delete(AgentEvaluationRun).where(AgentEvaluationRun.id == run_a),
                ):
                    savepoint = await connection.begin_nested()
                    with pytest.raises(DBAPIError):
                        await connection.execute(statement)
                    await savepoint.rollback()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
