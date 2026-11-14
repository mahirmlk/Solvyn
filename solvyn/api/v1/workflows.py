"""Workflow discovery, inspection, and execution endpoints.

Workflows are *declarative* — they live as templates in
``solvyn.workflows.templates`` and are executed by ``workflow_executor``.
Each workflow is a directed graph of agent steps with explicit
``depends_on`` and ``parallel_with`` declarations; the executor resolves
the graph at runtime.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.registry import agent_registry
from solvyn.api_metadata import COMMON_RESPONSES
from solvyn.config import settings
from solvyn.db.models import (
    Approval,
    Project,
    Session,
    Task,
    TaskAcceptanceCriterion,
    TaskDependency,
    TaskHandoff,
    TaskWorkspace,
    TeammateProfile,
    TeammateProjectAssignment,
    VerificationRun,
)
from solvyn.db.models import (
    ExecutionGraph as ExecutionGraphModel,
)
from solvyn.db.session import async_session_factory, bind_tenant_scope, get_session
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.proactive import task_assignment_event
from solvyn.task_results import result_metadata
from solvyn.workflows.executor import workflow_executor
from solvyn.workflows.task_completion import refresh_task_completion_state
from solvyn.workflows.task_generator import TaskGenerator
from solvyn.workflows.templates import WORKFLOW_TEMPLATES

router = APIRouter(prefix="/workflows", tags=["workflows"])
_MAX_HANDOFF_DEPTH = 3
_MAX_HANDOFF_INPUT_BYTES = 24_000
_HANDOFF_LEASE_DURATION = timedelta(seconds=60)
_HANDOFF_HEARTBEAT_INTERVAL = 20


class HandoffConflictError(RuntimeError):
    """Raised when a handoff cannot be claimed by this request."""


class HandoffLeaseLostError(RuntimeError):
    """Raised when a handoff worker loses its durable lease."""


def _workflow_tenant_ref() -> str:
    from solvyn.multi_tenant.middleware import SHARED_SECRET_PRINCIPAL_ID, tenant_context

    if tenant_context.tenant_id is not None:
        return str(tenant_context.tenant_id)
    if tenant_context.authenticated_tenant_id == SHARED_SECRET_PRINCIPAL_ID and tenant_context.user_id is None:
        return str(settings.default_tenant_id)
    raise HTTPException(status_code=403, detail="A tenant membership is required for workflow access")


async def _handoff_heartbeat(handoff_id: uuid.UUID, lease_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
    while True:
        await asyncio.sleep(_HANDOFF_HEARTBEAT_INTERVAL)
        async with async_session_factory() as db:
            result = await db.execute(
                update(TaskHandoff)
                .where(
                    TaskHandoff.id == handoff_id,
                    TaskHandoff.tenant_id == tenant_id,
                    TaskHandoff.status == "running",
                    TaskHandoff.lease_owner == lease_id,
                )
                .values(lease_expires_at=func.now() + _HANDOFF_LEASE_DURATION)
                .returning(TaskHandoff.id)
            )
            await db.commit()
            if result.scalar_one_or_none() != handoff_id:
                return False


async def _run_teammate_with_handoff_lease(
    handoff_id: uuid.UUID,
    lease_id: uuid.UUID,
    tenant_id: uuid.UUID,
    execute: Coroutine[Any, Any, dict[str, Any]],
) -> dict[str, Any]:
    execution_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(execute)
    heartbeat_task: asyncio.Task[bool] = asyncio.create_task(_handoff_heartbeat(handoff_id, lease_id, tenant_id))
    try:
        done, _ = await asyncio.wait({execution_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
        if execution_task in done:
            return await execution_task
        if not heartbeat_task.result():
            raise HandoffLeaseLostError("Task handoff lease was lost")
        return await execution_task
    finally:
        if not execution_task.done():
            execution_task.cancel()
        if not heartbeat_task.done():
            heartbeat_task.cancel()
        await asyncio.gather(execution_task, heartbeat_task, return_exceptions=True)


async def _execute_task_handoff(
    db: AsyncSession,
    *,
    handoff: TaskHandoff,
    child: Task,
    teammate: TeammateProfile,
    tenant_id: uuid.UUID,
    input_data: dict[str, Any],
    session_id: str | None,
) -> dict[str, Any]:
    await bind_tenant_scope(db, tenant_id)
    workspace_record = (
        await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.tenant_id == tenant_id,
                TaskWorkspace.task_id == child.id,
            )
        )
    ).scalar_one_or_none()
    repository_id = workspace_record.repository_id if workspace_record is not None else None
    repository_revision = workspace_record.source_commit_sha if workspace_record is not None else None
    if repository_id is not None and not repository_revision:
        raise HTTPException(status_code=409, detail="Task workspace has no pinned source commit")
    repository_paths: list[str] | None = None
    if workspace_record is not None and workspace_record.source_paths is not None:
        try:
            decoded_paths = json.loads(workspace_record.source_paths)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid") from exc
        if not isinstance(decoded_paths, list) or any(not isinstance(path, str) for path in decoded_paths):
            raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid")
        repository_paths = decoded_paths

    lease_id = uuid.uuid4()
    can_claim = or_(
        TaskHandoff.status.in_(["pending", "failed"]),
        and_(
            TaskHandoff.status == "running",
            or_(TaskHandoff.lease_expires_at.is_(None), TaskHandoff.lease_expires_at < func.now()),
        ),
    )
    claimed = await db.execute(
        update(TaskHandoff)
        .where(TaskHandoff.id == handoff.id, TaskHandoff.tenant_id == tenant_id, can_claim)
        .values(
            status="running",
            error=None,
            completed_at=None,
            lease_owner=lease_id,
            lease_expires_at=func.now() + _HANDOFF_LEASE_DURATION,
        )
        .returning(TaskHandoff.id)
    )
    if claimed.scalar_one_or_none() != handoff.id:
        await db.rollback()
        raise HandoffConflictError("Task handoff is already running or has completed")

    await db.execute(
        update(Task)
        .where(Task.id == child.id, Task.tenant_id == tenant_id)
        .values(status="running", error=None, attempt_count=Task.attempt_count + 1, started_at=func.now())
    )
    await db.commit()

    from solvyn.agents.teammate_runtime import TeammateRunConflictError, run_teammate

    try:
        output = await _run_teammate_with_handoff_lease(
            handoff.id,
            lease_id,
            tenant_id,
            run_teammate(
                db,
                teammate,
                tenant_id=tenant_id,
                project_id=handoff.project_id,
                input_data=input_data,
                session_id=session_id,
                repository_id=repository_id,
                repository_paths=repository_paths,
                repository_revision=repository_revision,
                task_id=child.id,
                handoff_lease_id=lease_id,
            ),
        )
    except TeammateRunConflictError as exc:
        released = await db.execute(
            update(TaskHandoff)
            .where(
                TaskHandoff.id == handoff.id,
                TaskHandoff.tenant_id == tenant_id,
                or_(
                    TaskHandoff.lease_owner == lease_id,
                    and_(
                        TaskHandoff.status == "running",
                        or_(TaskHandoff.lease_owner.is_(None), TaskHandoff.lease_expires_at < func.now()),
                    ),
                ),
            )
            .values(status="pending", error=None, lease_owner=None, lease_expires_at=None)
            .returning(TaskHandoff.id)
        )
        if released.scalar_one_or_none() != handoff.id:
            await db.rollback()
            raise HandoffLeaseLostError("Task handoff lease was lost")
        await db.execute(
            update(Task)
            .where(Task.id == child.id, Task.tenant_id == tenant_id)
            .values(status="pending", error=None)
        )
        await db.commit()
        raise HandoffConflictError("Teammate is already running; retry this handoff") from exc
    except asyncio.CancelledError:
        released = await db.execute(
            update(TaskHandoff)
            .where(TaskHandoff.id == handoff.id, TaskHandoff.tenant_id == tenant_id, TaskHandoff.lease_owner == lease_id)
            .values(status="pending", lease_owner=None, lease_expires_at=None)
            .returning(TaskHandoff.id)
        )
        if released.scalar_one_or_none() == handoff.id:
            await db.execute(
                update(Task)
                .where(Task.id == child.id, Task.tenant_id == tenant_id)
                .values(status="pending")
            )
            await db.commit()
        else:
            await db.rollback()
        raise
    except HandoffLeaseLostError:
        raise
    except Exception as exc:
        failed = await db.execute(
            update(TaskHandoff)
            .where(TaskHandoff.id == handoff.id, TaskHandoff.tenant_id == tenant_id, TaskHandoff.lease_owner == lease_id)
            .values(status="failed", error=str(exc), lease_owner=None, lease_expires_at=None, completed_at=func.now())
            .returning(TaskHandoff.id)
        )
        if failed.scalar_one_or_none() != handoff.id:
            await db.rollback()
            raise HandoffLeaseLostError("Task handoff lease was lost") from exc
        await db.execute(
            update(Task)
            .where(Task.id == child.id, Task.tenant_id == tenant_id)
            .values(
                status="failed",
                error=str(exc),
                result_summary="The assigned teammate failed during execution.",
                result_sha256=None,
                completed_at=func.now(),
            )
        )
        await db.commit()
        return {"handoff_id": str(handoff.id), "task_id": str(child.id), "status": "failed", "error": str(exc)}

    finalized = await db.execute(
        update(TaskHandoff)
        .where(TaskHandoff.id == handoff.id, TaskHandoff.tenant_id == tenant_id, TaskHandoff.lease_owner == lease_id)
        .values(
            status="completed",
            output_data=json.dumps(output),
            error=None,
            lease_owner=None,
            lease_expires_at=None,
            completed_at=func.now(),
        )
        .returning(TaskHandoff.id)
    )
    if finalized.scalar_one_or_none() != handoff.id:
        await db.rollback()
        raise HandoffLeaseLostError("Task handoff lease was lost before saving its result")
    summary, result_sha256 = result_metadata(output)
    child.status = "completed"
    child.result = json.dumps(output)
    child.result_summary = summary
    child.result_sha256 = result_sha256
    child.error = None
    child.completed_at = datetime.now(UTC)
    await refresh_task_completion_state(db, child)
    await db.commit()
    return {"handoff_id": str(handoff.id), "task_id": str(child.id), "status": child.status, "output": output}


class GenerateTasksRequest(BaseModel):
    """Request payload for the task-generation endpoint.

    The endpoint accepts a free-form product specification (typically a few
    paragraphs of plain English describing the feature or change) and
    returns a structured list of tasks plus a workflow graph that can be
    executed by ``POST /workflows/{name}/run``.
    """

    specification: str = Field(
        ...,
        min_length=10,
        max_length=20000,
        description=(
            "Free-form product specification in plain English. Should be at "
            "least a few sentences long so the LLM has enough signal to "
            "decompose it into well-scoped tasks."
        ),
        examples=[
            "Build a multi-tenant API gateway that routes requests based on the "
            "X-Tenant-Slug header and enforces per-tenant rate limits.",
        ],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "specification": (
                    "Build a Slack bot that summarises a GitHub pull request, "
                    "posts the summary to a channel, and allows reviewers to "
                    "approve or reject via emoji reactions."
                ),
            }
        }
    )


class TaskHandoffRequest(BaseModel):
    idempotency_key: uuid.UUID
    target_teammate_id: uuid.UUID
    project_id: uuid.UUID
    reason: str = Field(min_length=1, max_length=2000)
    input_data: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False


class WorkflowScheduleRequest(BaseModel):
    scheduled_at: datetime
    inputs: dict[str, Any] = Field(default_factory=dict)


@router.get(
    "/",
    summary="List workflow templates",
    description=(
        "Return the metadata for every built-in workflow template. The "
        "response is intentionally lightweight so dashboards can render a "
        "catalog without pulling the full step graph for each entry."
    ),
    response_description="Array of workflow summaries.",
    responses=COMMON_RESPONSES,
)
async def list_workflows() -> dict[str, Any]:
    """Enumerate all available workflow templates."""
    return {"workflows": [{"name": name, "description": tmpl.description, "steps": len(tmpl.steps)} for name, tmpl in WORKFLOW_TEMPLATES.items()]}


@router.get(
    "/agents",
    summary="List available agent types",
    description=(
        "Return the metadata for every registered agent. This is the same "
        "data surfaced by the `solvyn agents` CLI command and is the source "
        "of truth for which agent types can appear in a workflow step."
    ),
    response_description="Array of agent metadata records.",
    responses=COMMON_RESPONSES,
)
async def list_agents() -> dict[str, Any]:
    """Enumerate every agent the Solvyn runtime knows about."""
    agents_data = agent_registry.list_agents()
    return {
        "agents": [
            {"type": at, "name": m.name, "description": m.description, "capabilities": [c.value for c in m.capabilities]} for at, m in agents_data.items()
        ]
    }


@router.get("/tasks", summary="List recent tenant-scoped workflow tasks")
async def list_recent_workflow_tasks(
    limit: int = Query(default=50, ge=1, le=100),
    status: str | None = Query(default=None, min_length=1, max_length=50),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_ref = _workflow_tenant_ref()
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(tenant_ref)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc
    await bind_tenant_scope(db, tenant_id)

    query = (
        select(Task)
        .join(ExecutionGraphModel, ExecutionGraphModel.id == Task.execution_id)
        .join(Session, Session.id == ExecutionGraphModel.session_id)
        .where(Task.tenant_id == tenant_id, Session.tenant_id == tenant_id)
    )
    if status is not None:
        query = query.where(Task.status == status)
    result = await db.execute(query.order_by(Task.created_at.desc(), Task.id).limit(limit))
    tasks = result.scalars().all()
    return {
        "tasks": [
            {
                "task_id": str(task.id),
                "execution_id": str(task.execution_id),
                "session_id": str(task.session_id) if task.session_id else None,
                "title": task.title,
                "description": task.description[:4_000] if task.description else "",
                "assigned_agent": task.assigned_agent,
                "status": task.status,
                "priority": task.priority,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
                "error": task.error[:4_000] if task.error else None,
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "updated_at": task.updated_at.isoformat() if task.updated_at else "",
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            }
            for task in tasks
        ],
        "count": len(tasks),
    }


@router.get(
    "/{name}",
    summary="Inspect a single workflow template",
    description=(
        "Return the full step graph for a workflow template, including the "
        "agent type, description, dependency and parallelism declarations, "
        "and the default input map. The step graph is what the executor "
        "walks at runtime to schedule agent invocations."
    ),
    response_description="The full workflow definition.",
    responses={
        404: {
            "description": "No workflow with the given name exists.",
            "content": {"application/json": {"example": {"detail": "Workflow 'foo' not found"}}},
        },
        **COMMON_RESPONSES,
    },
)
async def get_workflow(name: str) -> dict[str, Any]:
    """Return the full definition of a single workflow template."""
    tmpl = WORKFLOW_TEMPLATES.get(name)
    if tmpl is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    return {
        "name": tmpl.name,
        "description": tmpl.description,
        "steps": [
            {
                "agent_type": s.agent_type,
                "description": s.description,
                "depends_on": s.depends_on,
                "parallel_with": s.parallel_with,
            }
            for s in tmpl.steps
        ],
        "default_inputs": tmpl.default_inputs,
    }


@router.post(
    "/{name}/run",
    summary="Execute a workflow",
    description=(
        "Start an execution of the named workflow template. A new session is "
        "allocated for the run, scoped to the calling tenant. The endpoint "
        "returns the full execution result including the status of every "
        "step and a durable execution ID. Poll GET /workflows/executions/{id} "
        "to read progress or POST /workflows/executions/{id}/resume after an "
        "interruption."
    ),
    response_description="The execution result, including per-step status.",
    responses={
        404: {
            "description": "No workflow with the given name exists.",
            "content": {"application/json": {"example": {"detail": "Workflow 'foo' not found"}}},
        },
        **COMMON_RESPONSES,
    },
)
async def run_workflow(name: str, inputs: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Execute a workflow template with the given inputs."""
    tmpl = WORKFLOW_TEMPLATES.get(name)
    if tmpl is None:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")

    session_id = str(uuid.uuid4())
    tenant_id = _workflow_tenant_ref()
    safe_inputs = {k: v for k, v in inputs.items() if k != "tenant_id"}
    try:
        result = await workflow_executor.run(
            template_name=name,
            inputs=safe_inputs,
            session_id=session_id,
            tenant_id=tenant_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "workflow": name,
        "execution_id": result.id,
        "session_id": result.session_id,
        "status": result.status,
        "nodes": [{"agent_type": n.agent_type, "status": n.status, "error": n.error} for n in result.nodes],
    }


@router.post("/{name}/schedule", summary="Schedule a durable workflow execution")
async def schedule_workflow(name: str, payload: WorkflowScheduleRequest) -> dict[str, Any]:
    if name not in WORKFLOW_TEMPLATES:
        raise HTTPException(status_code=404, detail=f"Workflow '{name}' not found")
    tenant_id = _workflow_tenant_ref()
    safe_inputs = {key: value for key, value in payload.inputs.items() if key != "tenant_id"}
    try:
        result = await workflow_executor.schedule(
            template_name=name,
            inputs=safe_inputs,
            scheduled_at=payload.scheduled_at,
            tenant_id=tenant_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "workflow": name,
        "execution_id": result.id,
        "session_id": result.session_id,
        "status": result.status,
        "scheduled_at": payload.scheduled_at.isoformat(),
    }


@router.get("/executions/{execution_id}", summary="Get a workflow execution snapshot")
async def get_workflow_execution(
    execution_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_ref = _workflow_tenant_ref()
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(tenant_ref)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc
    await bind_tenant_scope(db, tenant_id)
    result = await db.execute(
        select(ExecutionGraphModel)
        .join(Session, Session.id == ExecutionGraphModel.session_id)
        .where(ExecutionGraphModel.id == execution_id, Session.tenant_id == tenant_id)
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Workflow execution not found")
    return {
        "execution_id": str(record.id),
        "session_id": str(record.session_id),
        "status": record.status,
        "scheduled_at": record.scheduled_at.isoformat() if record.scheduled_at else None,
        "snapshot": json.loads(record.graph_data),
        "updated_at": record.updated_at.isoformat() if record.updated_at else "",
    }


@router.post("/executions/{execution_id}/resume", summary="Resume an interrupted workflow execution")
async def resume_workflow_execution(execution_id: uuid.UUID) -> dict[str, Any]:
    tenant_ref = _workflow_tenant_ref()
    try:
        result = await workflow_executor.resume(execution_id, tenant_ref)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "execution_id": result.id,
        "session_id": result.session_id,
        "status": result.status,
        "recovery_policy": "at_least_once_for_unfinished_nodes",
        "nodes": [
            {"id": node.id, "agent_type": node.agent_type, "status": node.status, "error": node.error}
            for node in result.nodes
        ],
    }


@router.post("/executions/{execution_id}/cancel", summary="Cancel a workflow execution")
async def cancel_workflow_execution(execution_id: uuid.UUID) -> dict[str, str]:
    tenant_ref = _workflow_tenant_ref()
    try:
        return await workflow_executor.cancel(execution_id, tenant_ref)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/executions/{execution_id}/tasks", summary="List durable tasks for a workflow execution")
async def list_workflow_execution_tasks(
    execution_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_ref = _workflow_tenant_ref()
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(tenant_ref)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc
    await bind_tenant_scope(db, tenant_id)

    graph = (
        await db.execute(
            select(ExecutionGraphModel)
            .join(Session, Session.id == ExecutionGraphModel.session_id)
            .where(ExecutionGraphModel.id == execution_id, Session.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if graph is None:
        raise HTTPException(status_code=404, detail="Workflow execution not found")

    tasks: list[Task] = list(
        (
            await db.execute(
                select(Task)
                .where(Task.execution_id == execution_id, Task.tenant_id == tenant_id)
                .order_by(Task.created_at, Task.node_id)
            )
        )
        .scalars()
        .all()
    )
    task_ids = [task.id for task in tasks]
    dependencies: list[TaskDependency] = []
    verification_runs: list[VerificationRun] = []
    acceptance_criteria: list[TaskAcceptanceCriterion] = []
    if task_ids:
        dependencies = list(
            (
                await db.execute(
                    select(TaskDependency).where(
                        TaskDependency.tenant_id == tenant_id,
                        TaskDependency.task_id.in_(task_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        verification_runs = list(
            (
                await db.execute(
                    select(VerificationRun)
                    .where(VerificationRun.tenant_id == tenant_id, VerificationRun.task_id.in_(task_ids))
                    .order_by(VerificationRun.started_at, VerificationRun.id)
                )
            )
            .scalars()
            .all()
        )
        acceptance_criteria = list(
            (
                await db.execute(
                    select(TaskAcceptanceCriterion)
                    .where(TaskAcceptanceCriterion.tenant_id == tenant_id, TaskAcceptanceCriterion.task_id.in_(task_ids))
                    .order_by(TaskAcceptanceCriterion.created_at, TaskAcceptanceCriterion.id)
                )
            )
            .scalars()
            .all()
        )
    handoffs: list[TaskHandoff] = []
    approvals_by_handoff: dict[uuid.UUID, Approval] = {}
    if task_ids:
        handoffs = list(
            (
                await db.execute(
                    select(TaskHandoff)
                    .where(
                        TaskHandoff.tenant_id == tenant_id,
                        TaskHandoff.parent_task_id.in_(task_ids),
                    )
                    .order_by(TaskHandoff.created_at)
                )
            )
            .scalars()
            .all()
        )
        if handoffs:
            approvals = (
                await db.execute(
                    select(Approval).where(Approval.task_handoff_id.in_([handoff.id for handoff in handoffs]))
                )
            ).scalars().all()
            approvals_by_handoff = {approval.task_handoff_id: approval for approval in approvals if approval.task_handoff_id}
    depends_on: dict[uuid.UUID, list[str]] = {}
    for dependency in dependencies:
        depends_on.setdefault(dependency.task_id, []).append(str(dependency.depends_on_task_id))
    verification_by_task: dict[uuid.UUID, list[VerificationRun]] = {}
    for run in verification_runs:
        verification_by_task.setdefault(run.task_id, []).append(run)
    criteria_by_task: dict[uuid.UUID, list[TaskAcceptanceCriterion]] = {}
    for criterion in acceptance_criteria:
        criteria_by_task.setdefault(criterion.task_id, []).append(criterion)

    return {
        "execution_id": str(execution_id),
        "tasks": [
            {
                "task_id": str(task.id),
                "node_id": task.node_id,
                "title": task.title,
                "assigned_agent": task.assigned_agent,
                "status": task.status,
                "attempt_count": task.attempt_count,
                "max_attempts": task.max_attempts,
                "dependencies": depends_on.get(task.id, []),
                "input": json.loads(task.description) if task.description else {},
                "result": json.loads(task.result) if task.result else None,
                "error": task.error,
                "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                "acceptance_criteria_defined": task.acceptance_criteria_defined,
                "verification_runs": [
                    {
                        "id": str(run.id),
                        "kind": run.kind,
                        "status": run.status,
                        "required_for_completion": run.required_for_completion,
                        "exit_code": run.exit_code,
                        "timed_out": run.timed_out,
                        "output_truncated": run.output_truncated,
                        "runner_version": run.runner_version,
                        "source_digest": run.source_digest,
                        "started_at": run.started_at.isoformat() if run.started_at else None,
                        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                    }
                    for run in verification_by_task.get(task.id, [])
                ],
                "acceptance_criteria": [
                    {
                        "id": str(criterion.id),
                        "text": criterion.text,
                        "status": criterion.status,
                        "reviewer_user_id": str(criterion.reviewer_user_id) if criterion.reviewer_user_id else None,
                        "reviewer_organization_id": str(criterion.reviewer_organization_id) if criterion.reviewer_organization_id else None,
                        "decision_note": criterion.decision_note,
                        "decision_workspace_digest": criterion.decision_workspace_digest,
                        "created_at": criterion.created_at.isoformat() if criterion.created_at else None,
                        "decided_at": criterion.decided_at.isoformat() if criterion.decided_at else None,
                    }
                    for criterion in criteria_by_task.get(task.id, [])
                ],
            }
            for task in tasks
        ],
        "handoffs": [
            {
                "handoff_id": str(handoff.id),
                "idempotency_key": str(handoff.idempotency_key),
                "parent_task_id": str(handoff.parent_task_id),
                "child_task_id": str(handoff.child_task_id),
                "teammate_id": str(handoff.teammate_id),
                "project_id": str(handoff.project_id),
                "requires_approval": handoff.requires_approval,
                "approval_id": str(approvals_by_handoff[handoff.id].id) if handoff.id in approvals_by_handoff else None,
                "approval_status": approvals_by_handoff[handoff.id].status if handoff.id in approvals_by_handoff else None,
                "delegated_from": handoff.delegated_from,
                "delegated_to": handoff.delegated_to,
                "reason": handoff.reason,
                "input": json.loads(handoff.input_data),
                "output": json.loads(handoff.output_data) if handoff.output_data else None,
                "status": handoff.status,
                "error": handoff.error,
                "created_at": handoff.created_at.isoformat() if handoff.created_at else None,
                "completed_at": handoff.completed_at.isoformat() if handoff.completed_at else None,
            }
            for handoff in handoffs
        ],
    }


@router.post(
    "/executions/{execution_id}/tasks/{task_id}/handoffs",
    summary="Delegate a workflow task to an assigned teammate",
)
async def create_task_handoff(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskHandoffRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_ref = _workflow_tenant_ref()
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(tenant_ref)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc

    # Resolve the tenant before touching task rows. RLS protects handoffs and
    # their related task records, including compatibility credentials whose
    # tenant is not present in request context.
    await bind_tenant_scope(db, tenant_id)

    parent = (
        await db.execute(
            select(Task)
            .join(ExecutionGraphModel, ExecutionGraphModel.id == Task.execution_id)
            .join(Session, Session.id == ExecutionGraphModel.session_id)
            .where(
                Task.id == task_id,
                Task.execution_id == execution_id,
                Task.tenant_id == tenant_id,
                Session.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if parent is None:
        raise HTTPException(status_code=404, detail="Workflow task not found")
    input_json = json.dumps(payload.input_data)
    if len(input_json.encode("utf-8")) > _MAX_HANDOFF_INPUT_BYTES:
        raise HTTPException(status_code=413, detail="Handoff input exceeds the 24 KB limit")
    if not payload.reason.strip():
        raise HTTPException(status_code=400, detail="Handoff reason must not be blank")

    handoff = (
        await db.execute(
            select(TaskHandoff).where(
                TaskHandoff.tenant_id == tenant_id,
                TaskHandoff.parent_task_id == parent.id,
                TaskHandoff.idempotency_key == payload.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if handoff is not None and (
        handoff.teammate_id != payload.target_teammate_id
        or handoff.project_id != payload.project_id
        or handoff.reason != payload.reason
        or handoff.input_data != input_json
        or handoff.requires_approval != payload.requires_approval
    ):
        raise HTTPException(status_code=409, detail="Idempotency key was already used for a different handoff")
    if handoff is not None and handoff.status == "completed":
        child = (
            await db.execute(select(Task).where(Task.id == handoff.child_task_id, Task.tenant_id == tenant_id))
        ).scalar_one()
        return {
            "handoff_id": str(handoff.id),
            "task_id": str(child.id),
            "status": "completed",
            "output": json.loads(handoff.output_data) if handoff.output_data else None,
        }
    if handoff is not None and handoff.status == "waiting_approval":
        approval = (
            await db.execute(select(Approval).where(Approval.task_handoff_id == handoff.id))
        ).scalar_one_or_none()
        return {
            "handoff_id": str(handoff.id),
            "task_id": str(handoff.child_task_id),
            "status": "waiting_approval",
            "approval_id": str(approval.id) if approval else None,
            "approval_status": approval.status if approval else "pending",
        }
    if handoff is not None and handoff.status == "rejected":
        return {"handoff_id": str(handoff.id), "task_id": str(handoff.child_task_id), "status": "rejected"}

    depth = 0
    ancestor = parent
    while ancestor.parent_task_id is not None:
        depth += 1
        if depth >= _MAX_HANDOFF_DEPTH:
            raise HTTPException(status_code=409, detail="Maximum task delegation depth reached")
        parent_task = (
            await db.execute(
                select(Task).where(Task.id == ancestor.parent_task_id, Task.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()
        if parent_task is None:
            raise HTTPException(status_code=409, detail="Task delegation ancestry is incomplete")
        ancestor = parent_task

    teammate = (
        await db.execute(
            select(TeammateProfile)
            .join(TeammateProjectAssignment, TeammateProjectAssignment.teammate_id == TeammateProfile.id)
            .join(Project, Project.id == TeammateProjectAssignment.project_id)
            .where(
                TeammateProfile.id == payload.target_teammate_id,
                TeammateProfile.tenant_id == tenant_id,
                TeammateProfile.is_enabled.is_(True),
                Project.id == payload.project_id,
                Project.tenant_id == tenant_id,
                Project.organization_id == TeammateProfile.organization_id,
                Project.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if teammate is None:
        raise HTTPException(status_code=404, detail="Assigned teammate not found")

    from solvyn.agents.teammate_runtime import validate_role_agent_type

    try:
        validate_role_agent_type(teammate.role, teammate.agent_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if handoff is None:
        child = Task(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            session_id=parent.session_id,
            execution_id=execution_id,
            title=f"Delegated to {teammate.name}: {parent.title}",
            description=input_json,
            assigned_agent=teammate.agent_type,
            status="waiting_approval" if payload.requires_approval else "pending",
            priority=parent.priority,
            parent_task_id=parent.id,
        )
        handoff = TaskHandoff(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            parent_task_id=parent.id,
            child_task_id=child.id,
            idempotency_key=payload.idempotency_key,
            teammate_id=teammate.id,
            project_id=payload.project_id,
            delegated_from=parent.assigned_agent or "workflow",
            delegated_to=teammate.agent_type,
            reason=payload.reason,
            input_data=input_json,
            status="waiting_approval" if payload.requires_approval else "pending",
            requires_approval=payload.requires_approval,
        )
        db.add_all([child, handoff])
        try:
            if payload.requires_approval:
                await db.flush()
                project = await db.get(Project, payload.project_id)
                if project is None or project.tenant_id != tenant_id:
                    raise HTTPException(status_code=404, detail="Assigned project not found")
                db.add(
                    Approval(
                        organization_id=project.organization_id,
                        project_id=project.id,
                        teammate_id=teammate.id,
                        task_handoff_id=handoff.id,
                        kind="plan",
                        summary=payload.reason,
                        payload=json.dumps(
                            {
                                "task_id": str(child.id),
                                "handoff_id": str(handoff.id),
                                "delegated_to": teammate.agent_type,
                                "input": payload.input_data,
                            }
                        ),
                        requested_by=parent.assigned_agent or "workflow",
                    )
                )
            else:
                db.add(
                    task_assignment_event(
                        organization_id=teammate.organization_id,
                        project_id=payload.project_id,
                        handoff=handoff,
                        task=child,
                    )
                )
            await db.commit()
        except IntegrityError:
            await db.rollback()
            await bind_tenant_scope(db, tenant_id)
            handoff = (
                await db.execute(
                    select(TaskHandoff).where(
                        TaskHandoff.tenant_id == tenant_id,
                        TaskHandoff.parent_task_id == parent.id,
                        TaskHandoff.idempotency_key == payload.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if handoff is None:
                raise
            if (
                handoff.teammate_id != payload.target_teammate_id
                or handoff.project_id != payload.project_id
                or handoff.reason != payload.reason
                or handoff.input_data != input_json
                or handoff.requires_approval != payload.requires_approval
            ):
                raise HTTPException(status_code=409, detail="Idempotency key was already used for a different handoff")
            teammate = (
                await db.execute(
                    select(TeammateProfile).where(
                        TeammateProfile.id == handoff.teammate_id,
                        TeammateProfile.tenant_id == tenant_id,
                        TeammateProfile.is_enabled.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if teammate is None:
                raise HTTPException(status_code=409, detail="Assigned teammate is disabled")
            child = (
                await db.execute(select(Task).where(Task.id == handoff.child_task_id, Task.tenant_id == tenant_id))
            ).scalar_one()
    else:
        child = (
            await db.execute(select(Task).where(Task.id == handoff.child_task_id, Task.tenant_id == tenant_id))
        ).scalar_one()

    if handoff.status == "waiting_approval":
        approval = (
            await db.execute(select(Approval).where(Approval.task_handoff_id == handoff.id))
        ).scalar_one_or_none()
        return {
            "handoff_id": str(handoff.id),
            "task_id": str(child.id),
            "status": "waiting_approval",
            "approval_id": str(approval.id) if approval else None,
            "approval_status": approval.status if approval else "pending",
        }

    try:
        await bind_tenant_scope(db, tenant_id)
        with tenant_context.scoped_tenant(tenant_id):
            return await _execute_task_handoff(
                db,
                handoff=handoff,
                child=child,
                teammate=teammate,
                tenant_id=tenant_id,
                input_data=payload.input_data,
                session_id=str(parent.session_id) if parent.session_id else None,
            )
    except HandoffConflictError as exc:
        # _execute_task_handoff rolls back on a lease conflict, clearing the
        # transaction-local tenant setting before the replay lookup.
        await bind_tenant_scope(db, tenant_id)
        current = (
            await db.execute(
                select(TaskHandoff)
                .where(TaskHandoff.id == handoff.id, TaskHandoff.tenant_id == tenant_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if current is not None and current.status == "completed":
            return {
                "handoff_id": str(current.id),
                "task_id": str(current.child_task_id),
                "status": "completed",
                "output": json.loads(current.output_data) if current.output_data else None,
            }
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HandoffLeaseLostError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/executions/{execution_id}/tasks/{task_id}/handoffs/{handoff_id}/resume",
    summary="Resume a pending or abandoned teammate handoff",
)
async def resume_task_handoff(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    handoff_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    tenant_ref = _workflow_tenant_ref()
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(tenant_ref)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc

    await bind_tenant_scope(db, tenant_id)

    result = await db.execute(
        select(TaskHandoff, Task, TeammateProfile)
        .join(Task, Task.id == TaskHandoff.child_task_id)
        .join(ExecutionGraphModel, ExecutionGraphModel.id == Task.execution_id)
        .join(Session, Session.id == ExecutionGraphModel.session_id)
        .join(TeammateProfile, TeammateProfile.id == TaskHandoff.teammate_id)
        .where(
            TaskHandoff.id == handoff_id,
            TaskHandoff.tenant_id == tenant_id,
            TaskHandoff.parent_task_id == task_id,
            TaskHandoff.child_task_id == Task.id,
            Task.parent_task_id == task_id,
            Task.execution_id == execution_id,
            Task.tenant_id == tenant_id,
            Session.tenant_id == tenant_id,
        )
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Workflow task handoff not found")
    handoff, child, teammate = row
    if not teammate.is_enabled:
        raise HTTPException(status_code=409, detail="Assigned teammate is disabled")

    try:
        await bind_tenant_scope(db, tenant_id)
        with tenant_context.scoped_tenant(tenant_id):
            return await _execute_task_handoff(
                db,
                handoff=handoff,
                child=child,
                teammate=teammate,
                tenant_id=tenant_id,
                input_data=json.loads(handoff.input_data),
                session_id=str(child.session_id) if child.session_id else None,
            )
    except HandoffConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HandoffLeaseLostError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/generate-tasks",
    summary="Generate a task list from a specification",
    description=(
        "Use the configured LLM to decompose a free-form product "
        "specification into a structured list of tasks and a workflow graph "
        "that the executor can run. The endpoint is the bridge between "
        "ambiguous, human-written requirements and the deterministic, "
        "machine-executable workflow engine."
    ),
    response_description="The generated tasks and the proposed workflow graph.",
    responses=COMMON_RESPONSES,
)
async def generate_tasks(request: GenerateTasksRequest) -> dict[str, Any]:
    """Decompose a product spec into tasks and a workflow graph."""
    generator = TaskGenerator()
    result = await generator.generate_from_spec(request.specification)
    graph = generator.build_workflow_graph(result["tasks"])
    return {"tasks": result["tasks"], "graph": graph}
