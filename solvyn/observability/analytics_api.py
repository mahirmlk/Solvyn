from typing import Any

from fastapi import APIRouter, Response, WebSocket, WebSocketDisconnect

from solvyn.agents.cost_tracker import cost_tracker
from solvyn.observability.audit import audit_logger
from solvyn.observability.metrics import Metrics, MetricsMiddleware
from solvyn.observability.tracing import tracing_manager
from solvyn.observability.websocket import ws_manager

router = APIRouter(prefix="/observability", tags=["observability"])

metrics_handler = Metrics()
metrics_middleware = MetricsMiddleware()


@router.get("/metrics")
async def get_metrics() -> Response:
    return Response(
        content=metrics_handler.generate(),
        media_type="text/plain; version=0.0.4",
    )


@router.get("/traces")
async def list_traces(session_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    if session_id:
        traces = tracing_manager.get_session_traces(session_id)
    else:
        traces = tracing_manager.list_traces(limit)
    return {"traces": traces, "count": len(traces)}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str) -> dict[str, Any]:
    trace = tracing_manager.get_trace(trace_id)
    if trace is None:
        return {"error": "Trace not found"}
    return {"trace": trace}


@router.get("/audit")
async def get_audit_logs(
    action: str | None = None,
    agent_type: str | None = None,
    session_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    entries = await audit_logger.search(action=action, agent_type=agent_type, session_id=session_id, limit=limit)
    return {"entries": entries, "count": len(entries)}


@router.get("/analytics/session/{session_id}")
async def get_session_analytics(session_id: str) -> dict[str, Any]:
    totals = await cost_tracker.get_session_totals(session_id)
    breakdown = await cost_tracker.get_agent_breakdown(session_id)
    return {"session_id": session_id, "totals": totals, "agent_breakdown": breakdown}


@router.get("/analytics/tenant/{tenant_id}")
async def get_tenant_analytics(tenant_id: str) -> dict[str, Any]:
    totals = await cost_tracker.get_tenant_totals(tenant_id)
    return {"tenant_id": tenant_id, "totals": totals}


@router.get("/analytics/health")
async def get_health_analytics() -> dict[str, Any]:
    import time

    return {
        "service": "solvyn",
        "version": "0.1.0",
        "timestamp": time.time(),
        "tracing_enabled": tracing_manager._enabled,
        "active_traces": len(tracing_manager._traces),
    }


@router.websocket("/ws/{tenant_id}")
async def websocket_endpoint(websocket: WebSocket, tenant_id: str, session_id: str | None = None) -> None:
    await ws_manager.connections.connect(websocket, tenant_id, session_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.connections.disconnect(websocket, tenant_id, session_id)
