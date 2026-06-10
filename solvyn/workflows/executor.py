import json
import logging
import uuid
from typing import Any

from solvyn.agents.graph import AgentGraph
from solvyn.agents.schema import ExecutionGraph
from solvyn.db.models import ExecutionGraph as ExecutionGraphModel
from solvyn.db.session import async_session_factory
from solvyn.workflows.templates import WORKFLOW_TEMPLATES, WorkflowStep

logger = logging.getLogger(__name__)


class WorkflowExecutor:
    async def run(
        self,
        template_name: str,
        inputs: dict[str, Any],
        session_id: str | None = None,
        tenant_id: str = "default",
    ) -> ExecutionGraph:
        template = WORKFLOW_TEMPLATES.get(template_name)
        if template is None:
            raise ValueError(f"Unknown workflow: {template_name}. Available: {list(WORKFLOW_TEMPLATES.keys())}")

        resolved_session_id = session_id or str(uuid.uuid4())
        merged_inputs = {**template.default_inputs, **inputs}

        graph = AgentGraph()
        node_map: dict[str, str] = {}
        latest_by_agent_type: dict[str, str] = {}

        for i, step in enumerate(template.steps):
            step_key = f"{step.agent_type}_{i}"
            step_input = self._resolve_inputs(step, merged_inputs, node_map, {})
            node_id = graph.add_node(agent_type=step.agent_type, input_data=step_input)
            node_map[step_key] = node_id
            latest_by_agent_type[step.agent_type] = node_id

            if step.depends_on:
                for dep in step.depends_on:
                    dep_node_id = latest_by_agent_type.get(dep)
                    if dep_node_id:
                        graph.add_edge(source=dep_node_id, target=node_id)

        logger.info(f"Starting workflow '{template_name}' ({resolved_session_id}) with {len(template.steps)} steps")

        result = await graph.execute(
            session_id=resolved_session_id,
            tenant_id=tenant_id,
            agent_configs={},
        )

        # Build runtime outputs from completed nodes and re-resolve inputs
        runtime_outputs: dict[str, Any] = {}
        for step_key, node_id in node_map.items():
            node = graph.nodes.get(node_id)
            if node and node.status == "completed" and node.output is not None:
                agent_type = step_key.rsplit("_", 1)[0]
                runtime_outputs[agent_type] = node.output

        # Update result nodes with fully-resolved inputs for persistence/inspection
        for i, step in enumerate(template.steps):
            step_key = f"{step.agent_type}_{i}"
            if step_key in node_map:
                resolved_input = self._resolve_inputs(step, merged_inputs, node_map, runtime_outputs)
                for node in result.nodes:
                    if node.id == node_map[step_key]:
                        node.input = resolved_input
                        break

        await self._persist_execution_graph(result, template_name, merged_inputs, tenant_id)
        logger.info(f"Workflow '{template_name}' completed: {result.status}")
        return result

    def _resolve_inputs(
        self,
        step: WorkflowStep,
        user_inputs: dict[str, Any],
        node_map: dict[str, str],
        runtime_outputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        resolved: dict[str, Any] = {}
        for key, value in step.input_map.items():
            if "," in value:
                parts = [v.strip() for v in value.split(",")]
                combined = []
                for part in parts:
                    part_val = self._resolve_single_input(part, user_inputs, node_map, runtime_outputs)
                    if part_val:
                        combined.append(str(part_val)[:500])
                resolved[key] = "\n\n".join(combined) if combined else ""
            else:
                resolved[key] = self._resolve_single_input(value, user_inputs, node_map, runtime_outputs) or ""
        return resolved

    def _resolve_single_input(
        self,
        value: str,
        user_inputs: dict[str, Any],
        node_map: dict[str, str],
        runtime_outputs: dict[str, Any] | None = None,
    ) -> Any:
        if value in user_inputs:
            return user_inputs[value]
        if ".output" in value:
            source_key = value.replace(".output", "")
            if runtime_outputs and source_key in runtime_outputs:
                return runtime_outputs[source_key]
            # Return the raw reference so AgentGraph._resolve_node_inputs
            # can resolve it at execution time using predecessor outputs.
            return value
        return value

    async def _persist_execution_graph(
        self,
        graph: ExecutionGraph,
        template_name: str,
        inputs: dict[str, Any],
        tenant_id: str,
    ) -> None:
        try:
            async with async_session_factory() as db:
                record = ExecutionGraphModel(
                    id=uuid.uuid4(),
                    session_id=uuid.UUID(graph.session_id),
                    graph_data=json.dumps(
                        {
                            "template": template_name,
                            "inputs": {k: str(v)[:200] for k, v in inputs.items()},
                            "nodes": [{"id": n.id, "agent_type": n.agent_type, "status": n.status, "error": n.error} for n in graph.nodes],
                            "edges": [{"source": e.source, "target": e.target} for e in graph.edges],
                        }
                    ),
                    status=graph.status,
                )
                db.add(record)
                await db.commit()
        except Exception as e:
            logger.warning(f"Failed to persist execution graph: {e}")


workflow_executor = WorkflowExecutor()
