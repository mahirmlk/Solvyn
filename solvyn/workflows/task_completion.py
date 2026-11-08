"""Completion gates for tasks with declared acceptance and verification requirements."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.config import settings
from solvyn.db.models import Task, TaskAcceptanceCriterion, TaskHandoff, TaskWorkspace, VerificationRun
from solvyn.sandbox.workspaces import WorkspaceError, WorkspaceManager


async def refresh_task_completion_state(db: AsyncSession, task: Task) -> str:
    """Keep completed tasks in review until declared criteria and checks pass."""
    locked_task = (
        await db.execute(
            select(Task)
            .where(Task.tenant_id == task.tenant_id, Task.id == task.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_task is None:
        return task.status
    task = locked_task
    if task.status not in {"completed", "review_required"}:
        return task.status

    workspace = (
        await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.tenant_id == task.tenant_id,
                TaskWorkspace.task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    has_handoff = False
    if workspace is not None:
        has_handoff = (
            await db.execute(
                select(TaskHandoff.id).where(
                    TaskHandoff.tenant_id == task.tenant_id,
                    TaskHandoff.child_task_id == task.id,
                )
            )
        ).scalar_one_or_none() is not None
    current_digest: str | None = None
    if workspace is not None:
        try:
            manager = WorkspaceManager(Path(settings.sandbox_workspace_root))
            current_digest = manager.fingerprint(manager.get(workspace.id))
        except (OSError, WorkspaceError, ValueError):
            pass

    criteria_pass = not task.acceptance_criteria_defined and workspace is None
    if task.acceptance_criteria_defined or workspace is not None:
        criteria = (
            await db.execute(
                select(TaskAcceptanceCriterion.status, TaskAcceptanceCriterion.decision_workspace_digest).where(
                    TaskAcceptanceCriterion.tenant_id == task.tenant_id,
                    TaskAcceptanceCriterion.task_id == task.id,
                )
            )
        ).all()
        criteria_pass = bool(criteria) and (workspace is None or current_digest is not None) and all(
            status == "accepted" and (workspace is None or digest == current_digest)
            for status, digest in criteria
        )

    required_runs = (
        await db.execute(
            select(VerificationRun)
            .where(
                VerificationRun.tenant_id == task.tenant_id,
                VerificationRun.task_id == task.id,
                VerificationRun.required_for_completion.is_(True),
            )
            .order_by(VerificationRun.kind, VerificationRun.started_at.desc(), VerificationRun.id.desc())
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    latest_by_kind: dict[str, VerificationRun] = {}
    for run in required_runs:
        latest_by_kind.setdefault(run.kind, run)

    verification_pass = not latest_by_kind and (workspace is None or not has_handoff)
    if workspace is not None:
        verification_pass = not has_handoff and not latest_by_kind
        if latest_by_kind and current_digest is not None:
            verification_pass = all(
                run.status == "passed" and run.source_digest == current_digest
                for run in latest_by_kind.values()
            )
    elif latest_by_kind:
        try:
            verification_pass = all(run.status == "passed" for run in latest_by_kind.values())
        except AttributeError:
            verification_pass = False

    if criteria_pass and verification_pass:
        if task.status == "review_required":
            task.status = "completed"
            task.completed_at = task.completed_at or datetime.now(UTC)
    else:
        task.status = "review_required"
        task.completed_at = None
    return task.status


def require_task_review(task: Task) -> None:
    """Reopen a completed task after its accepted output has changed or gained gates."""
    if task.status == "completed":
        task.status = "review_required"
        task.completed_at = None
