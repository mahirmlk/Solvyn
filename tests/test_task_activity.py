"""Tests for the unified, task-scoped activity feed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from solvyn.repositories.github import GitHubRepositoryError


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return self.rows

    def scalar_one_or_none(self) -> Any | None:
        return self.rows[0] if self.rows else None

    def first(self) -> Any | None:
        return self.rows[0] if self.rows else None


class _ActivitySession:
    def __init__(self, results: list[list[Any]]) -> None:
        self.results = iter(results)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Rows:
        self.statements.append(statement)
        return _Rows(next(self.results))


def _time(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 6, hour, minute, tzinfo=UTC)


@pytest.mark.asyncio
async def test_task_activity_combines_durable_records_in_reverse_chronological_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.api.v1 import verification

    task_id, tenant_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handoff_id, run_id, delivery_id, child_task_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task = SimpleNamespace(
        id=task_id,
        tenant_id=tenant_id,
        execution_id=execution_id,
        status="completed",
        assigned_agent="software_engineer",
        created_at=_time(9, 0),
        updated_at=_time(9, 1),
        completed_at=_time(9, 1),
    )
    invocation = SimpleNamespace(
        id=uuid.uuid4(), task_id=child_task_id, status="succeeded", tool_name="workspace_read_file", path="src/main.py",
        error_code=None, started_at=_time(10, 0), completed_at=_time(10, 1),
    )
    run = SimpleNamespace(
        id=run_id, task_id=task_id, kind="test", status="passed", exit_code=0, timed_out=False,
        started_at=_time(9, 45), completed_at=_time(9, 50),
    )
    attempt_events = [
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant_id, task_id=task_id, verification_run_id=run_id,
            verification_kind="test", attempt_number=1, event_type="started", status="running",
            exit_code=None, timed_out=False, output_truncated=False, error=None,
            occurred_at=_time(9, 46),
        ),
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant_id, task_id=task_id, verification_run_id=run_id,
            verification_kind="test", attempt_number=1, event_type="finished", status="passed",
            exit_code=0, timed_out=False, output_truncated=False, error=None,
            occurred_at=_time(9, 49),
        ),
    ]
    delivery = SimpleNamespace(
        id=delivery_id, task_id=task_id, status="pull_request_open", updated_at=_time(9, 42), created_at=_time(9, 40),
        pull_request_title="Implement feature", branch_name="solvyn/task-42", pull_request_number=42,
    )
    handoff = SimpleNamespace(
        id=handoff_id, parent_task_id=uuid.uuid4(), child_task_id=task_id, status="completed",
        completed_at=_time(9, 30), created_at=_time(9, 20), delegated_to="software_engineer", reason="Implement feature",
    )
    approval = SimpleNamespace(
        id=uuid.uuid4(), verification_run_id=run_id, task_handoff_id=handoff_id,
        kind="verification", status="pending", decided_at=None,
        updated_at=_time(9, 25), created_at=_time(9, 25), summary="Run the test profile", decision_note=None,
    )
    usage = SimpleNamespace(
        id=uuid.uuid4(), task_id=child_task_id, created_at=_time(8, 0), agent_type="software-engineer",
        provider="openrouter", model="openai/gpt-4o-mini", prompt_tokens=120, completion_tokens=80,
        total_tokens=200, cost=0.000066,
    )
    task_event = SimpleNamespace(
        id=uuid.uuid4(), task_id=child_task_id, parent_task_id=task_id, event_type="status_changed",
        previous_status="running", status="completed", source="user_request", actor_user_id=uuid.uuid4(),
        occurred_at=_time(9, 10), payload=None,
    )
    outcome_event = SimpleNamespace(
        id=uuid.uuid4(), task_id=child_task_id, parent_task_id=task_id, event_type="result_recorded",
        previous_status="running", status="completed", source="teammate", actor_user_id=None,
        occurred_at=_time(9, 11), payload={
            "outcome": "success", "summary": "Implemented the requested change.",
            "result_sha256": "a" * 64, "attempt_count": 1,
        },
    )
    orchestration_events = [
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant_id, task_id=task_id, execution_id=execution_id,
            entity_type="handoff", entity_id=handoff_id, event_type="created", previous_status=None,
            status="waiting_approval", source="user_request", actor_user_id=uuid.uuid4(), payload=None,
            occurred_at=_time(9, 21),
        ),
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant_id, task_id=task_id, execution_id=execution_id,
            entity_type="approval", entity_id=approval.id, event_type="status_changed", previous_status="pending",
            status="approved", source="user_request", actor_user_id=uuid.uuid4(), payload=None,
            occurred_at=_time(9, 26),
        ),
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant_id, task_id=task_id, execution_id=execution_id,
            entity_type="acceptance_criterion", entity_id=uuid.uuid4(), event_type="created", previous_status=None,
            status="pending", source="api", actor_user_id=None,
            payload={"text": "Build succeeds", "decision_note": None}, occurred_at=_time(9, 28),
        ),
        SimpleNamespace(
            id=uuid.uuid4(), tenant_id=tenant_id, task_id=task_id, execution_id=execution_id,
            entity_type="dependency", entity_id=uuid.uuid4(), event_type="created", previous_status=None,
            status="active", source="database", actor_user_id=None,
            payload={"depends_on_task_id": str(child_task_id)}, occurred_at=_time(9, 29),
        ),
    ]
    session = _ActivitySession(
        [
            [child_task_id], [], [invocation], [run], attempt_events, [delivery], [handoff], [outcome_event, task_event],
            orchestration_events, [usage], [approval],
        ]
    )
    monkeypatch.setattr(verification, "_owned_task", AsyncMock(return_value=task))
    monkeypatch.setattr(
        verification,
        "_refresh_task_pull_request_statuses",
        AsyncMock(
            return_value={
                delivery_id: {
                    "availability": "current",
                    "number": 42,
                    "state": "open",
                    "draft": True,
                    "merged": False,
                    "review_status": {
                        "state": "review_required",
                        "approvals": 0,
                        "changes_requested": 0,
                        "requested_reviewers": ["reviewer"],
                        "reviewer_decisions": [],
                        "history_truncated": False,
                    },
                }
            }
        ),
    )

    feed = await verification.list_task_activity(execution_id, task_id, session)  # type: ignore[arg-type]

    usage_query = next(statement for statement in session.statements if "token_usage" in str(statement))
    query_parameters = usage_query.compile().params
    task_filter = next(value for key, value in query_parameters.items() if key.startswith("task_id"))
    assert set(task_filter) == {task_id, child_task_id}

    assert feed["count"] == 14
    assert feed["truncated"] is False
    assert [event["type"] for event in feed["activity"]] == [
        "tool", "verification", "verification_attempt", "verification_attempt",
        "delivery", "handoff", "orchestration_transition", "orchestration_transition",
        "orchestration_transition", "approval", "orchestration_transition", "task_outcome", "task_lifecycle", "llm_usage",
    ]
    delivery_event = feed["activity"][4]
    assert delivery_event["details"]["pull_request_status"]["review_status"]["state"] == "review_required"
    assert feed["activity"][9]["details"]["decision_note"] == ""
    assert feed["activity"][8]["details"]["previous_status"] == "pending"
    assert feed["activity"][8]["status"] == "approved"
    assert feed["activity"][6]["summary"] == "Task dependency created as active"
    assert feed["activity"][6]["details"]["payload"]["depends_on_task_id"] == str(child_task_id)
    assert feed["activity"][7]["summary"] == "Acceptance criterion created as pending"
    assert feed["activity"][2]["details"]["attempt_number"] == 1
    assert feed["activity"][2]["status"] == "passed"
    assert feed["activity"][-3]["status"] == "completed"
    assert str(child_task_id)[:8] in feed["activity"][-3]["summary"]
    assert feed["activity"][-3]["details"]["task_id"] == str(child_task_id)
    outcome_activity = next(event for event in feed["activity"] if event["type"] == "task_outcome")
    assert outcome_activity["summary"] == "Child task " + str(child_task_id)[:8] + ": Implemented the requested change."
    assert outcome_activity["details"]["result_sha256"] == "a" * 64
    assert outcome_activity["details"]["attempt_count"] == 1
    assert feed["activity"][-1]["details"] == {
        "task_id": str(child_task_id), "agent_type": "software-engineer", "provider": "openrouter",
        "model": "openai/gpt-4o-mini", "prompt_tokens": 120, "completion_tokens": 80,
        "total_tokens": 200, "cost": 0.000066,
    }


@pytest.mark.asyncio
async def test_task_activity_tree_bounds_wide_and_deep_trees() -> None:
    from solvyn.api.v1.verification import (
        _ACTIVITY_TASK_TREE_DEPTH_LIMIT,
        _ACTIVITY_TASK_TREE_LIMIT,
        _activity_task_tree_ids,
    )

    root_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    task = SimpleNamespace(id=root_id, tenant_id=tenant_id, execution_id=uuid.uuid4())
    wide_children = [uuid.uuid4() for _ in range(_ACTIVITY_TASK_TREE_LIMIT + 1)]
    wide_ids, wide_truncated = await _activity_task_tree_ids(_ActivitySession([wide_children]), task)  # type: ignore[arg-type]
    assert len(wide_ids) == _ACTIVITY_TASK_TREE_LIMIT
    assert wide_ids[0] == root_id
    assert wide_truncated is True

    deep_levels = [[uuid.uuid4()] for _ in range(_ACTIVITY_TASK_TREE_DEPTH_LIMIT + 1)]
    deep_ids, deep_truncated = await _activity_task_tree_ids(
        _ActivitySession(deep_levels), task  # type: ignore[arg-type]
    )
    assert len(deep_ids) == _ACTIVITY_TASK_TREE_DEPTH_LIMIT + 1
    assert deep_truncated is True


@pytest.mark.asyncio
async def test_task_activity_does_not_query_records_when_task_is_not_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.api.v1 import verification

    async def deny_task(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPException(status_code=404, detail="Workflow task not found")

    monkeypatch.setattr(verification, "_owned_task", deny_task)
    session = AsyncMock()

    with pytest.raises(HTTPException) as exc:
        await verification.list_task_activity(uuid.uuid4(), uuid.uuid4(), session)

    assert exc.value.status_code == 404
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_pull_request_refresh_is_deduplicated_bounded_and_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.api.v1 import verification
    from solvyn.multi_tenant.middleware import tenant_context

    tenant_id, task_id, repository_id, project_id = (uuid.uuid4() for _ in range(4))
    task = SimpleNamespace(id=task_id, tenant_id=tenant_id)
    workspace = SimpleNamespace(id=uuid.uuid4(), repository_id=repository_id)
    handoff = SimpleNamespace(project_id=project_id)
    project = SimpleNamespace(id=project_id, tenant_id=tenant_id, organization_id=uuid.uuid4(), is_active=True)
    organization = SimpleNamespace(tenant_id=tenant_id, is_active=True)
    repository = SimpleNamespace(
        id=repository_id, project_id=project_id, provider="github", full_name="octo/repo", is_active=True
    )

    class _Provider:
        def __init__(self) -> None:
            self.requested: list[int] = []

        async def __aenter__(self) -> _Provider:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def pull_request_status(self, _full_name: str, number: int) -> dict[str, Any]:
            self.requested.append(number)
            if number == 2:
                raise GitHubRepositoryError(503, "temporary failure")
            return {
                "title": f"PR {number}",
                "state": "open",
                "draft": False,
                "merged": False,
                "review_status": {"state": "no_reviews"},
            }

    provider = _Provider()
    session = _ActivitySession([[workspace], [handoff]])

    async def get_record(model: Any, _record_id: uuid.UUID) -> Any:
        if model is verification.Project:
            return project
        if model is verification.Repository:
            return repository
        return organization

    monkeypatch.setattr(verification, "get_authorized_repository_provider", AsyncMock(return_value=provider))
    monkeypatch.setattr(session, "get", get_record, raising=False)
    tenant_context.clear()
    deliveries = [
        SimpleNamespace(
            id=uuid.uuid4(), workspace_id=workspace.id, repository_id=repository_id, pull_request_number=number
        )
        for number in (1, 2, 1, 3, 4, 5, 6)
    ]

    try:
        statuses = await verification._refresh_task_pull_request_statuses(session, task, deliveries)  # type: ignore[arg-type]
    finally:
        tenant_context.clear()

    assert provider.requested == [1, 2, 3, 4, 5]
    assert statuses[deliveries[0].id]["availability"] == "current"
    assert statuses[deliveries[2].id] == statuses[deliveries[0].id]
    assert statuses[deliveries[1].id] == {"availability": "unavailable", "reason": "github_unavailable"}
    assert statuses[deliveries[6].id] == {"availability": "not_refreshed", "reason": "refresh_limit"}
