from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def _snapshot(status: str) -> str:
    return json.dumps(
        {
            "status": status,
            "template": "feature",
            "inputs": {"requirement": "fixture"},
            "nodes": [
                {
                    "id": "done",
                    "agent_type": "planner",
                    "status": "completed",
                    "input": {},
                    "output": {"result": "saved"},
                },
                {"id": "unfinished", "agent_type": "builder", "status": "running", "input": {}},
            ],
            "edges": [{"source": "done", "target": "unfinished"}],
        }
    )


@pytest.mark.asyncio
async def test_workflow_reclaim_cap_retry_fencing_and_cancel_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith("postgresql+"):
        pytest.skip("Workflow recovery RLS behavior requires PostgreSQL and migrations at head")

    from solvyn.agents.schema import ExecutionGraph
    from solvyn.api.v1 import workflows as workflow_api
    from solvyn.db.models import ExecutionGraph as ExecutionGraphModel
    from solvyn.db.models import OrchestrationEvent
    from solvyn.db.models import Task as TaskModel
    from solvyn.db.session import bind_tenant_scope
    from solvyn.multi_tenant.middleware import tenant_context
    from solvyn.workflows import executor as executor_module
    from solvyn.workflows import scheduler

    engine = create_async_engine(database_url, pool_size=3, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(executor_module, "async_session_factory", sessions)
    monkeypatch.setattr(scheduler, "async_session_factory", sessions)
    tenant_id, other_tenant_id, user_id, organization_id = (uuid.uuid4() for _ in range(4))
    session_id = uuid.uuid4()
    exhausted_id, race_id, cancelled_id, stale_id = (uuid.uuid4() for _ in range(4))
    parent_task_id, unfinished_task_id = uuid.uuid4(), uuid.uuid4()
    project_id, teammate_id, assignment_id, handoff_id, approval_id = (uuid.uuid4() for _ in range(5))
    old_owner = uuid.uuid4()
    email = f"workflow-recovery-{tenant_id}@example.invalid"
    tenant_context.clear()

    try:
        async with engine.connect() as connection:
            role = (
                await connection.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))
            ).one()
            assert role == (False, False)

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO tenants (id, name, slug, api_key_hash) VALUES (:id, 'Recovery', :slug, '')"),
                [
                    {"id": tenant_id, "slug": f"wf-recovery-{tenant_id}"},
                    {"id": other_tenant_id, "slug": f"wf-recovery-other-{other_tenant_id}"},
                ],
            )
            await connection.execute(
                text("INSERT INTO users (id, email, password_hash, display_name) VALUES (:id, :email, 'hash', 'Operator')"),
                {"id": user_id, "email": email},
            )
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
            await connection.execute(
                text("INSERT INTO organizations (id, name, slug, tenant_id, is_active) VALUES (:id, 'Recovery', :slug, :tenant_id, true)"),
                {"id": organization_id, "slug": f"org-wf-recovery-{tenant_id}", "tenant_id": tenant_id},
            )
            await connection.execute(
                text("INSERT INTO organization_memberships (id, user_id, organization_id, role) VALUES (:id, :user_id, :org_id, 'member')"),
                {"id": uuid.uuid4(), "user_id": user_id, "org_id": organization_id},
            )
            await connection.execute(
                text("INSERT INTO sessions (id, tenant_id, title, status) VALUES (:id, :tenant_id, 'Recovery', 'active')"),
                {"id": session_id, "tenant_id": tenant_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO execution_graphs "
                    "(id, session_id, graph_data, status, lease_owner, lease_expires_at, lease_reclaim_count) "
                    "VALUES (:id, :session_id, :graph_data, :status, :owner, now() - interval '1 minute', :count)"
                ),
                [
                    {
                        "id": exhausted_id,
                        "session_id": session_id,
                        "graph_data": _snapshot("resuming"),
                        "status": "resuming",
                        "owner": old_owner,
                        "count": 3,
                    },
                    {
                        "id": race_id,
                        "session_id": session_id,
                        "graph_data": _snapshot("resuming"),
                        "status": "resuming",
                        "owner": uuid.uuid4(),
                        "count": 0,
                    },
                    {
                        "id": cancelled_id,
                        "session_id": session_id,
                        "graph_data": _snapshot("cancelling"),
                        "status": "cancelling",
                        "owner": uuid.uuid4(),
                        "count": 0,
                    },
                    {
                        "id": stale_id,
                        "session_id": session_id,
                        "graph_data": _snapshot("running"),
                        "status": "running",
                        "owner": old_owner,
                        "count": 0,
                    },
                ],
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(id, tenant_id, session_id, execution_id, node_id, parent_task_id, title, description, status, priority) "
                    "VALUES (:id, :tenant_id, :session_id, :execution_id, 'unfinished', :parent_id, 'Unfinished workflow step', '', 'running', 0), "
                    "(:parent_id, :tenant_id, :session_id, :execution_id, 'done', NULL, 'Workflow parent step', '', 'completed', 0)"
                ),
                {
                    "id": unfinished_task_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "execution_id": exhausted_id,
                    "parent_id": parent_task_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO projects (id, organization_id, tenant_id, name, slug) "
                    "VALUES (:id, :organization_id, :tenant_id, 'Recovery project', :slug)"
                ),
                {
                    "id": project_id,
                    "organization_id": organization_id,
                    "tenant_id": tenant_id,
                    "slug": f"workflow-recovery-{tenant_id}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO teammate_profiles (id, organization_id, tenant_id, name, role, agent_type) "
                    "VALUES (:id, :organization_id, :tenant_id, 'Recovery teammate', 'backend_engineer', 'backend-engineer')"
                ),
                {"id": teammate_id, "organization_id": organization_id, "tenant_id": tenant_id},
            )
            await connection.execute(
                text("INSERT INTO teammate_project_assignments (id, teammate_id, project_id) VALUES (:id, :teammate_id, :project_id)"),
                {"id": assignment_id, "teammate_id": teammate_id, "project_id": project_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO task_handoffs "
                    "(id, tenant_id, parent_task_id, child_task_id, idempotency_key, teammate_id, project_id, "
                    "delegated_from, delegated_to, reason, input_data, status, requires_approval) "
                    "VALUES (:id, :tenant_id, :parent_id, :child_id, :idempotency_key, :teammate_id, :project_id, "
                    "'planner', 'backend-engineer', 'workflow recovery fixture', '{}', 'waiting_approval', true)"
                ),
                {
                    "id": handoff_id,
                    "tenant_id": tenant_id,
                    "parent_id": parent_task_id,
                    "child_id": unfinished_task_id,
                    "idempotency_key": uuid.uuid4(),
                    "teammate_id": teammate_id,
                    "project_id": project_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO approvals "
                    "(id, organization_id, project_id, teammate_id, task_handoff_id, kind, summary, status, requested_by) "
                    "VALUES (:id, :organization_id, :project_id, :teammate_id, :handoff_id, 'handoff', 'Recovery fixture', 'pending', 'planner')"
                ),
                {
                    "id": approval_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "teammate_id": teammate_id,
                    "handoff_id": handoff_id,
                },
            )

        tenant_context.authenticate(tenant_id, f"wf-recovery-{tenant_id}", "user", user_id=user_id)
        async with sessions() as db:
            with pytest.raises(HTTPException) as member_result:
                await workflow_api._require_workflow_operator(db, exhausted_id, tenant_id)
            assert member_result.value.status_code == 403
            await db.execute(
                text("UPDATE organization_memberships SET role = 'owner' WHERE user_id = :user_id AND organization_id = :org_id"),
                {"user_id": user_id, "org_id": organization_id},
            )
            await db.commit()
            await workflow_api._require_workflow_operator(db, exhausted_id, tenant_id)
            with pytest.raises(HTTPException) as cross_tenant:
                await workflow_api._require_workflow_operator(db, exhausted_id, other_tenant_id)
            assert cross_tenant.value.status_code == 404

        async with sessions() as db:
            await bind_tenant_scope(db, other_tenant_id)
            hidden = await db.execute(select(ExecutionGraphModel.id).where(ExecutionGraphModel.id == exhausted_id))
            assert hidden.scalar_one_or_none() is None
            hidden_update = await db.execute(
                text("UPDATE execution_graphs SET status = 'cancelled' WHERE id = :id RETURNING id"),
                {"id": exhausted_id},
            )
            assert hidden_update.scalar_one_or_none() is None
            await db.rollback()

        executor = executor_module.WorkflowExecutor()
        with pytest.raises(RuntimeError, match="exhausted 3 automatic lease reclaims"):
            await executor.resume(exhausted_id, str(tenant_id))
        async with sessions() as db:
            await bind_tenant_scope(db, tenant_id)
            dead_letter = await db.get(ExecutionGraphModel, exhausted_id)
            assert dead_letter is not None
            assert dead_letter.status == "dead_letter"
            assert dead_letter.lease_reclaim_count == 3
            assert dead_letter.dead_lettered_at is not None
            assert dead_letter.dead_letter_reason == "Workflow execution exhausted 3 automatic lease reclaims"
            dead_letter_graph = json.loads(dead_letter.graph_data)
            assert dead_letter_graph["nodes"][0]["status"] == "completed"
            assert dead_letter_graph["nodes"][1]["status"] == "failed"
            task = await db.get(TaskModel, unfinished_task_id)
            assert task is not None
            assert task.status == "failed"
            assert task.error == "Workflow execution exhausted 3 automatic lease reclaims"
            handoff = await db.scalar(select(workflow_api.TaskHandoff).where(workflow_api.TaskHandoff.id == handoff_id))
            assert handoff is not None and handoff.status == "failed" and handoff.lease_owner is None
            approval = await db.scalar(select(workflow_api.Approval).where(workflow_api.Approval.id == approval_id))
            assert approval is not None and approval.status == "expired"

        retry = await executor.retry_dead_letter(exhausted_id, str(tenant_id), user_id)
        duplicate_retry = await executor.retry_dead_letter(exhausted_id, str(tenant_id), user_id)
        assert retry["already_retried"] is False
        assert duplicate_retry["already_retried"] is True
        assert retry["execution_id"] == duplicate_retry["execution_id"]
        assert retry["redriven_from_execution_id"] == str(exhausted_id)
        async with sessions() as db:
            await bind_tenant_scope(db, tenant_id)
            child = await db.get(ExecutionGraphModel, uuid.UUID(retry["execution_id"]))
            source = await db.get(ExecutionGraphModel, exhausted_id)
            assert child is not None and child.status == "scheduled" and child.lease_reclaim_count == 0
            assert child.redriven_from_execution_id == exhausted_id
            assert source is not None and source.status == "dead_letter"
            retry_graph = json.loads(child.graph_data)
            assert retry_graph["nodes"][0]["output"] == {"result": "saved"}
            assert retry_graph["nodes"][0]["status"] == "completed"
            assert retry_graph["nodes"][1]["status"] == "pending"
            event = (
                await db.execute(
                    select(OrchestrationEvent).where(
                        OrchestrationEvent.entity_type == "workflow_execution",
                        OrchestrationEvent.entity_id == child.id,
                        OrchestrationEvent.event_type == "created",
                    )
                )
            ).scalar_one()
            assert event.source == "workflow_operator_retry"
            assert event.actor_user_id == user_id

        with pytest.raises(LookupError, match="not found"):
            await executor.retry_dead_letter(exhausted_id, str(other_tenant_id), user_id)

        start_claims = asyncio.Event()
        ready = 0
        ready_lock = asyncio.Lock()
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold_claim(
            claimed_execution_id: str,
            _lease_id: uuid.UUID,
            _tenant_uuid: uuid.UUID,
            execution: object,
        ) -> ExecutionGraph:
            execution.close()  # type: ignore[attr-defined]
            started.set()
            await release.wait()
            return ExecutionGraph(claimed_execution_id, str(session_id), [], [], status="resuming")

        monkeypatch.setattr(executor, "_execute_with_lease", hold_claim)
        async def synchronized_resume() -> None:
            nonlocal ready
            async with ready_lock:
                ready += 1
                if ready == 2:
                    start_claims.set()
            await start_claims.wait()
            await executor.resume(race_id, str(tenant_id))

        first = asyncio.create_task(synchronized_resume())
        second = asyncio.create_task(synchronized_resume())
        await asyncio.wait_for(started.wait(), timeout=5)
        release.set()
        race_results = await asyncio.gather(first, second, return_exceptions=True)
        failures = [result for result in race_results if isinstance(result, RuntimeError)]
        assert len(failures) == 1
        assert "already being resumed" in str(failures[0])
        assert sum(result is None for result in race_results) == 1
        async with sessions() as db:
            await bind_tenant_scope(db, tenant_id)
            raced = await db.get(ExecutionGraphModel, race_id)
            assert raced is not None and raced.lease_reclaim_count == 1

        stale_snapshot = _snapshot("running")
        graph = ExecutionGraph(
            str(stale_id),
            str(session_id),
            [],
            [],
            status="running",
        )
        with pytest.raises(RuntimeError, match="lease was lost"):
            await executor._persist_execution_graph(graph, "feature", {}, tenant_id, old_owner)
        real_sleep = asyncio.sleep

        async def no_wait(_seconds: float) -> None:
            return None

        monkeypatch.setattr(executor_module.asyncio, "sleep", no_wait)
        assert await executor._heartbeat(str(stale_id), old_owner, tenant_id) is False
        await executor._mark_failed(str(stale_id), old_owner, tenant_id, error_message="stale worker")
        async with sessions() as db:
            await bind_tenant_scope(db, tenant_id)
            stale = await db.get(ExecutionGraphModel, stale_id)
            assert stale is not None and stale.status == "running" and stale.graph_data == stale_snapshot

        # Start a fenced write while its lease is valid, hold the target row lock
        # until after expiry, and verify the predicate observes wall-clock time.
        async with engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
            )
            await connection.execute(
                text("UPDATE execution_graphs SET lease_expires_at = clock_timestamp() + interval '1 second' WHERE id = :id"),
                {"id": stale_id},
            )
        lock_connection = await engine.connect()
        lock_transaction = await lock_connection.begin()
        await lock_connection.execute(
            text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
        )
        await lock_connection.execute(
            text("SELECT id FROM execution_graphs WHERE id = :id FOR UPDATE"), {"id": stale_id}
        )
        delayed_write = asyncio.create_task(
            executor._persist_execution_graph(graph, "feature", {}, tenant_id, old_owner)
        )
        await real_sleep(0.15)
        assert not delayed_write.done()
        await real_sleep(1.1)
        await lock_transaction.commit()
        await lock_connection.close()
        with pytest.raises(RuntimeError, match="lease was lost"):
            await delayed_write
        async with sessions() as db:
            await bind_tenant_scope(db, tenant_id)
            stale = await db.get(ExecutionGraphModel, stale_id)
            assert stale is not None and stale.graph_data == stale_snapshot

        await scheduler._finalize_stale_workflow_cancellation(cancelled_id, tenant_id)
        async with sessions() as db:
            await bind_tenant_scope(db, tenant_id)
            cancelled = await db.get(ExecutionGraphModel, cancelled_id)
            assert cancelled is not None and cancelled.status == "cancelled"
            cancelled_graph = json.loads(cancelled.graph_data)
            assert cancelled_graph["nodes"][0]["output"] == {"result": "saved"}
            assert cancelled_graph["nodes"][1]["status"] == "cancelled"
            assert (
                await db.execute(select(TaskModel.id).where(TaskModel.execution_id == cancelled_id))
            ).scalar_one_or_none() is None
    finally:
        tenant_context.clear()
        async with engine.begin() as connection:
            await connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)})
            await connection.execute(text("DELETE FROM approvals WHERE id = :id"), {"id": approval_id})
            await connection.execute(text("DELETE FROM task_handoffs WHERE id = :id"), {"id": handoff_id})
            await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": unfinished_task_id})
            await connection.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": parent_task_id})
            await connection.execute(text("DELETE FROM teammate_project_assignments WHERE id = :id"), {"id": assignment_id})
            await connection.execute(text("DELETE FROM projects WHERE id = :id"), {"id": project_id})
            await connection.execute(text("DELETE FROM teammate_profiles WHERE id = :id"), {"id": teammate_id})
            await connection.execute(
                text("DELETE FROM execution_graphs WHERE session_id = :session_id"), {"session_id": session_id}
            )
            await connection.execute(text("DELETE FROM sessions WHERE id = :session_id"), {"session_id": session_id})
            await connection.execute(
                text("DELETE FROM organization_memberships WHERE organization_id = :organization_id"),
                {"organization_id": organization_id},
            )
            await connection.execute(text("DELETE FROM organizations WHERE id = :id"), {"id": organization_id})
            await connection.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
            await connection.execute(
                text("DELETE FROM tenants WHERE id IN (:tenant_id, :other_tenant_id)"),
                {"tenant_id": tenant_id, "other_tenant_id": other_tenant_id},
            )
        await engine.dispose()
