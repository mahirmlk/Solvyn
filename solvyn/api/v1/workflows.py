import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from solvyn.agents.registry import agent_registry
from solvyn.config import settings
from solvyn.workflows.executor import workflow_executor
from solvyn.workflows.task_generator import TaskGenerator
from solvyn.workflows.templates import WORKFLOW_TEMPLATES

router = APIRouter(prefix="/workflows", tags=["workflows"])


@router.get("/")
async def list_workflows() -> dict[str, Any]:
    return {"workflows": [{"name": name, "description": tmpl.description, "steps": len(tmpl.steps)} for name, tmpl in WORKFLOW_TEMPLATES.items()]}


@router.get("/agents")
async def list_agents() -> dict[str, Any]:
    agents_data = agent_registry.list_agents()
    return {
        "agents": [
            {"type": at, "name": m.name, "description": m.description, "capabilities": [c.value for c in m.capabilities]} for at, m in agents_data.items()
        ]
    }


@router.get("/{name}")
async def get_workflow(name: str) -> dict[str, Any]:
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


@router.post("/{name}/run")
async def run_workflow(name: str, inputs: dict[str, Any] = Body(...)) -> dict[str, Any]:
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


class GenerateTasksRequest(BaseModel):
    specification: str


@router.post("/generate-tasks")
async def generate_tasks(request: GenerateTasksRequest) -> dict[str, Any]:
    generator = TaskGenerator()
    result = await generator.generate_from_spec(request.specification)
    graph = generator.build_workflow_graph(result["tasks"])
    return {"tasks": result["tasks"], "graph": graph}
