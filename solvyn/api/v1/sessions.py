"""Session management endpoints.

A *session* is the durable container for a user-facing conversation: it
holds the tenant-scoped message history, the LLM trace, and the
configuration of any agents that participated. All endpoints in this module
are tenant-scoped — they will return an empty list (or 400) when no tenant
context is present.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api_metadata import COMMON_RESPONSES
from solvyn.db.models import Session
from solvyn.db.session import bind_tenant_scope, get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(prefix="/sessions", tags=["sessions"])

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@router.get(
    "/",
    summary="List sessions for the current tenant",
    description=(
        "Return up to 50 of the most recently created sessions that belong to "
        "the tenant resolved from the `X-Tenant-Slug` header. When the header "
        "is missing (single-tenant mode) the response is an empty list rather "
        "than an error, so that monitoring dashboards can poll the endpoint "
        "without leaking cross-tenant data."
    ),
    response_description="List of session summaries scoped to the calling tenant.",
    responses=COMMON_RESPONSES,
)
async def list_sessions(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """List the recent sessions for the current tenant."""
    current_tenant_id = tenant_context.tenant_id
    if not current_tenant_id:
        return {"sessions": []}
    await bind_tenant_scope(db, current_tenant_id)
    result = await db.execute(select(Session).where(Session.tenant_id == current_tenant_id).limit(50))
    sessions = result.scalars().all()
    return {"sessions": [{"id": str(s.id), "title": s.title, "status": s.status, "created_at": s.created_at.isoformat()} for s in sessions]}


@router.post(
    "/",
    summary="Create a new session",
    description=(
        "Create a fresh, empty session owned by the calling tenant. Requires "
        "an `X-Tenant-Slug` header so that the new row can be associated with "
        "the correct tenant; if the header is missing the request is rejected "
        "with ``400`` rather than silently falling back to the default tenant."
    ),
    response_description="The newly created session.",
    status_code=201,
    responses={
        400: {
            "description": "Missing `X-Tenant-Slug` header.",
            "content": {"application/json": {"example": {"detail": "X-Tenant-Slug header required to create a session"}}},
        },
        **COMMON_RESPONSES,
    },
)
async def create_session(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Create a new session for the calling tenant."""
    if not tenant_context.tenant_id:
        raise HTTPException(status_code=400, detail="X-Tenant-Slug header required to create a session")
    await bind_tenant_scope(db, tenant_context.tenant_id)
    session = Session(
        title="New Session",
        tenant_id=tenant_context.tenant_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {"id": str(session.id), "title": session.title, "status": session.status}
