from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from solvyn.db.models import VerificationRun


def test_overlapping_verification_runs_are_inconclusive() -> None:
    from solvyn.api.v1.verification import _verification_history

    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    task_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    first = VerificationRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_id=task_id,
        workspace_id=workspace_id,
        kind="test",
        command='["pytest", "-q"]',
        status="passed",
        runner_version="docker-sandbox/1",
        source_digest="a" * 64,
        started_at=now,
        completed_at=now + timedelta(seconds=10),
    )
    second = VerificationRun(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        task_id=task_id,
        workspace_id=workspace_id,
        kind="test",
        command='["pytest", "-q"]',
        status="failed",
        runner_version="docker-sandbox/1",
        source_digest="b" * 64,
        started_at=now + timedelta(seconds=5),
        completed_at=now + timedelta(seconds=15),
    )

    history = _verification_history([first, second])

    assert [item["regression_status"] for item in history] == ["inconclusive", "inconclusive"]
    assert history[1]["compared_to_run_id"] == str(first.id)
    assert history[1]["source_changed"] is True


@pytest.mark.asyncio
async def test_owned_task_binds_resolved_shared_secret_tenant_before_query(monkeypatch) -> None:
    from solvyn.api.v1 import verification
    from solvyn.db.models import Task

    tenant_id, task_id, execution_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    events: list[str] = []
    task = Task(id=task_id, tenant_id=tenant_id, execution_id=execution_id, title="scoped")
    result = SimpleNamespace(scalar_one_or_none=lambda: task)

    async def bind_scope(_db, resolved_tenant_id):
        assert resolved_tenant_id == tenant_id
        events.append("bind")

    async def execute(_query):
        events.append("query")
        return result

    db = SimpleNamespace(execute=execute)
    monkeypatch.setattr(verification, "_tenant_ref", lambda: "configured-default")
    monkeypatch.setattr(verification.workflow_executor, "_resolve_tenant_uuid", AsyncMock(return_value=tenant_id))
    monkeypatch.setattr(verification, "bind_tenant_scope", bind_scope)

    owned = await verification._owned_task(execution_id, task_id, db)

    assert owned is task
    assert events == ["bind", "query", "bind"]
