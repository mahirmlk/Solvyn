import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from solvyn.multi_tenant.middleware import tenant_context


@pytest.mark.asyncio
async def test_list_sessions_rejects_request_without_resolved_tenant() -> None:
    from solvyn.api.v1.sessions import list_sessions

    tenant_context.clear()
    result = await list_sessions(AsyncMock())
    assert result == {"sessions": []}


@pytest.mark.asyncio
async def test_list_sessions_filters_records_by_current_tenant() -> None:
    from solvyn.api.v1.sessions import list_sessions

    tenant_id = uuid.uuid4()
    tenant_context.set(tenant_id, "tenant-a")
    db = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    db.execute.return_value = mock_result
    try:
        result = await list_sessions(db)
        assert result == {"sessions": []}
    finally:
        tenant_context.clear()

    statement = db.execute.await_args.args[0]
    assert tenant_id in statement.compile().params.values()


@pytest.mark.asyncio
async def test_create_session_assigns_current_tenant() -> None:
    from solvyn.api.v1.sessions import create_session

    tenant_id = uuid.uuid4()
    tenant_context.set(tenant_id, "tenant-a")
    db = AsyncMock()
    try:
        await create_session(db)
    finally:
        tenant_context.clear()

    session = db.add.call_args.args[0]
    assert session.tenant_id == tenant_id
