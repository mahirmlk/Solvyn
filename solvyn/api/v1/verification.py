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

from solvyn.config import settings
from solvyn.db.models import Task, TaskWorkspace, VerificationRun
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
    command: list[str] = Field(min_length=1, max_length=128)


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
    if any(not part or "\x00" in part for part in payload.command):
        raise HTTPException(status_code=422, detail="Command arguments must be non-empty strings without NUL bytes")
    if any(len(part.encode("utf-8")) > 4096 for part in payload.command) or sum(len(part.encode("utf-8")) for part in payload.command) > 16_384:
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
        command=json.dumps(payload.command),
        status="running",
        runner_version="docker-sandbox/1",
    )
    db.add(run)
    await db.commit()
    try:
        result: SandboxResult = await DockerSandboxRunner(
            _workspace_manager(), image=settings.sandbox_image
        ).run(workspace, payload.command)
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
