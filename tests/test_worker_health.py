"""Operator health endpoint for persisted workflow workers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest


class _ScalarRows:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def scalars(self) -> _ScalarRows:
        return self

    def all(self) -> list[Any]:
        return self.rows


class _WorkerSession:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    async def __aenter__(self) -> _WorkerSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: Any) -> _ScalarRows:
        return _ScalarRows(self.rows)


@pytest.mark.asyncio
async def test_worker_health_reports_current_stale_and_stopped_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.api.v1 import health

    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            worker_id=uuid.uuid4(),
            worker_name="workflow-scheduler",
            host_name="worker-a",
            status="running",
            started_at=now - timedelta(minutes=3),
            last_seen_at=now - timedelta(seconds=4),
        ),
        SimpleNamespace(
            worker_id=uuid.uuid4(),
            worker_name="workflow-scheduler",
            host_name="worker-b",
            status="running",
            started_at=now - timedelta(minutes=10),
            last_seen_at=now - timedelta(seconds=45),
        ),
        SimpleNamespace(
            worker_id=uuid.uuid4(),
            worker_name="workflow-scheduler",
            host_name="worker-c",
            status="stopped",
            started_at=now - timedelta(minutes=8),
            last_seen_at=now - timedelta(minutes=1),
        ),
    ]
    monkeypatch.setattr(health, "async_session_factory", lambda: _WorkerSession(rows))

    result = await health.worker_health_check()

    assert result["status"] == "healthy"
    assert result["healthy_count"] == 1
    assert [worker["status"] for worker in result["workers"]] == ["running", "stale", "stopped"]


@pytest.mark.asyncio
async def test_worker_health_without_heartbeat_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.api.v1 import health

    monkeypatch.setattr(health, "async_session_factory", lambda: _WorkerSession([]))

    result = await health.worker_health_check()

    assert result == {"status": "unhealthy", "healthy_count": 0, "worker_count": 0, "workers": []}
