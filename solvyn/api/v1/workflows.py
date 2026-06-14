"""Workflow discovery, inspection, and execution endpoints.

Workflows are *declarative* — they live as templates in
``solvyn.workflows.templates`` and are executed by ``workflow_executor``.
Each workflow is a directed graph of agent steps with explicit
``depends_on`` and ``parallel_with`` declarations; the executor resolves
the graph at runtime.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from solvyn.agents.registry import agent_registry
from solvyn.api_metadata import COMMON_RESPONSES
from solvyn.config import settings
from solvyn.workflows.executor import workflow_executor
from solvyn.workflows.task_generator import TaskGenerator
from solvyn.workflows.templates import WORKFLOW_TEMPLATES

router = APIRouter(prefix="/workflows", tags=["workflows"])


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
        "step; long-running workflows should be polled via the session and "
        "trace endpoints rather than waiting on this response."
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

    from solvyn.multi_tenant.middleware import tenant_context

    session_id = str(uuid.uuid4())
    tenant_id = str(tenant_context.tenant_id) if tenant_context.tenant_id else settings.default_tenant_id
    safe_inputs = {k: v for k, v in inputs.items() if k != "tenant_id"}
    result = await workflow_executor.run(
        template_name=name,
        inputs=safe_inputs,
        session_id=session_id,
        tenant_id=tenant_id,
    )

    return {
        "workflow": name,
        "session_id": result.session_id,
        "status": result.status,
        "nodes": [{"agent_type": n.agent_type, "status": n.status, "error": n.error} for n in result.nodes],
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
