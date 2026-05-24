from unittest.mock import AsyncMock

import pytest

from solvyn.observability.audit import AuditLogger
from solvyn.observability.tracing import tracing_manager
from solvyn.observability.websocket import ws_manager


# ─── Tracing Manager ────────────────────────────────────────
def test_tracing_create_trace() -> None:
    trace_id = tracing_manager.create_trace("test-trace", "test-agent", session_id="s1", tenant_id="t1")
    assert trace_id is not None
    assert trace_id.startswith("trace_")
    trace = tracing_manager.get_trace(trace_id)
    assert trace is not None
    assert trace["name"] == "test-trace"
    assert trace["agent_type"] == "test-agent"


def test_tracing_add_span() -> None:
    trace_id = tracing_manager.create_trace("test", "a", session_id="s1")
    tracing_manager.add_span(trace_id, "do-work", span_type="llm", input_data={"key": "val"}, duration_ms=50.0)
    trace = tracing_manager.get_trace(trace_id)
    assert trace is not None
    assert len(trace["spans"]) == 1
    assert trace["spans"][0]["name"] == "do-work"
    assert trace["spans"][0]["duration_ms"] == 50.0


def test_tracing_add_span_with_error() -> None:
    trace_id = tracing_manager.create_trace("test", "a")
    tracing_manager.add_span(trace_id, "failing", error="something broke")
    trace = tracing_manager.get_trace(trace_id)
    assert trace["spans"][0]["error"] == "something broke"


def test_tracing_get_missing_trace() -> None:
    assert tracing_manager.get_trace("nonexistent") is None


def test_tracing_list_traces() -> None:
    for i in range(5):
        tracing_manager.create_trace(f"trace-{i}", "a")
    traces = tracing_manager.list_traces(limit=3)
    assert len(traces) == 3


def test_tracing_get_session_traces() -> None:
    tracing_manager.create_trace("t1", "a", session_id="sess-1")
    tracing_manager.create_trace("t2", "b", session_id="sess-1")
    tracing_manager.create_trace("t3", "c", session_id="sess-2")
    traces = tracing_manager.get_session_traces("sess-1")
    assert len(traces) == 2


# ─── WebSocket Manager ──────────────────────────────────────
@pytest.mark.asyncio
async def test_websocket_handle_graph_update() -> None:
    mock_ws = AsyncMock()
    await ws_manager.connections.connect(mock_ws, "t1")
    await ws_manager.handle_graph_update("g1", "n1", "agent-a", "running", "s1", "t1")
    mock_ws.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_websocket_disconnect() -> None:
    mock_ws = AsyncMock()
    await ws_manager.connections.connect(mock_ws, "t1")
    ws_manager.connections.disconnect(mock_ws, "t1")
    await ws_manager.handle_graph_update("g1", "n1", "a", "running", "s1", "t1")
    # no crash after disconnect


@pytest.mark.asyncio
async def test_websocket_session_scoped() -> None:
    mock_ws = AsyncMock()
    await ws_manager.connections.connect(mock_ws, "t1", session_id="s1")
    await ws_manager.handle_graph_update("g1", "n1", "a", "completed", "s1", "t1")
    mock_ws.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_websocket_handle_agent_message() -> None:
    mock_ws = AsyncMock()
    await ws_manager.connections.connect(mock_ws, "t1")
    await ws_manager.handle_agent_message("agent-a", "agent-b", "request", {"text": "hi"}, "s1", "t1")
    mock_ws.send_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_websocket_handle_workflow_update() -> None:
    mock_ws = AsyncMock()
    await ws_manager.connections.connect(mock_ws, "t1")
    await ws_manager.handle_workflow_update("feature", "code-gen", "completed", "s1", "t1")
    mock_ws.send_text.assert_awaited_once()


# ─── Audit Logger ───────────────────────────────────────────
@pytest.mark.asyncio
async def test_audit_logger_log() -> None:
    logger = AuditLogger()
    logger.log = AsyncMock(return_value={"id": "a1", "action": "agent.run", "status": "ok"})
    entry = await logger.log(action="agent.run", agent_type="test-agent", tenant_id="t1", session_id="s1")
    assert entry["action"] == "agent.run"


@pytest.mark.asyncio
async def test_audit_logger_search() -> None:
    logger = AuditLogger()
    logger.search = AsyncMock(return_value=[
        {"id": "a1", "action": "agent.run", "agent_type": "test-agent"},
    ])
    results = await logger.search(action="agent.run", limit=5)
    assert len(results) == 1


@pytest.mark.asyncio
async def test_audit_logger_search_empty() -> None:
    logger = AuditLogger()
    logger.search = AsyncMock(return_value=[])
    results = await logger.search(action="nonexistent")
    assert results == []
