"""Tenant-scoped task workspaces and durable sandbox verification results."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import OWNER_ADMIN
from solvyn.config import settings
from solvyn.db.models import Organization, OrganizationMembership, Task, TaskAcceptanceCriterion, TaskWorkspace, User, VerificationRun
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import SHARED_SECRET_PRINCIPAL_ID, tenant_context
from solvyn.sandbox.runtime import DockerSandboxRunner, SandboxResult, SandboxRuntimeError
from solvyn.sandbox.workspaces import WorkspaceError, WorkspaceManager
from solvyn.workflows.executor import workflow_executor

router = APIRouter(prefix="/workflows/executions/{execution_id}/tasks/{task_id}", tags=["verification"])
_MAX_INITIAL_FILES_BYTES = 16 * 1024 * 1024
_MAX_STORED_OUTPUT_CHARS = 256 * 1024
VerificationKind = Literal["test", "build", "lint", "typecheck", "security"]


class TaskWorkspaceCreate(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)


class VerificationRequest(BaseModel):
    kind: VerificationKind
    command: list[str] | None = Field(default=None, min_length=1, max_length=128)


class AcceptanceCriteriaCreate(BaseModel):
    criteria: list[str] = Field(min_length=1, max_length=30)


class AcceptanceDecision(BaseModel):
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


_DEFAULT_VERIFICATION_COMMANDS: dict[VerificationKind, list[str]] = {
    "test": ["pytest", "-q"],
    "build": ["python", "-m", "compileall", "-q", "."],
    "lint": ["ruff", "check", "."],
    "typecheck": ["mypy", "."],
    "security": ["ruff", "check", "--select", "S", "."],
}


def _workspace_manager() -> WorkspaceManager:
    return WorkspaceManager(Path(settings.sandbox_workspace_root))


def _tenant_ref() -> str:
    if tenant_context.tenant_id is not None:
        return str(tenant_context.tenant_id)
    if tenant_context.authenticated_tenant_id == SHARED_SECRET_PRINCIPAL_ID and tenant_context.user_id is None:
        return settings.default_tenant_id
    raise HTTPException(status_code=403, detail="A tenant membership is required for verification access")


async def _owned_task(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession,
) -> Task:
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(_tenant_ref())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc
    task = (
        await db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.execution_id == execution_id,
                Task.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Workflow task not found")
    return task


def _verification_dict(run: VerificationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "task_id": str(run.task_id),
        "execution_id": str(run.execution_id) if run.execution_id else None,
        "workspace_id": str(run.workspace_id),
        "kind": run.kind,
        "command": json.loads(run.command),
        "status": run.status,
        "exit_code": run.exit_code,
        "timed_out": run.timed_out,
        "output_truncated": run.output_truncated,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "error": run.error,
        "duration_seconds": float(run.duration_seconds) if run.duration_seconds is not None else None,
        "runner_version": run.runner_version,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def _criterion_dict(criterion: TaskAcceptanceCriterion) -> dict[str, Any]:
    return {
        "id": str(criterion.id),
        "text": criterion.text,
        "status": criterion.status,
        "reviewer_user_id": str(criterion.reviewer_user_id) if criterion.reviewer_user_id else None,
        "reviewer_organization_id": str(criterion.reviewer_organization_id) if criterion.reviewer_organization_id else None,
        "decision_note": criterion.decision_note,
        "created_at": criterion.created_at.isoformat() if criterion.created_at else None,
        "decided_at": criterion.decided_at.isoformat() if criterion.decided_at else None,
    }


async def _require_acceptance_reviewer(db: AsyncSession, task: Task) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = tenant_context.user_id
    if not tenant_context.is_authenticated or user_id is None:
        raise HTTPException(status_code=401, detail="A signed-in organization owner or admin must review acceptance criteria")
    membership = (
        await db.execute(
            select(OrganizationMembership.organization_id)
            .join(Organization, Organization.id == OrganizationMembership.organization_id)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.role.in_(OWNER_ADMIN),
                Organization.tenant_id == task.tenant_id,
                Organization.is_active.is_(True),
                User.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="An active organization owner or admin membership is required")
    return user_id, membership


async def _finish_run(db: AsyncSession, run: VerificationRun, values: dict[str, Any]) -> dict[str, Any]:
    completed = await db.execute(
        update(VerificationRun)
        .where(VerificationRun.id == run.id, VerificationRun.tenant_id == run.tenant_id, VerificationRun.status == "running")
        .values(**values)
        .returning(VerificationRun.id)
    )
    if completed.scalar_one_or_none() is None:
        await db.refresh(run)
        return _verification_dict(run)
    await db.commit()
    await db.refresh(run)
    return _verification_dict(run)


@router.post("/workspace", status_code=201, summary="Create a managed task workspace")
async def create_task_workspace(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskWorkspaceCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    task = await _owned_task(execution_id, task_id, db)
    existing = (
        await db.execute(select(TaskWorkspace).where(TaskWorkspace.tenant_id == task.tenant_id, TaskWorkspace.task_id == task.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Task already has a managed workspace")

    total_bytes = sum(len(contents.encode("utf-8")) for contents in payload.files.values())
    if total_bytes > _MAX_INITIAL_FILES_BYTES:
        raise HTTPException(status_code=413, detail="Initial workspace files exceed 16 MiB")

    manager = _workspace_manager()
    workspace = manager.create()
    try:
        for relative_path, contents in payload.files.items():
            manager.write_file(workspace, relative_path, contents.encode("utf-8"))
        record = TaskWorkspace(id=workspace.id, tenant_id=task.tenant_id, task_id=task.id)
        db.add(record)
        await db.commit()
    except WorkspaceError as exc:
        manager.delete(workspace.id)
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        manager.delete(workspace.id)
        await db.rollback()
        raise
    return {"workspace_id": str(workspace.id), "task_id": str(task.id), "status": "ready"}


@router.post("/verification-runs", summary="Run and persist a sandbox verification check")
async def run_task_verification(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: VerificationRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    workspace_record = (
        await db.execute(select(TaskWorkspace).where(TaskWorkspace.tenant_id == task.tenant_id, TaskWorkspace.task_id == task.id))
    ).scalar_one_or_none()
    if workspace_record is None:
        raise HTTPException(status_code=409, detail="Create a task workspace before running verification")
    command = payload.command or _DEFAULT_VERIFICATION_COMMANDS[payload.kind]
    if any(not part or "\x00" in part for part in command):
        raise HTTPException(status_code=422, detail="Command arguments must be non-empty strings without NUL bytes")
    if any(len(part.encode("utf-8")) > 4096 for part in command) or sum(len(part.encode("utf-8")) for part in command) > 16_384:
        raise HTTPException(status_code=422, detail="Command arguments exceed the 16 KiB limit")

    try:
        workspace = _workspace_manager().get(workspace_record.id)
    except WorkspaceError as exc:
        raise HTTPException(status_code=409, detail="Task workspace is missing or invalid") from exc

    run = VerificationRun(
        tenant_id=task.tenant_id,
        task_id=task.id,
        execution_id=execution_id,
        workspace_id=workspace_record.id,
        kind=payload.kind,
        command=json.dumps(command),
        status="running",
        runner_version="docker-sandbox/1",
    )
    db.add(run)
    await db.commit()
    try:
        result: SandboxResult = await DockerSandboxRunner(
            _workspace_manager(), image=settings.sandbox_image
        ).run(workspace, command)
    except (SandboxRuntimeError, WorkspaceError, ValueError) as exc:
        return await _finish_run(
            db,
            run,
            {
                "status": "failed",
                "error": str(exc)[:_MAX_STORED_OUTPUT_CHARS],
                "completed_at": datetime.now(UTC),
            },
        )

    return await _finish_run(
        db,
        run,
        {
            "status": "timed_out" if result.timed_out else ("passed" if result.exit_code == 0 else "failed"),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
            "stdout": result.stdout[:_MAX_STORED_OUTPUT_CHARS],
            "stderr": result.stderr[:_MAX_STORED_OUTPUT_CHARS],
            "duration_seconds": result.duration_seconds,
            "completed_at": datetime.now(UTC),
        },
    )


@router.get("/verification-runs", summary="List task verification evidence")
async def list_task_verification_runs(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    runs = (
        await db.execute(
            select(VerificationRun)
            .where(VerificationRun.tenant_id == task.tenant_id, VerificationRun.task_id == task.id)
            .order_by(VerificationRun.started_at, VerificationRun.id)
        )
    ).scalars().all()
    return {"task_id": str(task.id), "verification_runs": [_verification_dict(run) for run in runs]}


@router.post("/acceptance-criteria", status_code=201, summary="Add human-reviewable acceptance criteria")
async def create_task_acceptance_criteria(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AcceptanceCriteriaCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    criteria_text = [criterion.strip() for criterion in payload.criteria]
    if any(not criterion or len(criterion) > 2000 for criterion in criteria_text):
        raise HTTPException(status_code=422, detail="Acceptance criteria must contain 1 to 2000 non-whitespace characters")
    if len({criterion.casefold() for criterion in criteria_text}) != len(criteria_text):
        raise HTTPException(status_code=422, detail="Acceptance criteria must be unique")
    claimed = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.tenant_id == task.tenant_id, Task.acceptance_criteria_defined.is_(False))
        .values(acceptance_criteria_defined=True)
        .returning(Task.id)
    )
    if claimed.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Acceptance criteria are already defined for this task")

    records = [TaskAcceptanceCriterion(tenant_id=task.tenant_id, task_id=task.id, text=criterion) for criterion in criteria_text]
    db.add_all(records)
    await db.commit()
    return {"task_id": str(task.id), "acceptance_criteria": [_criterion_dict(record) for record in records]}


@router.get("/acceptance-criteria", summary="Get task acceptance criteria and review decisions")
async def list_task_acceptance_criteria(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    criteria = (
        await db.execute(
            select(TaskAcceptanceCriterion)
            .where(TaskAcceptanceCriterion.tenant_id == task.tenant_id, TaskAcceptanceCriterion.task_id == task.id)
            .order_by(TaskAcceptanceCriterion.created_at, TaskAcceptanceCriterion.id)
        )
    ).scalars().all()
    return {"task_id": str(task.id), "acceptance_criteria": [_criterion_dict(record) for record in criteria]}


@router.post("/acceptance-criteria/{criterion_id}/decision", summary="Record an owner/admin acceptance decision")
async def decide_task_acceptance_criterion(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    criterion_id: uuid.UUID,
    payload: AcceptanceDecision,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    reviewer_user_id, reviewer_organization_id = await _require_acceptance_reviewer(db, task)
    decided = await db.execute(
        update(TaskAcceptanceCriterion)
        .where(
            TaskAcceptanceCriterion.id == criterion_id,
            TaskAcceptanceCriterion.tenant_id == task.tenant_id,
            TaskAcceptanceCriterion.task_id == task.id,
            TaskAcceptanceCriterion.status == "pending",
        )
        .values(
            status=payload.decision,
            reviewer_user_id=reviewer_user_id,
            reviewer_organization_id=reviewer_organization_id,
            decision_note=payload.note,
            decided_at=datetime.now(UTC),
        )
        .returning(TaskAcceptanceCriterion.id)
    )
    if decided.scalar_one_or_none() is None:
        existing_status = (
            await db.execute(
                select(TaskAcceptanceCriterion.status).where(
                    TaskAcceptanceCriterion.id == criterion_id,
                    TaskAcceptanceCriterion.tenant_id == task.tenant_id,
                    TaskAcceptanceCriterion.task_id == task.id,
                )
            )
        ).scalar_one_or_none()
        await db.rollback()
        if existing_status is None:
            raise HTTPException(status_code=404, detail="Acceptance criterion not found")
        raise HTTPException(status_code=409, detail=f"Acceptance criterion is already {existing_status}")

    await db.commit()
    record = (
        await db.execute(
            select(TaskAcceptanceCriterion).where(
                TaskAcceptanceCriterion.id == criterion_id,
                TaskAcceptanceCriterion.tenant_id == task.tenant_id,
                TaskAcceptanceCriterion.task_id == task.id,
            )
        )
    ).scalar_one()
    return _criterion_dict(record)
