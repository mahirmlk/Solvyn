import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from solvyn.api.v1 import teammates as teammate_routes
from solvyn.db.models import Message


@pytest.mark.asyncio
async def test_list_teammates_includes_only_assigned_project_ids() -> None:
    organization_id, teammate_id, project_id, other_project_id = (uuid.uuid4() for _ in range(4))
    teammate = SimpleNamespace(
        id=teammate_id,
        organization_id=organization_id,
        name="Engineer",
        role="Software Engineer",
        description="",
        responsibilities="",
        agent_type="backend-engineer",
        is_enabled=True,
        status="idle",
        model_provider=None,
        model_name=None,
        max_tokens_per_run=None,
        max_run_seconds=None,
        created_at=None,
        updated_at=None,
    )
    teammates_result = MagicMock()
    teammates_result.scalars.return_value.all.return_value = [teammate]
    assignments_result = MagicMock()
    assignments_result.all.return_value = [(teammate_id, project_id)]
    db = AsyncMock()
    db.execute.side_effect = [teammates_result, assignments_result]

    with patch.object(teammate_routes, "require_org_membership", new_callable=AsyncMock):
        response = await teammate_routes.list_teammates(organization_id, db)

    assert response["teammates"][0]["project_ids"] == [str(project_id)]
    assert str(other_project_id) not in response["teammates"][0]["project_ids"]


@pytest.mark.asyncio
async def test_assigned_project_lookup_excludes_inactive_projects() -> None:
    teammate = SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4())
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute.return_value = result

    with pytest.raises(HTTPException) as error:
        await teammate_routes._assigned_project(db, teammate, uuid.uuid4(), uuid.uuid4())

    assert error.value.status_code == 404
    statement = str(db.execute.await_args.args[0])
    assert "projects.is_active IS true" in statement


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

    async def refresh_record(record: object) -> None:
        if record is conversation:
            conversation.updated_at = datetime.now(UTC)

    db.refresh.side_effect = refresh_record
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
    assert conversation.updated_at is not None
    assert conversation.title == "Earlier question"


@pytest.mark.asyncio
async def test_failed_conversation_turn_does_not_persist_an_unanswered_user_message() -> None:
    tenant_id, teammate_id, project_id, conversation_id, session_id = (uuid.uuid4() for _ in range(5))
    teammate = SimpleNamespace(id=teammate_id, organization_id=uuid.uuid4(), name="Engineer")
    conversation = SimpleNamespace(
        id=conversation_id,
        tenant_id=tenant_id,
        teammate_id=teammate_id,
        project_id=project_id,
        session_id=session_id,
        title="New conversation",
        created_at=None,
        updated_at=None,
    )
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute.return_value = query_result

    with (
        patch.object(teammate_routes, "require_tenant", return_value=tenant_id),
        patch.object(teammate_routes, "require_org_membership", new_callable=AsyncMock),
        patch.object(teammate_routes, "_load_teammate", new_callable=AsyncMock, return_value=teammate),
        patch.object(teammate_routes, "_load_conversation", new_callable=AsyncMock, return_value=conversation),
        patch.object(
            teammate_routes,
            "run_teammate",
            new_callable=AsyncMock,
            side_effect=RuntimeError("model unavailable"),
        ),
    ):
        with pytest.raises(RuntimeError, match="model unavailable"):
            await teammate_routes.send_teammate_conversation_message(
                teammate_id,
                conversation_id,
                teammate_routes.TeammateConversationMessageIn(content="Try this request"),
                db,
            )

    db.add.assert_not_called()
    db.rollback.assert_awaited_once()
    assert conversation.title == "New conversation"
    assert conversation.updated_at is None


@pytest.mark.asyncio
async def test_successful_first_turn_sets_conversation_title_after_model_reply() -> None:
    tenant_id, teammate_id, project_id, conversation_id, session_id = (uuid.uuid4() for _ in range(5))
    teammate = SimpleNamespace(id=teammate_id, organization_id=uuid.uuid4(), name="Engineer")
    conversation = SimpleNamespace(
        id=conversation_id,
        tenant_id=tenant_id,
        teammate_id=teammate_id,
        project_id=project_id,
        session_id=session_id,
        title="New conversation",
        created_at=None,
        updated_at=None,
    )
    query_result = MagicMock()
    query_result.scalars.return_value.all.return_value = []
    db = AsyncMock()
    db.execute.return_value = query_result

    async def refresh_record(record: object) -> None:
        if record is conversation:
            conversation.updated_at = datetime.now(UTC)

    db.refresh.side_effect = refresh_record
    output = {"response": "First answer"}
    content = "Investigate deployment failures in production"

    with (
        patch.object(teammate_routes, "require_tenant", return_value=tenant_id),
        patch.object(teammate_routes, "require_org_membership", new_callable=AsyncMock),
        patch.object(teammate_routes, "_load_teammate", new_callable=AsyncMock, return_value=teammate),
        patch.object(teammate_routes, "_load_conversation", new_callable=AsyncMock, return_value=conversation),
        patch.object(teammate_routes, "run_teammate", new_callable=AsyncMock, return_value=output),
    ):
        response = await teammate_routes.send_teammate_conversation_message(
            teammate_id,
            conversation_id,
            teammate_routes.TeammateConversationMessageIn(content=content),
            db,
        )

    assert conversation.title == content
    assert conversation.updated_at is not None
    assert response["conversation"]["title"] == content


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
