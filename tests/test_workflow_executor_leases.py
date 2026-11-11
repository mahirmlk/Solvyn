import asyncio
import uuid

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
    from solvyn.config import settings
    from solvyn.workflows.executor import WorkflowExecutor

    executor = WorkflowExecutor()
    graph_cancelled = asyncio.Event()
    heartbeat_cancelled = asyncio.Event()
    monkeypatch.setattr(settings, "workflow_execution_timeout_seconds", 0.01)

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

    with pytest.raises(TimeoutError, match="Workflow execution exceeded 0.01 seconds"):
        await executor._execute_with_lease(str(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), run_graph())

    assert graph_cancelled.is_set()
    assert heartbeat_cancelled.is_set()


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
