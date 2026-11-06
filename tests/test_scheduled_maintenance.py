"""Tests for recurring maintenance trigger configuration and dispatch."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1.maintenance import MaintenanceScheduleCreate, create_maintenance_schedule
from solvyn.db.models import MaintenanceSchedule, ProactiveEvent
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.workflows.scheduler import _queue_due_maintenance_events


def test_schedule_interval_is_limited_to_one_through_thirty_days() -> None:
    with pytest.raises(ValidationError):
        MaintenanceScheduleCreate(
            project_id=uuid.uuid4(),
            maintenance_type="code_quality",
            title="Quality check",
            interval_days=0,
        )
    valid = MaintenanceScheduleCreate(
        project_id=uuid.uuid4(),
        maintenance_type="dependency_review",
        title="Dependencies",
        interval_days=30,
    )
    assert valid.interval_days == 30


@pytest.mark.asyncio
async def test_maintenance_schedule_create_requires_admin_and_project_scope() -> None:
    organization_id, project_id, user_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project = SimpleNamespace(id=project_id)

    class StubDB:
        def __init__(self) -> None:
            self.added: MaintenanceSchedule | None = None

        async def execute(self, _query: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: project)

        async def scalar(self, _query: object) -> int:
            return 0

        def add(self, item: MaintenanceSchedule) -> None:
            self.added = item

        async def commit(self) -> None:
            return None

        async def refresh(self, item: MaintenanceSchedule) -> None:
            item.id = uuid.uuid4()

    stub_db = StubDB()
    user_id_token = tenant_context._user_id.set(user_id)
    with (
        patch("solvyn.api.v1.maintenance.require_org_membership", new_callable=AsyncMock) as membership,
    ):
        try:
            result = await create_maintenance_schedule(
                organization_id,
                MaintenanceScheduleCreate(
                    project_id=project_id,
                    maintenance_type="documentation_review",
                    title=" Docs review ",
                    interval_days=7,
                ),
                db=cast(AsyncSession, stub_db),
            )
        finally:
            tenant_context._user_id.reset(user_id_token)

    membership.assert_awaited_once_with(cast(AsyncSession, stub_db), organization_id, roles=("owner", "admin"))
    assert stub_db.added is not None
    assert stub_db.added.created_by == user_id
    assert stub_db.added.title == "Docs review"
    assert stub_db.added.next_run_at > datetime.now(UTC)
    assert result["enabled"] is True


@pytest.mark.asyncio
async def test_maintenance_schedule_requires_a_nonempty_title() -> None:
    with (
        patch("solvyn.api.v1.maintenance.require_org_membership", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc,
    ):
        await create_maintenance_schedule(
            uuid.uuid4(),
            MaintenanceScheduleCreate(
                project_id=uuid.uuid4(),
                maintenance_type="code_quality",
                title="   ",
                interval_days=1,
            ),
            db=cast(AsyncSession, SimpleNamespace()),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_due_maintenance_schedule_adds_one_human_triage_event_and_advances() -> None:
    now = datetime.now(UTC)
    schedule = MaintenanceSchedule(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        maintenance_type="code_quality",
        title="Monthly backend review",
        interval_days=14,
        next_run_at=now - timedelta(days=1),
        enabled=True,
    )

    class Results:
        def scalars(self) -> Results:
            return self

        def all(self) -> list[MaintenanceSchedule]:
            return [schedule]

    class StubDB:
        def __init__(self) -> None:
            self.events: list[ProactiveEvent] = []
            self.commits = 0
            self.execute_count = 0
            self.tenant_id = uuid.uuid4()

        async def execute(self, _query: object) -> Results:
            self.execute_count += 1
            if self.execute_count == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.tenant_id]))
            return Results()

        def add(self, event: ProactiveEvent) -> None:
            self.events.append(event)

        async def commit(self) -> None:
            self.commits += 1

    stub_db = StubDB()
    with patch("solvyn.workflows.scheduler.bind_tenant_scope", new_callable=AsyncMock) as bind_scope:
        count = await _queue_due_maintenance_events(cast(AsyncSession, stub_db))
    bind_scope.assert_awaited_once_with(cast(AsyncSession, stub_db), stub_db.tenant_id)

    assert count == 1
    assert stub_db.commits == 1
    assert len(stub_db.events) == 1
    event = stub_db.events[0]
    assert event.event_type == "scheduled_maintenance"
    assert event.action == "due"
    assert event.status == "pending"
    assert "Monthly backend review" in event.workflow_name
    assert event.source_event_id.startswith(f"maintenance:{schedule.id}:")
    assert schedule.next_run_at > now
