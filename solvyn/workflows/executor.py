import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from solvyn.agents.graph import AgentGraph
from solvyn.agents.schema import ExecutionEdge, ExecutionGraph, ExecutionNode
from solvyn.db.models import ExecutionGraph as ExecutionGraphModel
from solvyn.db.models import Session, Tenant
from solvyn.db.session import async_session_factory
from solvyn.workflows.templates import WORKFLOW_TEMPLATES, WorkflowStep

logger = logging.getLogger(__name__)
_LEASE_DURATION = timedelta(seconds=60)
_LEASE_HEARTBEAT_INTERVAL = 20


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
        tenant_uuid = await self._ensure_session(resolved_session_id, tenant_id, template_name)
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
        checkpoint_lock = asyncio.Lock()
        lease_id = uuid.uuid4()
        execution_id = str(uuid.uuid4())

        async def persist_checkpoint(snapshot: ExecutionGraph) -> None:
            async with checkpoint_lock:
                await self._persist_execution_graph(snapshot, template_name, merged_inputs, tenant_uuid, lease_id)

        try:
            result = await self._execute_with_lease(
                execution_id,
                lease_id,
                tenant_uuid,
                graph.execute(
                    session_id=resolved_session_id,
                    tenant_id=tenant_id,
                    agent_configs={},
                    on_checkpoint=persist_checkpoint,
                    graph_id=execution_id,
                ),
            )
        except BaseException:
            await self._mark_failed(execution_id, lease_id, tenant_uuid)
            raise

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

        await self._persist_execution_graph(result, template_name, merged_inputs, tenant_uuid, lease_id)
        logger.info(f"Workflow '{template_name}' completed: {result.status}")
        return result

    async def resume(self, execution_id: uuid.UUID, tenant_id: str) -> ExecutionGraph:
        """Resume an interrupted run, rerunning every unfinished node at least once."""
        tenant_uuid = await self._resolve_tenant_uuid(tenant_id)
        async with async_session_factory() as db:
            record = (
                await db.execute(
                    select(ExecutionGraphModel)
                    .join(Session, Session.id == ExecutionGraphModel.session_id)
                    .where(ExecutionGraphModel.id == execution_id, Session.tenant_id == tenant_uuid)
                )
            ).scalar_one_or_none()
            if record is None:
                raise LookupError("Workflow execution not found")
            if record.status not in {"running", "failed", "resuming"}:
                raise RuntimeError(f"Workflow execution cannot resume from status '{record.status}'")
            graph_data = json.loads(record.graph_data)
            lease_id = uuid.uuid4()
            can_claim = or_(
                ExecutionGraphModel.status == "failed",
                and_(
                    ExecutionGraphModel.status.in_(["running", "resuming"]),
                    or_(ExecutionGraphModel.lease_expires_at.is_(None), ExecutionGraphModel.lease_expires_at < func.now()),
                ),
            )
            claimed = await db.execute(
                update(ExecutionGraphModel)
                .where(
                    ExecutionGraphModel.id == execution_id,
                    ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_uuid)),
                    can_claim,
                )
                .values(
                    status="resuming",
                    lease_owner=lease_id,
                    lease_expires_at=func.now() + _LEASE_DURATION,
                )
                .returning(ExecutionGraphModel.id)
            )
            if claimed.scalar_one_or_none() != execution_id:
                await db.rollback()
                raise RuntimeError("Workflow execution is already being resumed")
            await db.commit()
            session_id = str(record.session_id)

        graph = AgentGraph()
        graph.graph_id = str(execution_id)
        graph.session_id = session_id
        for item in graph_data.get("nodes", []):
            node = ExecutionNode(
                id=item["id"],
                agent_type=item["agent_type"],
                status=item.get("status", "pending"),
                input=item.get("input"),
                output=item.get("output"),
                started_at=item.get("started_at"),
                completed_at=item.get("completed_at"),
                error=item.get("error"),
            )
            if node.status != "completed":
                # A worker interruption may have happened before the node's
                # external call returned; resuming is intentionally at-least-once.
                node.status = "pending"
                node.output = None
                node.error = None
                node.started_at = None
                node.completed_at = None
            graph.nodes[node.id] = node
        graph.edges = [
            ExecutionEdge(source=item["source"], target=item["target"], condition=item.get("condition"))
            for item in graph_data.get("edges", [])
        ]
        template_name = str(graph_data["template"])
        inputs = graph_data.get("inputs", {})
        checkpoint_lock = asyncio.Lock()

        async def persist_checkpoint(snapshot: ExecutionGraph) -> None:
            async with checkpoint_lock:
                await self._persist_execution_graph(snapshot, template_name, inputs, tenant_uuid, lease_id)

        try:
            result = await self._execute_with_lease(
                str(execution_id),
                lease_id,
                tenant_uuid,
                graph.execute(
                    session_id=session_id,
                    tenant_id=str(tenant_uuid),
                    on_checkpoint=persist_checkpoint,
                    graph_id=str(execution_id),
                    execution_status="resuming",
                ),
            )
        except BaseException:
            await self._mark_failed(str(execution_id), lease_id, tenant_uuid)
            raise
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
        tenant_id: uuid.UUID,
        lease_id: uuid.UUID,
    ) -> None:
        async with async_session_factory() as db:
            graph_id = uuid.UUID(graph.id)
            record = await db.get(ExecutionGraphModel, graph_id)
            graph_data = json.dumps(
                {
                    "template": template_name,
                    "inputs": inputs,
                    "nodes": [
                        {
                            "id": n.id,
                            "agent_type": n.agent_type,
                            "status": n.status,
                            "input": n.input,
                            "output": n.output,
                            "started_at": n.started_at,
                            "completed_at": n.completed_at,
                            "error": n.error,
                        }
                        for n in graph.nodes
                    ],
                    "edges": [{"source": e.source, "target": e.target, "condition": e.condition} for e in graph.edges],
                }
            )
            if record is None:
                record = ExecutionGraphModel(
                    id=graph_id,
                    session_id=uuid.UUID(graph.session_id),
                    graph_data=graph_data,
                    status=graph.status,
                    lease_owner=lease_id,
                    lease_expires_at=func.now() + _LEASE_DURATION if graph.status in {"running", "resuming"} else None,
                )
                db.add(record)
            else:
                updated = await db.execute(
                    update(ExecutionGraphModel)
                    .where(
                        ExecutionGraphModel.id == graph_id,
                        ExecutionGraphModel.lease_owner == lease_id,
                        ExecutionGraphModel.session_id == uuid.UUID(graph.session_id),
                        ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_id)),
                    )
                    .values(
                        graph_data=graph_data,
                        status=graph.status,
                        lease_expires_at=func.now() + _LEASE_DURATION if graph.status in {"running", "resuming"} else None,
                        lease_owner=lease_id,
                    )
                    .returning(ExecutionGraphModel.id)
                )
                if updated.scalar_one_or_none() is None:
                    raise RuntimeError("Workflow execution lease was lost; refusing to overwrite its state")
            await db.commit()

    async def _execute_with_lease(
        self,
        execution_id: str,
        lease_id: uuid.UUID,
        tenant_id: uuid.UUID,
        execution: Awaitable[ExecutionGraph],
    ) -> ExecutionGraph:
        execution_task = asyncio.create_task(execution)
        heartbeat_task = asyncio.create_task(self._heartbeat(execution_id, lease_id, tenant_id))
        try:
            done, _ = await asyncio.wait({execution_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED)
            if execution_task in done:
                return await execution_task
            if heartbeat_task in done:
                still_owner = heartbeat_task.result()
                if not still_owner:
                    raise RuntimeError("Workflow execution lease was lost; stopping this worker")
            return await execution_task
        finally:
            if not execution_task.done():
                execution_task.cancel()
            if not heartbeat_task.done():
                heartbeat_task.cancel()
            await asyncio.gather(execution_task, heartbeat_task, return_exceptions=True)

    async def _heartbeat(self, execution_id: str, lease_id: uuid.UUID, tenant_id: uuid.UUID) -> bool:
        while True:
            await asyncio.sleep(_LEASE_HEARTBEAT_INTERVAL)
            async with async_session_factory() as db:
                renewed = await db.execute(
                    update(ExecutionGraphModel)
                    .where(
                        ExecutionGraphModel.id == uuid.UUID(execution_id),
                        ExecutionGraphModel.lease_owner == lease_id,
                        ExecutionGraphModel.status.in_(["running", "resuming"]),
                        ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_id)),
                    )
                    .values(lease_expires_at=func.now() + _LEASE_DURATION)
                )
                await db.commit()
                if renewed.rowcount != 1:
                    return False

    async def _mark_failed(self, execution_id: str, lease_id: uuid.UUID, tenant_id: uuid.UUID) -> None:
        async with async_session_factory() as db:
            await db.execute(
                update(ExecutionGraphModel)
                .where(
                    ExecutionGraphModel.id == uuid.UUID(execution_id),
                    ExecutionGraphModel.lease_owner == lease_id,
                    ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_id)),
                )
                .values(status="failed", lease_owner=None, lease_expires_at=None)
            )
            await db.commit()

    async def _ensure_session(self, session_id: str, tenant_id: str, template_name: str) -> uuid.UUID:
        tenant_uuid = await self._resolve_tenant_uuid(tenant_id)

        session_uuid = uuid.UUID(session_id)
        async with async_session_factory() as db:
            session = await db.get(Session, session_uuid)
            if session is not None:
                if session.tenant_id != tenant_uuid:
                    raise PermissionError("Session does not belong to the current tenant")
                return tenant_uuid
            db.add(Session(id=session_uuid, tenant_id=tenant_uuid, title=f"Workflow: {template_name}"))
            await db.commit()
        return tenant_uuid

    async def _resolve_tenant_uuid(self, tenant_id: str) -> uuid.UUID:
        async with async_session_factory() as db:
            try:
                parsed_id = uuid.UUID(tenant_id)
            except ValueError:
                tenant = (await db.execute(select(Tenant).where(Tenant.slug == tenant_id, Tenant.is_active.is_(True)))).scalar_one_or_none()
            else:
                tenant = (await db.execute(select(Tenant).where(Tenant.id == parsed_id, Tenant.is_active.is_(True)))).scalar_one_or_none()
            if tenant is None:
                raise ValueError("Workflow execution requires an active tenant")
            return tenant.id


workflow_executor = WorkflowExecutor()
