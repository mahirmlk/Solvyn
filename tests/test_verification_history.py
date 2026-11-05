from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

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
