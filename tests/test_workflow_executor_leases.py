import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from solvyn.agents.schema import ExecutionGraph


@pytest.mark.asyncio
async def test_lost_lease_cancels_graph_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.workflows.executor import WorkflowExecutor

    executor = WorkflowExecutor()
    graph_cancelled = asyncio.Event()

    async def run_graph() -> ExecutionGraph:
        try:
            await asyncio.Event().wait()
        finally:
            graph_cancelled.set()
        raise AssertionError("The graph should be cancelled after lease loss")

    async def lease_lost(execution_id: str, lease_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
        return False

    monkeypatch.setattr(executor, "_heartbeat", lease_lost)

    with pytest.raises(RuntimeError, match="lease was lost"):
        await executor._execute_with_lease(str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), run_graph())

    assert graph_cancelled.is_set()


@pytest.mark.asyncio
async def test_workflow_deadline_cancels_graph_and_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.workflows import executor as executor_module

    executor = executor_module.WorkflowExecutor()
    graph_cancelled = asyncio.Event()
    heartbeat_cancelled = asyncio.Event()
    monkeypatch.setattr(executor_module.settings, "workflow_execution_timeout_seconds", 0.01)

    async def run_graph() -> ExecutionGraph:
        try:
            await asyncio.Event().wait()
        finally:
            graph_cancelled.set()
        raise AssertionError("The graph should be cancelled after the deadline")

    async def heartbeat(_execution_id: str, _lease_id: uuid.UUID, _tenant_id: uuid.UUID) -> bool:
        try:
            await asyncio.Event().wait()
        finally:
            heartbeat_cancelled.set()
        raise AssertionError("The heartbeat should be cancelled after the deadline")

    monkeypatch.setattr(executor, "_heartbeat", heartbeat)

    async def deadline_reached(_tasks: object, *, timeout: float, return_when: object) -> tuple[set[object], set[object]]:
        assert timeout == 0.01
        assert return_when is asyncio.FIRST_COMPLETED
        await asyncio.sleep(0)
        return set(), set()

    monkeypatch.setattr(executor_module.asyncio, "wait", deadline_reached)

    with pytest.raises(TimeoutError, match="Workflow execution exceeded 0.01 seconds"):
        await executor._execute_with_lease(str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), run_graph())

    assert graph_cancelled.is_set()
    assert heartbeat_cancelled.is_set()


@pytest.mark.asyncio
async def test_timeout_failure_marks_unfinished_nodes_and_tasks_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.db.models import ExecutionGraph as ExecutionGraphModel
    from solvyn.workflows import executor as executor_module

    execution_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    record = SimpleNamespace(
        graph_data=json.dumps(
            {
                "status": "running",
                "nodes": [
                    {"id": "active", "status": "running"},
                    {"id": "waiting", "status": "pending"},
                    {"id": "approval", "status": "waiting_approval"},
                    {"id": "done", "status": "completed"},
                ]
            }
        )
    )

    class FakeResult:
        def __init__(self, value: object | None) -> None:
            self.value = value

        def scalar_one_or_none(self) -> object | None:
            return self.value

    class FakeSession:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.statements: list[object] = []
            self.committed = False

        async def __aenter__(self) -> "FakeSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> FakeResult:
            self.execute_calls += 1
            self.statements.append(_statement)
            return FakeResult(None if self.execute_calls == 1 else execution_id)

        async def get(self, model: type[object], _identity: uuid.UUID) -> object | None:
            assert model is ExecutionGraphModel
            return record

        async def commit(self) -> None:
            self.committed = True

    session = FakeSession()
    monkeypatch.setattr(executor_module, "async_session_factory", lambda: session)
    monkeypatch.setattr(executor_module, "bind_tenant_scope", AsyncMock())

    await executor_module.WorkflowExecutor()._mark_failed(
        str(execution_id),
        uuid.uuid4(),
        tenant_id,
        error_message="Workflow execution exceeded 1800 seconds",
    )

    graph_snapshot = json.loads(record.graph_data)
    nodes = graph_snapshot["nodes"]
    assert graph_snapshot["status"] == "failed"
    assert nodes[0]["status"] == "failed"
    assert nodes[0]["error"] == "Workflow execution exceeded 1800 seconds"
    assert nodes[1]["status"] == "failed"
    assert nodes[2]["status"] == "failed"
    assert nodes[3]["status"] == "completed"
    assert session.execute_calls == 3
    task_update_params = session.statements[2].compile().params
    assert "failed" in task_update_params.values()
    assert "Workflow execution exceeded 1800 seconds" in task_update_params.values()
    assert session.committed


def test_shared_secret_workflow_scope_uses_default_tenant() -> None:
    from solvyn.api.v1.workflows import _workflow_tenant_ref
    from solvyn.multi_tenant.middleware import tenant_context

    try:
        tenant_context.authenticate_shared_secret("USER")
        assert _workflow_tenant_ref() == "test-tenant"
    finally:
        tenant_context.clear()


def test_tenantless_jwt_cannot_use_default_workflow_tenant() -> None:
    from solvyn.api.v1.workflows import _workflow_tenant_ref
    from solvyn.multi_tenant.middleware import tenant_context

    try:
        tenant_context.set_authenticated(None, user_id=uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            _workflow_tenant_ref()
        assert exc_info.value.status_code == 403
    finally:
        tenant_context.clear()


@pytest.mark.asyncio
async def test_scoped_tenant_is_inherited_by_worker_tasks_and_reset() -> None:
    from solvyn.multi_tenant.middleware import tenant_context

    tenant_id = uuid.uuid4()

    async def observe_tenant() -> uuid.UUID | None:
        await asyncio.sleep(0)
        return tenant_context.tenant_id

    assert tenant_context.tenant_id is None
    with tenant_context.scoped_tenant(tenant_id):
        child = asyncio.create_task(observe_tenant())
        assert await child == tenant_id
        assert tenant_context.tenant_id == tenant_id
    assert tenant_context.tenant_id is None
