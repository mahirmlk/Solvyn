from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from solvyn.db.models import Task
from solvyn.workflows.task_completion import refresh_task_completion_state


def _scalar_rows(values: list[object]) -> SimpleNamespace:
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: values))


def _rows(values: list[object]) -> SimpleNamespace:
    return SimpleNamespace(all=lambda: values)


def _scalar(value: object | None) -> SimpleNamespace:
    return SimpleNamespace(scalar_one_or_none=lambda: value)


def _task(*, criteria_defined: bool, status: str = "completed") -> Task:
    return Task(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        title="Completion gate test",
        status=status,
        acceptance_criteria_defined=criteria_defined,
        completed_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("criterion_status", ["pending", "rejected"])
async def test_unaccepted_criteria_keep_task_in_review(criterion_status: str) -> None:
    task = _task(criteria_defined=True)
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar(task),
        _scalar(None),
        _rows([(criterion_status, None)]),
        _scalar_rows([]),
    ]

    status = await refresh_task_completion_state(db, task)

    assert status == "review_required"
    assert task.completed_at is None


@pytest.mark.asyncio
async def test_tasks_without_declared_gates_keep_existing_completion_behavior() -> None:
    task = _task(criteria_defined=False)
    db = AsyncMock()
    db.execute.side_effect = [_scalar(task), _scalar(None), _scalar_rows([])]

    status = await refresh_task_completion_state(db, task)

    assert status == "completed"
    assert task.completed_at is not None
    assert db.execute.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_status", "current_digest", "expected_status"),
    [
        ("failed", "workspace-digest", "review_required"),
        ("passed", "changed-workspace-digest", "review_required"),
        ("passed", "workspace-digest", "completed"),
    ],
)
async def test_required_verification_must_pass_for_the_current_workspace(
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
    current_digest: str,
    expected_status: str,
) -> None:
    from solvyn.workflows import task_completion

    task = _task(criteria_defined=True, status="review_required")
    run = SimpleNamespace(
        kind="test",
        status=run_status,
        source_digest="workspace-digest",
    )
    workspace = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar(task),
        _scalar(workspace),
        _scalar("completed"),
        _rows([("accepted", "workspace-digest")]),
        _scalar_rows([run]),
    ]

    class FakeWorkspaceManager:
        def __init__(self, root: Path) -> None:
            assert root == Path(task_completion.settings.sandbox_workspace_root)

        def get(self, workspace_id: uuid.UUID) -> object:
            assert workspace_id == workspace.id
            return workspace

        def fingerprint(self, _workspace: object) -> str:
            return current_digest

    monkeypatch.setattr(task_completion, "WorkspaceManager", FakeWorkspaceManager)

    status = await refresh_task_completion_state(db, task)

    assert status == expected_status
    if expected_status == "completed":
        assert task.completed_at is not None
    else:
        assert task.completed_at is None


@pytest.mark.asyncio
async def test_accepted_criteria_must_be_reapproved_after_workspace_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solvyn.workflows import task_completion

    task = _task(criteria_defined=True, status="review_required")
    workspace = SimpleNamespace(id=uuid.uuid4())
    run = SimpleNamespace(kind="test", status="passed", source_digest="new-digest")
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar(task),
        _scalar(workspace),
        _scalar("completed"),
        _rows([("accepted", "old-digest")]),
        _scalar_rows([run]),
    ]

    class FakeWorkspaceManager:
        def __init__(self, _root: Path) -> None:
            pass

        def get(self, _workspace_id: uuid.UUID) -> object:
            return workspace

        def fingerprint(self, _workspace: object) -> str:
            return "new-digest"

    monkeypatch.setattr(task_completion, "WorkspaceManager", FakeWorkspaceManager)

    status = await refresh_task_completion_state(db, task)

    assert status == "review_required"
    assert task.completed_at is None


@pytest.mark.asyncio
async def test_manual_workspace_without_handoff_can_complete_on_accepted_criteria(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solvyn.workflows import task_completion

    task = _task(criteria_defined=True, status="review_required")
    workspace = SimpleNamespace(id=uuid.uuid4())
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar(task),
        _scalar(workspace),
        _scalar(None),
        _rows([("accepted", "manual-digest")]),
        _scalar_rows([]),
    ]

    class FakeWorkspaceManager:
        def __init__(self, _root: Path) -> None:
            pass

        def get(self, _workspace_id: uuid.UUID) -> object:
            return workspace

        def fingerprint(self, _workspace: object) -> str:
            return "manual-digest"

    monkeypatch.setattr(task_completion, "WorkspaceManager", FakeWorkspaceManager)

    status = await refresh_task_completion_state(db, task)

    assert status == "completed"
    assert task.completed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handoff_status", "expected_status"),
    [
        ("pending", "review_required"),
        ("running", "review_required"),
        ("failed", "review_required"),
        ("completed", "completed"),
    ],
)
async def test_repository_workspace_requires_completed_handoff_even_with_passed_verification(
    monkeypatch: pytest.MonkeyPatch,
    handoff_status: str,
    expected_status: str,
) -> None:
    from solvyn.workflows import task_completion

    task = _task(criteria_defined=True, status="review_required")
    workspace = SimpleNamespace(id=uuid.uuid4())
    run = SimpleNamespace(kind="test", status="passed", source_digest="workspace-digest")
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar(task),
        _scalar(workspace),
        _scalar(handoff_status),
        _rows([("accepted", "workspace-digest")]),
        _scalar_rows([run]),
    ]

    class FakeWorkspaceManager:
        def __init__(self, _root: Path) -> None:
            pass

        def get(self, _workspace_id: uuid.UUID) -> object:
            return workspace

        def fingerprint(self, _workspace: object) -> str:
            return "workspace-digest"

    monkeypatch.setattr(task_completion, "WorkspaceManager", FakeWorkspaceManager)

    status = await refresh_task_completion_state(db, task)

    assert status == expected_status
    if expected_status == "completed":
        assert task.completed_at is not None
    else:
        assert task.completed_at is None


@pytest.mark.asyncio
async def test_workspace_fingerprint_failure_never_satisfies_acceptance_or_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from solvyn.sandbox.workspaces import WorkspaceError
    from solvyn.workflows import task_completion

    task = _task(criteria_defined=True, status="review_required")
    workspace = SimpleNamespace(id=uuid.uuid4())
    run = SimpleNamespace(kind="test", status="passed", source_digest=None)
    db = AsyncMock()
    db.execute.side_effect = [
        _scalar(task),
        _scalar(workspace),
        _scalar("completed"),
        _rows([("accepted", None)]),
        _scalar_rows([run]),
    ]

    class BrokenWorkspaceManager:
        def __init__(self, _root: Path) -> None:
            pass

        def get(self, _workspace_id: uuid.UUID) -> object:
            return workspace

        def fingerprint(self, _workspace: object) -> str:
            raise WorkspaceError("workspace unavailable")

    monkeypatch.setattr(task_completion, "WorkspaceManager", BrokenWorkspaceManager)

    status = await refresh_task_completion_state(db, task)

    assert status == "review_required"
    assert task.completed_at is None
