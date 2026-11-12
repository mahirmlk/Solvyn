import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from solvyn.api.v1 import teammates as teammate_routes
from solvyn.db.models import Message


@pytest.mark.asyncio
async def test_send_conversation_message_persists_turn_and_reuses_bound_session() -> None:
    tenant_id, teammate_id, project_id, conversation_id, session_id = (uuid.uuid4() for _ in range(5))
    teammate = SimpleNamespace(id=teammate_id, organization_id=uuid.uuid4(), name="Engineer")
    conversation = SimpleNamespace(
        id=conversation_id,
        tenant_id=tenant_id,
        teammate_id=teammate_id,
        project_id=project_id,
        session_id=session_id,
        title="Earlier question",
        created_at=None,
        updated_at=None,
    )
    prior = SimpleNamespace(role="user", content="Earlier question")
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = [prior]
    db = AsyncMock()
    db.execute.return_value = query_result
    output = {"code": "answer", "provider": "test", "model": "test-model", "cost": 0}

    with (
        patch.object(teammate_routes, "require_tenant", return_value=tenant_id),
        patch.object(teammate_routes, "require_org_membership", new_callable=AsyncMock) as membership,
        patch.object(teammate_routes, "_load_teammate", new_callable=AsyncMock, return_value=teammate),
        patch.object(teammate_routes, "_load_conversation", new_callable=AsyncMock, return_value=conversation),
        patch.object(teammate_routes, "run_teammate", new_callable=AsyncMock, return_value=output) as run,
    ):
        response = await teammate_routes.send_teammate_conversation_message(
            teammate_id,
            conversation_id,
            teammate_routes.TeammateConversationMessageIn(content="Follow-up question"),
            db,
        )

    membership.assert_awaited_once()
    run.assert_awaited_once()
    assert run.await_args.kwargs["session_id"] == str(session_id)
    assert "Earlier question" in run.await_args.kwargs["input_data"]["text"]
    assert "Follow-up question" in run.await_args.kwargs["input_data"]["text"]
    saved_messages = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], Message)]
    assert [message.role for message in saved_messages] == ["user", "assistant"]
    assert json.loads(saved_messages[1].content) == output
    assert response["output"] == output
    assert len(response["messages"]) == 2


@pytest.mark.asyncio
async def test_send_conversation_message_rejects_unowned_conversation_before_model_call() -> None:
    tenant_id, teammate_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    teammate = SimpleNamespace(id=teammate_id, organization_id=uuid.uuid4(), name="Engineer")
    db = AsyncMock()

    with (
        patch.object(teammate_routes, "require_tenant", return_value=tenant_id),
        patch.object(teammate_routes, "require_org_membership", new_callable=AsyncMock),
        patch.object(teammate_routes, "_load_teammate", new_callable=AsyncMock, return_value=teammate),
        patch.object(
            teammate_routes,
            "_load_conversation",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=404, detail="Conversation not found"),
        ),
        patch.object(teammate_routes, "run_teammate", new_callable=AsyncMock) as run,
    ):
        with pytest.raises(HTTPException) as error:
            await teammate_routes.send_teammate_conversation_message(
                teammate_id,
                conversation_id,
                teammate_routes.TeammateConversationMessageIn(content="hello"),
                db,
            )

    assert error.value.status_code == 404
    run.assert_not_awaited()
