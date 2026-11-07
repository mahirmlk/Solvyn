"""Tests for recurring maintenance trigger configuration and dispatch."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1.maintenance import (
    MaintenanceScheduleCreate,
    create_maintenance_schedule,
    list_maintenance_schedules,
    list_maintenance_task_targets,
)
from solvyn.db.models import Approval, MaintenanceSchedule, ProactiveEvent, VerificationRun
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
    project = SimpleNamespace(id=project_id, tenant_id=uuid.uuid4())

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
async def test_code_quality_schedule_requires_a_task_workspace() -> None:
    project = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())

    class StubDB:
        async def execute(self, _query: object) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: project)

    with (
        patch("solvyn.api.v1.maintenance.require_org_membership", new_callable=AsyncMock),
        pytest.raises(HTTPException) as exc,
    ):
        await create_maintenance_schedule(
            uuid.uuid4(),
            MaintenanceScheduleCreate(
                project_id=project.id,
                maintenance_type="code_quality",
                title="Lint",
                interval_days=7,
            ),
            db=cast(AsyncSession, StubDB()),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_schedule_list_reports_read_only_membership() -> None:
    organization_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    schedule = MaintenanceSchedule(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        organization_id=organization_id,
        project_id=uuid.uuid4(),
        maintenance_type="code_quality",
        title="Weekly lint",
        interval_days=7,
        next_run_at=datetime.now(UTC),
        enabled=True,
    )

    class Results:
        def all(self) -> list[tuple[MaintenanceSchedule, str]]:
            return [(schedule, "Lint task")]

    with patch(
        "solvyn.api.v1.maintenance.require_org_membership",
        new_callable=AsyncMock,
        return_value=SimpleNamespace(role="member"),
    ):
        response = await list_maintenance_schedules(organization_id, db=cast(AsyncSession, SimpleNamespace(execute=AsyncMock(return_value=Results()))))

    assert response["can_manage"] is False
    assert response["count"] == 1
    assert response["schedules"][0]["id"] == str(schedule.id)  # type: ignore[index]
    assert response["schedules"][0]["task_title"] == "Lint task"  # type: ignore[index]


@pytest.mark.asyncio
async def test_maintenance_task_targets_are_project_scoped() -> None:
    organization_id, project_id = uuid.uuid4(), uuid.uuid4()
    project = SimpleNamespace(id=project_id, tenant_id=uuid.uuid4())
    target = (uuid.uuid4(), uuid.uuid4(), "Lint backend", "completed")

    class Results:
        def __init__(self, *, project_result: bool) -> None:
            self.project_result = project_result

        def scalar_one_or_none(self) -> object | None:
            return project if self.project_result else None

        def all(self) -> list[tuple[uuid.UUID, uuid.UUID, str, str]]:
            return [target]

    db = SimpleNamespace(execute=AsyncMock(side_effect=[Results(project_result=True), Results(project_result=False)]))
    with patch("solvyn.api.v1.maintenance.require_org_membership", new_callable=AsyncMock):
        response = await list_maintenance_task_targets(
            organization_id,
            project_id,
            db=cast(AsyncSession, db),
        )

    assert response == {
        "tasks": [{"id": str(target[0]), "execution_id": str(target[1]), "title": target[2], "status": target[3]}],
        "count": 1,
    }


@pytest.mark.asyncio
async def test_due_maintenance_schedule_adds_one_human_triage_event_and_advances() -> None:
    now = datetime.now(UTC)
    tenant_id = uuid.uuid4()
    schedule = MaintenanceSchedule(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
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
            self.tenant_id = tenant_id

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


@pytest.mark.asyncio
async def test_due_code_quality_schedule_requests_real_approval_bound_lint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solvyn.api.v1 import verification

    now = datetime.now(UTC)
    tenant_id, organization_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task_id, workspace_id, teammate_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scheduled_for = now - timedelta(minutes=5)
    schedule = MaintenanceSchedule(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        organization_id=organization_id,
        project_id=project_id,
        task_id=task_id,
        maintenance_type="code_quality",
        title="Weekly lint",
        interval_days=7,
        next_run_at=scheduled_for,
        enabled=True,
    )
    task = SimpleNamespace(id=task_id, tenant_id=tenant_id, execution_id=uuid.uuid4(), title="Existing task")
    workspace = SimpleNamespace(id=workspace_id)
    handoff = SimpleNamespace(teammate_id=teammate_id)
    teammate = SimpleNamespace(id=teammate_id)
    digest = "a" * 64

    class Results:
        def __init__(self, *, values: list[object] | None = None, row: object | None = None) -> None:
            self.values = values or []
            self.row = row

        def scalars(self) -> Results:
            return self

        def all(self) -> list[object]:
            return self.values

        def first(self) -> object | None:
            return self.row

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.added: list[object] = []
            self.commits = 0

        async def execute(self, _query: object) -> Results:
            self.execute_count += 1
            if self.execute_count == 1:
                return Results(values=[tenant_id])
            if self.execute_count == 2:
                return Results(values=[schedule])
            return Results(row=(task, workspace, handoff, teammate))

        def add(self, item: object) -> None:
            self.added.append(item)

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            self.commits += 1

    class WorkspaceManager:
        def get(self, _workspace_id: uuid.UUID) -> object:
            return object()

        def fingerprint(self, _workspace: object) -> str:
            return digest

    stub_db = StubDB()
    monkeypatch.setattr(verification, "_workspace_manager", lambda: WorkspaceManager())
    with patch("solvyn.workflows.scheduler.bind_tenant_scope", new_callable=AsyncMock):
        count = await _queue_due_maintenance_events(cast(AsyncSession, stub_db))

    runs = [item for item in stub_db.added if isinstance(item, VerificationRun)]
    approvals = [item for item in stub_db.added if isinstance(item, Approval)]
    events = [item for item in stub_db.added if isinstance(item, ProactiveEvent)]
    assert count == 1
    assert len(runs) == len(approvals) == len(events) == 1
    run = runs[0]
    assert run.status == "pending_approval"
    assert run.kind == "lint"
    assert json.loads(run.command) == ["ruff", "check", "."]
    assert run.source_digest == digest
    assert run.maintenance_schedule_id == schedule.id
    assert run.maintenance_scheduled_for == scheduled_for
    approval = approvals[0]
    assert approval.verification_run_id == run.id
    assert approval.status == "pending"
    assert json.loads(approval.payload)["source_digest"] == digest
    assert events[0].conclusion == "awaiting_approval"
    assert events[0].action == "approval_requested"
    assert schedule.next_run_at > now
