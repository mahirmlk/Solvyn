from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_stale_cancellation_discovery_is_tenant_scoped_and_ignores_active_leases() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.workflows.scheduler import _fetch_due_cancellation_workflows

    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
    session_a, session_b = uuid.uuid4(), uuid.uuid4()
    stale_cancel_a, active_cancel_b = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            assert role == (False, False)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Cancellation RLS', :slug, '')"),
                [
                    {"id": tenant_a, "slug": f"rls-cancel-{tenant_a}"},
                    {"id": tenant_b, "slug": f"rls-cancel-{tenant_b}"},
                ],
            )
            for tenant_id, session_id, execution_id, lease_offset_seconds in (
                (tenant_a, session_a, stale_cancel_a, -60),
                (tenant_b, session_b, active_cancel_b, 60),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(
                    text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Cancel RLS', 'active')"),
                    {"id": session_id, "tenant_id": tenant_id},
                )
                await connection.execute(
                    text(
                        "INSERT INTO execution_graphs (id, session_id, graph_data, status, lease_owner, lease_expires_at) "
                        "VALUES (:id, :session_id, '{}', 'cancelling', :lease_owner, "
                        "now() + (:lease_offset_seconds * interval '1 second'))"
                    ),
                    {
                        "id": execution_id,
                        "session_id": session_id,
                        "lease_owner": uuid.uuid4(),
                        "lease_offset_seconds": lease_offset_seconds,
                    },
                )

        async with sessions() as session:
            due = await _fetch_due_cancellation_workflows(session)
            assert (stale_cancel_a, tenant_a) in due
            assert (active_cancel_b, tenant_b) not in due
    finally:
        async with engine.begin() as connection:
            for tenant_id, session_id, execution_id in (
                (tenant_a, session_a, stale_cancel_a),
                (tenant_b, session_b, active_cancel_b),
            ):
                await connection.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await connection.execute(text("DELETE FROM execution_graphs WHERE id = :id"), {"id": execution_id})
                await connection.execute(text("DELETE FROM sessions WHERE id = :id"), {"id": session_id})
            await connection.execute(text("DELETE FROM tenants WHERE id IN (:a, :b)"), {"a": tenant_a, "b": tenant_b})
        await engine.dispose()
