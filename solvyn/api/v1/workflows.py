"""Workflow discovery, inspection, and execution endpoints.

Workflows are *declarative* — they live as templates in
``solvyn.workflows.templates`` and are executed by ``workflow_executor``.
Each workflow is a directed graph of agent steps with explicit
``depends_on`` and ``parallel_with`` declarations; the executor resolves
the graph at runtime.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.registry import agent_registry
from solvyn.api_metadata import COMMON_RESPONSES
from solvyn.config import settings
from solvyn.db.models import ExecutionGraph as ExecutionGraphModel
from solvyn.db.models import Session, Task, TaskDependency
from solvyn.db.session import get_session
from solvyn.workflows.executor import workflow_executor
from solvyn.workflows.task_generator import TaskGenerator
from solvyn.workflows.templates import WORKFLOW_TEMPLATES

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _workflow_tenant_ref() -> str:
    from solvyn.multi_tenant.middleware import SHARED_SECRET_PRINCIPAL_ID, tenant_context

    if tenant_context.tenant_id is not None:
        return str(tenant_context.tenant_id)
    if tenant_context.authenticated_tenant_id == SHARED_SECRET_PRINCIPAL_ID and tenant_context.user_id is None:
        return str(settings.default_tenant_id)
    raise HTTPException(status_code=403, detail="A tenant membership is required for workflow access")


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
    if task_ids:
        dependencies = (
            await db.execute(
                select(TaskDependency).where(
                    TaskDependency.tenant_id == tenant_id,
                    TaskDependency.task_id.in_(task_ids),
                )
            )
        ).scalars().all()
    depends_on: dict[uuid.UUID, list[str]] = {}
    for dependency in dependencies:
        depends_on.setdefault(dependency.task_id, []).append(str(dependency.depends_on_task_id))

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
            }
            for task in tasks
        ],
    }


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
