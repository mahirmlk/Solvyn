"""Observability endpoints: metrics, traces, audit logs, and cost analytics.

These endpoints are read-only and never mutate state. They are intended to
back:

* Prometheus scrapers (the ``/metrics`` endpoint),
* Internal dashboards (traces, audit log, session/tenant analytics),
* Real-time WebSocket subscribers (per-tenant event stream).

All endpoints require the standard ``X-API-Key`` header; tenant-scoped
endpoints additionally check that the caller's tenant matches the resource.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.cost_tracker import cost_tracker
from solvyn.api_metadata import COMMON_RESPONSES
from solvyn.config import settings
from solvyn.db.models import Session as SessionModel
from solvyn.db.models import Tenant
from solvyn.db.session import bind_tenant_scope, get_session
from solvyn.multi_tenant.auth import verify_api_key
from solvyn.multi_tenant.middleware import SHARED_SECRET_PRINCIPAL_ID, tenant_context
from solvyn.observability.audit import audit_logger
from solvyn.observability.metrics import Metrics, MetricsMiddleware
from solvyn.observability.tracing import tracing_manager
from solvyn.observability.websocket import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/observability", tags=["observability"])

metrics_handler = Metrics()
metrics_middleware = MetricsMiddleware()


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    description=(
        "Prometheus exposition endpoint serving the in-process metrics "
        "registry. Returned content-type is `text/plain; version=0.0.4` per "
        "the Prometheus specification. Scrape with the standard interval "
        "(15s by default); there is no pagination or filtering — the full "
        "registry is returned every time."
    ),
    response_description="Prometheus text-format metrics payload.",
    responses={200: {"content": {"text/plain": {"example": "# HELP solvyn_http_requests_total ..."}}}},
)
async def get_metrics() -> Response:
    """Return the current Prometheus metrics snapshot."""
    return Response(
        content=metrics_handler.generate(),
        media_type="text/plain; version=0.0.4",
    )


@router.get(
    "/traces",
    summary="List execution traces",
    description=(
        "Return a list of recently completed agent execution traces. By "
        "default the most recent ``limit`` traces are returned; supply a "
        "`session_id` to narrow the list to a single session. Each trace is "
        "a thin record (id, name, agent_type, span count, session_id) — use "
        "`GET /observability/traces/{trace_id}` to fetch the full span tree."
    ),
    response_description="List of trace summaries.",
    responses=COMMON_RESPONSES,
)
async def list_traces(session_id: str | None = None, limit: int = 20) -> dict[str, Any]:
    """List recent traces, optionally scoped to a single session."""
    if session_id:
        traces = tracing_manager.get_session_traces(session_id)
    else:
        traces = tracing_manager.list_traces(limit)
    return {"traces": traces, "count": len(traces)}


@router.get(
    "/traces/{trace_id}",
    summary="Fetch a single trace",
    description=(
        "Return the full span tree for a single trace. The trace must have "
        "been emitted by a Solvyn agent invocation; ad-hoc trace ids are not "
        "recognised. The response is identical in shape to the in-memory "
        "representation used by the runtime."
    ),
    response_description="The complete trace, including all spans.",
    responses={
        404: {
            "description": "No trace with the given id exists (or it has aged out).",
            "content": {"application/json": {"example": {"detail": "Trace not found"}}},
        },
        **COMMON_RESPONSES,
    },
)
async def get_trace(trace_id: str) -> dict[str, Any]:
    """Return the full span tree for a single trace id."""
    trace = tracing_manager.get_trace(trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")
    return {"trace": trace}


@router.get(
    "/audit",
    summary="Search the audit log",
    description=(
        "Search the audit log for entries matching the supplied filters. "
        "All filter parameters are optional and combine with logical AND. "
        "Results are returned newest-first, capped at ``limit`` entries "
        "(default 50, hard maximum 500)."
    ),
    response_description="Matching audit log entries.",
    responses=COMMON_RESPONSES,
)
async def get_audit_logs(
    action: str | None = None,
    agent_type: str | None = None,
    session_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Query the audit log with optional filters."""
    tenant_id = tenant_context.tenant_id
    if tenant_id is None or not tenant_context.is_authenticated:
        raise HTTPException(status_code=401, detail="Authenticated tenant context required")
    entries = await audit_logger.search(
        action=action,
        agent_type=agent_type,
        session_id=session_id,
        limit=limit,
        tenant_id=tenant_id,
    )
    return {"entries": entries, "count": len(entries)}


@router.get(
    "/analytics/session/{session_id}",
    summary="Per-session cost analytics",
    description=(
        "Return the cumulative LLM cost and per-agent cost breakdown for a "
        "single session. Useful for in-product spend meters and post-hoc "
        "analysis. The cost values are in USD and are computed at the "
        "provider's published list price; cached responses may reduce the "
        "actual charged amount."
    ),
    response_description="Cost totals and per-agent breakdown for the session.",
    responses=COMMON_RESPONSES,
)
async def get_session_analytics(session_id: str, db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Return the cost analytics for a single session."""
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc

    current_tenant = tenant_context.tenant_id
    if current_tenant is None:
        if (
            tenant_context.authenticated_tenant_id != SHARED_SECRET_PRINCIPAL_ID
            or tenant_context.user_id is not None
        ):
            raise HTTPException(status_code=401, detail="Analytics tenant is not configured")
        tenant_ref = settings.default_tenant_id
        try:
            tenant_uuid = uuid.UUID(tenant_ref)
        except ValueError:
            tenant = (
                await db.execute(
                    select(Tenant).where(Tenant.slug == tenant_ref, Tenant.is_active.is_(True))
                )
            ).scalar_one_or_none()
        else:
            tenant = (
                await db.execute(select(Tenant).where(Tenant.id == tenant_uuid, Tenant.is_active.is_(True)))
            ).scalar_one_or_none()
        if tenant is None:
            raise HTTPException(status_code=401, detail="Analytics tenant is not configured")
        current_tenant = tenant.id

    await bind_tenant_scope(db, current_tenant)
    owned_session = (
        await db.execute(
            select(SessionModel.id).where(
                SessionModel.id == session_uuid,
                SessionModel.tenant_id == current_tenant,
            )
        )
    ).scalar_one_or_none()
    if owned_session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    tenant_id = str(current_tenant)
    totals = await cost_tracker.get_session_totals(session_id, tenant_id)
    breakdown = await cost_tracker.get_agent_breakdown(session_id, tenant_id)
    return {"session_id": session_id, "totals": totals, "agent_breakdown": breakdown, "tenant_id": tenant_id}


@router.get(
    "/analytics/tenant/{tenant_id}",
    summary="Per-tenant cost analytics",
    description=(
        "Return the cumulative LLM cost across every session owned by a "
        "tenant. The endpoint enforces tenant isolation: callers can only "
        "read the analytics for the tenant they are authenticated as, and "
        "attempts to read another tenant's data are rejected with ``403``."
    ),
    response_description="Cost totals for the tenant.",
    responses={
        403: {
            "description": "Caller is authenticated as a different tenant than the one in the URL.",
            "content": {"application/json": {"example": {"detail": "Access denied: tenant mismatch"}}},
        },
        **COMMON_RESPONSES,
    },
)
async def get_tenant_analytics(tenant_id: str) -> dict[str, Any]:
    """Return the cost analytics for a single tenant."""
    current_tenant = str(tenant_context.tenant_id) if tenant_context.tenant_id else settings.default_tenant_id
    if tenant_id != current_tenant:
        raise HTTPException(status_code=403, detail="Access denied: tenant mismatch")
    totals = await cost_tracker.get_tenant_totals(tenant_id)
    return {"tenant_id": tenant_id, "totals": totals}


@router.get(
    "/analytics/health",
    summary="Observability subsystem health",
    description=(
        "Lightweight liveness check for the observability subsystem itself. "
        "Returns whether the in-process tracing store is enabled and how "
        "many traces it currently holds. Intended for the internal "
        "monitoring dashboard; pair with the public ``/api/v1/health/deep`` "
        "for a full readiness picture."
    ),
    response_description="Snapshot of the observability subsystem state.",
    responses=COMMON_RESPONSES,
)
async def get_health_analytics() -> dict[str, Any]:
    """Return a health snapshot for the observability subsystem."""
    return {
        "service": "solvyn",
        "version": "0.1.0",
        "timestamp": time.time(),
        "tracing_enabled": tracing_manager._enabled,
        "active_traces": len(tracing_manager._traces),
    }


@router.websocket(
    "/ws/{tenant_id}",
    name="observability_stream",
    # WebSockets cannot declare HTTP-style responses; the summary/description
    # are surfaced in the OpenAPI schema for discoverability only.
)
async def websocket_endpoint(websocket: WebSocket, tenant_id: str, session_id: str | None = None) -> None:
    """Real-time observability event stream for a tenant.

    The WebSocket authenticates the same way the HTTP endpoints do — pass
    the API key as the ``api_key`` query parameter or the ``X-API-Key``
    header. In per-tenant mode the key must match the tenant id in the URL.

    The server emits JSON-encoded events as they occur (agent invocations,
    cost updates, audit log entries). Clients are responsible for handling
    keepalive pings.
    """
    api_key = websocket.query_params.get("api_key") or websocket.headers.get("X-API-Key")
    if not api_key:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    # Validate API key and tenant ownership (key must belong to the URL tenant)
    from solvyn.multi_tenant.auth import _validate_tenant_api_key
    if tenant_context.tenant_id is not None:
        tenant = await _validate_tenant_api_key(api_key, expected_tenant_id=tenant_context.tenant_id)
        if tenant is None or str(tenant.id) != tenant_id:
            await websocket.close(code=4001, reason="Unauthorized")
            return
    else:
        # Shared-secret mode
        if not verify_api_key(api_key, settings.api_key_secret):
            await websocket.close(code=4001, reason="Unauthorized")
            return

    await ws_manager.connections.connect(websocket, tenant_id, session_id)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error")
    finally:
        ws_manager.connections.disconnect(websocket, tenant_id, session_id)
