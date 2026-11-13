import asyncio
import json
import logging
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.graph import AgentGraph
from solvyn.agents.schema import ExecutionEdge, ExecutionGraph, ExecutionNode
from solvyn.config import settings
from solvyn.db.models import Approval, Session, Task, TaskDependency, TaskHandoff, Tenant
from solvyn.db.models import ExecutionGraph as ExecutionGraphModel
from solvyn.db.session import async_session_factory, bind_tenant_scope
from solvyn.multi_tenant.middleware import tenant_context
from solvyn.workflows.task_completion import refresh_task_completion_state
from solvyn.workflows.templates import WORKFLOW_TEMPLATES, WorkflowStep

logger = logging.getLogger(__name__)
_LEASE_DURATION = timedelta(seconds=60)
_LEASE_HEARTBEAT_INTERVAL = 20


class WorkflowExecutionTimeoutError(TimeoutError):
    """Raised when a workflow exceeds its configured wall-clock deadline."""


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
            with tenant_context.scoped_tenant(tenant_uuid):
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
        except BaseException as exc:
            await self._mark_failed(
                execution_id,
                lease_id,
                tenant_uuid,
                error_message=str(exc) if isinstance(exc, WorkflowExecutionTimeoutError) else "Workflow execution failed",
            )
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

        with tenant_context.scoped_tenant(tenant_uuid):
            await self._persist_execution_graph(result, template_name, merged_inputs, tenant_uuid, lease_id)
        logger.info(f"Workflow '{template_name}' completed: {result.status}")
        return result

    async def schedule(
        self,
        template_name: str,
        inputs: dict[str, Any],
        scheduled_at: datetime,
        session_id: str | None = None,
        tenant_id: str = "default",
    ) -> ExecutionGraph:
        """Persist a future workflow run so a worker can start it after restart."""
        template = WORKFLOW_TEMPLATES.get(template_name)
        if template is None:
            raise ValueError(f"Unknown workflow: {template_name}. Available: {list(WORKFLOW_TEMPLATES.keys())}")
        if scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone")
        if scheduled_at <= datetime.now(UTC):
            raise ValueError("scheduled_at must be in the future")

        resolved_session_id = session_id or str(uuid.uuid4())
        tenant_uuid = await self._ensure_session(resolved_session_id, tenant_id, template_name)
        merged_inputs = {**template.default_inputs, **inputs}
        graph = AgentGraph()
        node_map: dict[str, str] = {}
        latest_by_agent_type: dict[str, str] = {}
        for index, step in enumerate(template.steps):
            step_key = f"{step.agent_type}_{index}"
            step_input = self._resolve_inputs(step, merged_inputs, node_map, {})
            node_id = graph.add_node(agent_type=step.agent_type, input_data=step_input)
            node_map[step_key] = node_id
            latest_by_agent_type[step.agent_type] = node_id
            if step.depends_on:
                for dependency in step.depends_on:
                    dependency_id = latest_by_agent_type.get(dependency)
                    if dependency_id:
                        graph.add_edge(source=dependency_id, target=node_id)

        execution_id = str(uuid.uuid4())
        graph_data = json.dumps(
            {
                "template": template_name,
                "inputs": merged_inputs,
                "nodes": [
                    {"id": node.id, "agent_type": node.agent_type, "status": "pending", "input": node.input}
                    for node in graph.nodes.values()
                ],
                "edges": [
                    {"source": edge.source, "target": edge.target, "condition": edge.condition} for edge in graph.edges
                ],
            }
        )
        async with async_session_factory() as db:
            await bind_tenant_scope(db, tenant_uuid)
            db.add(
                ExecutionGraphModel(
                    id=uuid.UUID(execution_id),
                    session_id=uuid.UUID(resolved_session_id),
                    graph_data=graph_data,
                    status="scheduled",
                    scheduled_at=scheduled_at.astimezone(UTC),
                )
            )
            await db.commit()
        return ExecutionGraph(
            id=execution_id,
            session_id=resolved_session_id,
            nodes=list(graph.nodes.values()),
            edges=list(graph.edges),
            status="scheduled",
        )

    async def resume(self, execution_id: uuid.UUID, tenant_id: str) -> ExecutionGraph:
        """Resume an interrupted run, rerunning every unfinished node at least once."""
        tenant_uuid = await self._resolve_tenant_uuid(tenant_id)
        async with async_session_factory() as db:
            await bind_tenant_scope(db, tenant_uuid)
            record = (
                await db.execute(
                    select(ExecutionGraphModel)
                    .join(Session, Session.id == ExecutionGraphModel.session_id)
                    .where(ExecutionGraphModel.id == execution_id, Session.tenant_id == tenant_uuid)
                )
            ).scalar_one_or_none()
            if record is None:
                raise LookupError("Workflow execution not found")
            if record.status not in {"scheduled", "running", "failed", "resuming"}:
                raise RuntimeError(f"Workflow execution cannot resume from status '{record.status}'")
            if record.status == "scheduled" and record.scheduled_at and record.scheduled_at > datetime.now(UTC):
                raise RuntimeError("Workflow execution is not due yet")
            graph_data = json.loads(record.graph_data)
            lease_id = uuid.uuid4()
            can_claim = or_(
                ExecutionGraphModel.status == "failed",
                and_(ExecutionGraphModel.status == "scheduled", ExecutionGraphModel.scheduled_at <= func.now()),
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
            with tenant_context.scoped_tenant(tenant_uuid):
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
        except BaseException as exc:
            await self._mark_failed(
                str(execution_id),
                lease_id,
                tenant_uuid,
                error_message=str(exc) if isinstance(exc, WorkflowExecutionTimeoutError) else "Workflow execution failed",
            )
            raise
        return result

    async def cancel(self, execution_id: uuid.UUID, tenant_id: str) -> dict[str, str]:
        """Cancel a tenant-owned execution and fence its current worker."""
        tenant_uuid = await self._resolve_tenant_uuid(tenant_id)
        async with async_session_factory() as db:
            await bind_tenant_scope(db, tenant_uuid)
            record = (
                await db.execute(
                    select(ExecutionGraphModel)
                    .join(Session, Session.id == ExecutionGraphModel.session_id)
                    .where(ExecutionGraphModel.id == execution_id, Session.tenant_id == tenant_uuid)
                )
            ).scalar_one_or_none()
            if record is None:
                raise LookupError("Workflow execution not found")
            if record.status == "cancelled":
                await self._terminate_execution_handoffs(
                    db,
                    execution_id,
                    tenant_uuid,
                    status="cancelled",
                    error="Workflow execution was cancelled",
                )
                await self._finalize_cancelled_execution(db, execution_id, tenant_uuid, record.graph_data)
                await db.commit()
                return {"execution_id": str(execution_id), "status": "cancelled"}
            if record.status == "cancelling":
                finalized = await db.execute(
                    update(ExecutionGraphModel)
                    .where(
                        ExecutionGraphModel.id == execution_id,
                        ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_uuid)),
                        ExecutionGraphModel.status == "cancelling",
                        ExecutionGraphModel.lease_owner == record.lease_owner,
                        or_(
                            ExecutionGraphModel.lease_owner.is_(None),
                            ExecutionGraphModel.lease_expires_at.is_(None),
                            ExecutionGraphModel.lease_expires_at <= func.now(),
                        ),
                    )
                    .values(status="cancelled", lease_owner=None, lease_expires_at=None)
                    .returning(ExecutionGraphModel.id)
                )
                if finalized.scalar_one_or_none() is None:
                    current_status = await db.scalar(
                        select(ExecutionGraphModel.status)
                        .join(Session, Session.id == ExecutionGraphModel.session_id)
                        .where(ExecutionGraphModel.id == execution_id, Session.tenant_id == tenant_uuid)
                    )
                    if current_status in {"cancelling", "cancelled"}:
                        return {"execution_id": str(execution_id), "status": current_status}
                    raise RuntimeError("Workflow execution changed while stale cancellation was being finalized")
                await self._terminate_execution_handoffs(
                    db,
                    execution_id,
                    tenant_uuid,
                    status="cancelled",
                    error="Workflow execution was cancelled",
                )
                await self._finalize_cancelled_execution(db, execution_id, tenant_uuid, record.graph_data)
                await db.commit()
                return {"execution_id": str(execution_id), "status": "cancelled"}
            if record.status not in {"scheduled", "running", "resuming", "failed"}:
                raise RuntimeError(f"Workflow execution cannot be cancelled from status '{record.status}'")

            worker_active = (
                record.status in {"running", "resuming"}
                and record.lease_owner is not None
                and record.lease_expires_at is not None
                and record.lease_expires_at > datetime.now(UTC)
            )
            next_status = "cancelling" if worker_active else "cancelled"
            changed = await db.execute(
                update(ExecutionGraphModel)
                .where(
                    ExecutionGraphModel.id == execution_id,
                    ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_uuid)),
                    ExecutionGraphModel.status == record.status,
                    ExecutionGraphModel.lease_owner == record.lease_owner,
                )
                .values(
                    status=next_status,
                    lease_owner=record.lease_owner if worker_active else None,
                    lease_expires_at=record.lease_expires_at if worker_active else None,
                )
                .returning(ExecutionGraphModel.id)
            )
            if changed.scalar_one_or_none() is None:
                await db.rollback()
                raise RuntimeError("Workflow execution changed while cancellation was requested")

            await self._terminate_execution_handoffs(
                db,
                execution_id,
                tenant_uuid,
                status="cancelled",
                error="Workflow execution was cancelled",
            )
            if not worker_active:
                await self._finalize_cancelled_execution(db, execution_id, tenant_uuid, record.graph_data)
            await db.commit()
            return {"execution_id": str(execution_id), "status": next_status}

    async def _terminate_execution_handoffs(
        self,
        db: AsyncSession,
        execution_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        status: str,
        error: str,
    ) -> None:
        """Fence handoff workers and expire approvals when their workflow ends."""
        task_ids = select(Task.id).where(Task.execution_id == execution_id, Task.tenant_id == tenant_id)
        updated = await db.execute(
            update(TaskHandoff)
            .where(
                TaskHandoff.tenant_id == tenant_id,
                TaskHandoff.child_task_id.in_(task_ids),
                TaskHandoff.status.in_(["pending", "waiting_approval", "running", "failed"]),
            )
            .values(
                status=status,
                error=error[:2_000],
                lease_owner=None,
                lease_expires_at=None,
                completed_at=func.now(),
            )
            .returning(TaskHandoff.id)
        )
        handoff_ids = updated.scalars().all()
        if handoff_ids:
            await db.execute(
                update(Approval)
                .where(Approval.task_handoff_id.in_(handoff_ids), Approval.status == "pending")
                .values(
                    status="expired",
                    decision_note=error[:2_000],
                    decided_at=func.now(),
                    updated_at=func.now(),
                )
            )

    async def _finalize_cancelled_execution(
        self,
        db: AsyncSession,
        execution_id: uuid.UUID,
        tenant_id: uuid.UUID,
        serialized_graph: str,
    ) -> None:
        graph_data = json.loads(serialized_graph)
        graph_data["status"] = "cancelled"
        for node in graph_data.get("nodes", []):
            if node.get("status") in {"pending", "running", "resuming", "waiting_approval"}:
                node["status"] = "cancelled"
                node["error"] = "Workflow execution was cancelled"
                node["completed_at"] = datetime.now(UTC).isoformat()
        await db.execute(
            update(ExecutionGraphModel)
            .where(ExecutionGraphModel.id == execution_id, ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_id)))
            .values(graph_data=json.dumps(graph_data))
        )
        await db.execute(
            update(Task)
            .where(
                Task.execution_id == execution_id,
                Task.tenant_id == tenant_id,
                Task.status.in_(["pending", "running", "resuming", "waiting_approval"]),
            )
            .values(status="cancelled", error="Workflow execution was cancelled", completed_at=func.now())
        )

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
            await bind_tenant_scope(db, tenant_id)
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
                        ExecutionGraphModel.status.in_(["running", "resuming", "completed", "failed"]),
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

            tasks_by_node: dict[str, Task] = {}
            for node in graph.nodes:
                task_result = await db.execute(
                    select(Task).where(
                        Task.execution_id == graph_id,
                        Task.node_id == node.id,
                        Task.tenant_id == tenant_id,
                    )
                )
                task = task_result.scalar_one_or_none()
                if task is None:
                    task = Task(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        session_id=uuid.UUID(graph.session_id),
                        execution_id=graph_id,
                        node_id=node.id,
                        title=f"{node.agent_type} workflow step",
                        description=json.dumps(node.input or {}),
                        assigned_agent=node.agent_type,
                        status=node.status,
                        priority=0,
                        attempt_count=1 if node.status == "running" else 0,
                        scheduled_at=func.now(),
                    )
                else:
                    if node.status == "running" and task.status != "running":
                        task.attempt_count += 1
                    task.description = json.dumps(node.input or {})
                    task.status = node.status
                task.result = json.dumps(node.output) if node.output is not None else None
                task.error = node.error
                task.started_at = self._parse_timestamp(node.started_at)
                task.completed_at = self._parse_timestamp(node.completed_at)
                db.add(task)
                tasks_by_node[node.id] = task

            await db.flush()
            for node in graph.nodes:
                if node.status == "completed":
                    await refresh_task_completion_state(db, tasks_by_node[node.id])
            for edge in graph.edges:
                source = tasks_by_node.get(edge.source)
                target = tasks_by_node.get(edge.target)
                if source is None or target is None:
                    continue
                dependency = (
                    await db.execute(
                        select(TaskDependency).where(
                            TaskDependency.tenant_id == tenant_id,
                            TaskDependency.task_id == target.id,
                            TaskDependency.depends_on_task_id == source.id,
                        )
                    )
                ).scalar_one_or_none()
                if dependency is None:
                    db.add(
                        TaskDependency(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            task_id=target.id,
                            depends_on_task_id=source.id,
                        )
                    )
            await db.commit()

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        return datetime.fromisoformat(value) if value else None

    async def _execute_with_lease(
        self,
        execution_id: str,
        lease_id: uuid.UUID,
        tenant_id: uuid.UUID,
        execution: Coroutine[Any, Any, ExecutionGraph],
    ) -> ExecutionGraph:
        execution_task: asyncio.Task[ExecutionGraph] = asyncio.create_task(execution)
        heartbeat_task: asyncio.Task[bool] = asyncio.create_task(self._heartbeat(execution_id, lease_id, tenant_id))
        try:
            done, _ = await asyncio.wait(
                {execution_task, heartbeat_task},
                timeout=settings.workflow_execution_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise WorkflowExecutionTimeoutError(
                    "Workflow execution exceeded "
                    f"{settings.workflow_execution_timeout_seconds} seconds"
                )
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
                await bind_tenant_scope(db, tenant_id)
                renewed = await db.execute(
                    update(ExecutionGraphModel)
                    .where(
                        ExecutionGraphModel.id == uuid.UUID(execution_id),
                        ExecutionGraphModel.lease_owner == lease_id,
                        ExecutionGraphModel.status.in_(["running", "resuming"]),
                        ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_id)),
                    )
                    .values(lease_expires_at=func.now() + _LEASE_DURATION)
                    .returning(ExecutionGraphModel.id)
                )
                await db.commit()
                if renewed.scalar_one_or_none() != uuid.UUID(execution_id):
                    return False

    async def _mark_failed(
        self,
        execution_id: str,
        lease_id: uuid.UUID,
        tenant_id: uuid.UUID,
        *,
        error_message: str = "Workflow execution failed",
    ) -> None:
        async with async_session_factory() as db:
            await bind_tenant_scope(db, tenant_id)
            identity_scope = (
                ExecutionGraphModel.id == uuid.UUID(execution_id),
                ExecutionGraphModel.lease_owner == lease_id,
                ExecutionGraphModel.session_id.in_(select(Session.id).where(Session.tenant_id == tenant_id)),
            )
            cancelled = await db.execute(
                update(ExecutionGraphModel)
                .where(*identity_scope, ExecutionGraphModel.status == "cancelling")
                .values(status="cancelled", lease_owner=None, lease_expires_at=None)
                .returning(ExecutionGraphModel.id)
            )
            if cancelled.scalar_one_or_none() is not None:
                record = await db.get(ExecutionGraphModel, uuid.UUID(execution_id))
                if record is not None:
                    graph_data = json.loads(record.graph_data)
                    graph_data["status"] = "cancelled"
                    for node in graph_data.get("nodes", []):
                        if node.get("status") in {"pending", "running", "resuming", "waiting_approval"}:
                            node["status"] = "cancelled"
                            node["error"] = "Workflow execution was cancelled"
                            node["completed_at"] = datetime.now(UTC).isoformat()
                    record.graph_data = json.dumps(graph_data)
                await self._terminate_execution_handoffs(
                    db,
                    uuid.UUID(execution_id),
                    tenant_id,
                    status="cancelled",
                    error="Workflow execution was cancelled",
                )
                await db.execute(
                    update(Task)
                    .where(
                        Task.execution_id == uuid.UUID(execution_id),
                        Task.tenant_id == tenant_id,
                        Task.status.in_(["pending", "running", "resuming", "waiting_approval"]),
                    )
                    .values(status="cancelled", error="Workflow execution was cancelled", completed_at=func.now())
                )
                await db.commit()
                return

            failed = await db.execute(
                update(ExecutionGraphModel)
                .where(
                    *identity_scope,
                    ExecutionGraphModel.status.in_(["running", "resuming"]),
                )
                .values(status="failed", lease_owner=None, lease_expires_at=None)
                .returning(ExecutionGraphModel.id)
            )
            if failed.scalar_one_or_none() is None:
                await db.commit()
                return
            record = await db.get(ExecutionGraphModel, uuid.UUID(execution_id))
            if record is not None:
                graph_data = json.loads(record.graph_data)
                graph_data["status"] = "failed"
                for node in graph_data.get("nodes", []):
                    if node.get("status") in {"pending", "running"}:
                        node["status"] = "failed"
                        node["error"] = error_message[:2_000]
                        node["completed_at"] = datetime.now(UTC).isoformat()
                record.graph_data = json.dumps(graph_data)
            await self._terminate_execution_handoffs(
                db,
                uuid.UUID(execution_id),
                tenant_id,
                status="failed",
                error=error_message,
            )
            await db.execute(
                update(Task)
                .where(
                    Task.execution_id == uuid.UUID(execution_id),
                    Task.tenant_id == tenant_id,
                    Task.status.in_(["pending", "running", "resuming", "waiting_approval"]),
                )
                .values(
                    status="failed",
                    error=error_message[:2_000],
                    completed_at=func.now(),
                )
            )
            await db.commit()

    async def _ensure_session(self, session_id: str, tenant_id: str, template_name: str) -> uuid.UUID:
        tenant_uuid = await self._resolve_tenant_uuid(tenant_id)

        session_uuid = uuid.UUID(session_id)
        async with async_session_factory() as db:
            await bind_tenant_scope(db, tenant_uuid)
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
