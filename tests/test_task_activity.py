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


class _ActivitySession:
    def __init__(self, results: list[list[Any]]) -> None:
        self.results = iter(results)

    async def execute(self, _statement: Any) -> _Rows:
        return _Rows(next(self.results))


def _time(hour: int, minute: int) -> datetime:
    return datetime(2026, 11, 6, hour, minute, tzinfo=UTC)


@pytest.mark.asyncio
async def test_task_activity_combines_durable_records_in_reverse_chronological_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.api.v1 import verification

    task_id, tenant_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    handoff_id, run_id, delivery_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
        id=uuid.uuid4(), status="succeeded", tool_name="workspace_read_file", path="src/main.py",
        error_code=None, started_at=_time(10, 0), completed_at=_time(10, 1),
    )
    run = SimpleNamespace(
        id=run_id, kind="test", status="passed", exit_code=0, timed_out=False,
        started_at=_time(9, 45), completed_at=_time(9, 50),
    )
    delivery = SimpleNamespace(
        id=delivery_id, status="pull_request_open", updated_at=_time(9, 42), created_at=_time(9, 40),
        pull_request_title="Implement feature", branch_name="solvyn/task-42", pull_request_number=42,
    )
    handoff = SimpleNamespace(
        id=handoff_id, parent_task_id=uuid.uuid4(), child_task_id=task_id, status="completed",
        completed_at=_time(9, 30), created_at=_time(9, 20), delegated_to="software_engineer", reason="Implement feature",
    )
    approval = SimpleNamespace(
        id=uuid.uuid4(), kind="verification", status="pending", decided_at=None,
        updated_at=_time(9, 25), created_at=_time(9, 25), summary="Run the test profile", decision_note=None,
    )
    session = _ActivitySession([[invocation], [run], [delivery], [handoff], [approval]])
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

    assert feed["count"] == 6
    assert feed["truncated"] is False
    assert [event["type"] for event in feed["activity"]] == [
        "tool", "verification", "delivery", "handoff", "approval", "task",
    ]
    delivery_event = feed["activity"][2]
    assert delivery_event["details"]["pull_request_status"]["review_status"]["state"] == "review_required"
    assert feed["activity"][4]["details"]["decision_note"] == ""
    assert feed["activity"][-1]["status"] == "completed"


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
    project = SimpleNamespace(id=project_id, tenant_id=tenant_id, organization_id=uuid.uuid4())
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
