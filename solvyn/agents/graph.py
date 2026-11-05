from __future__ import annotations

import asyncio
import copy
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from solvyn.agents.registry import agent_registry
from solvyn.agents.schema import ExecutionEdge, ExecutionGraph, ExecutionNode

logger = logging.getLogger(__name__)


class AgentGraph:
    """Execution graph with parallel node execution for independent nodes.

    Nodes at the same topological level (no interdependencies) run
    concurrently via ``asyncio.gather``.  Nodes whose predecessors all
    failed are marked ``skipped`` rather than ``failed``.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, ExecutionNode] = {}
        self.edges: list[ExecutionEdge] = []
        self.session_id: str = ""
        self.graph_id: str = ""
        self.overall_status = "pending"

    # ------------------------------------------------------------------
    # Graph construction helpers
    # ------------------------------------------------------------------

    def add_node(self, agent_type: str, input_data: dict[str, Any] | None = None) -> str:
        node_id = str(uuid.uuid4())
        self.nodes[node_id] = ExecutionNode(id=node_id, agent_type=agent_type, input=input_data or {})
        return node_id

    def add_edge(self, source: str, target: str, condition: str | None = None) -> None:
        self.edges.append(ExecutionEdge(source=source, target=target, condition=condition))

    def get_start_nodes(self) -> list[str]:
        targets = {e.target for e in self.edges}
        return [nid for nid in self.nodes if nid not in targets]

    def get_next_nodes(self, node_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == node_id]

    def get_prev_nodes(self, node_id: str) -> list[str]:
        return [e.source for e in self.edges if e.target == node_id]

    # ------------------------------------------------------------------
    # Topological levels for parallel execution
    # ------------------------------------------------------------------

    def _compute_levels(self) -> list[list[str]]:
        """Return nodes grouped by topological level.

        Level 0 = start nodes (no predecessors).
        Level N = nodes whose predecessors are all in levels < N.
        Nodes within the same level have no interdependency and can
        run concurrently.
        """
        in_degree: dict[str, int] = {nid: 0 for nid in self.nodes}
        for edge in self.edges:
            in_degree[edge.target] = in_degree.get(edge.target, 0) + 1

        levels: list[list[str]] = []
        remaining = set(self.nodes.keys())

        while remaining:
            # Current level: all nodes whose in-degree (among remaining) is 0
            current_level = [nid for nid in remaining if in_degree[nid] == 0]
            if not current_level:
                # Circular dependency – break it by picking one node
                logger.warning("Circular dependency detected; breaking cycle")
                current_level = [next(iter(remaining))]

            levels.append(current_level)
            for nid in current_level:
                remaining.discard(nid)
                for edge in self.edges:
                    if edge.source == nid:
                        in_degree[edge.target] -= 1

        return levels

    # ------------------------------------------------------------------
    # Input resolution
    # ------------------------------------------------------------------

    def _resolve_value(self, value: Any, node_id: str) -> Any:
        """Recursively resolve output references in a single value."""
        suffix = ".output"
        # String reference: "agent_type.output"
        if isinstance(value, str) and value.endswith(suffix):
            source_type = value[:-len(suffix)]
            for prev_id in reversed(self.get_prev_nodes(node_id)):
                prev_node = self.nodes[prev_id]
                if prev_node.agent_type == source_type and prev_node.status == "completed" and prev_node.output is not None:
                    return prev_node.output
            logger.warning("Could not resolve output reference '%s' for node %s", value, node_id)
            return None

        # Dict reference: {"$node_output": "<node_id>"} or nested dicts
        if isinstance(value, dict):
            if "$node_output" in value:
                ref_node_id = value["$node_output"]
                ref_node = self.nodes.get(ref_node_id)
                if ref_node and ref_node.status == "completed" and ref_node.output is not None:
                    return ref_node.output
                return value
            return {k: self._resolve_value(v, node_id) for k, v in value.items()}

        # List with potential references
        if isinstance(value, list):
            return [self._resolve_value(item, node_id) for item in value]

        return value

    def _resolve_node_inputs(self, node_id: str) -> dict[str, Any]:
        """Resolve output references in node inputs using predecessor outputs.

        Supports two formats:
        - ``"agent_type.output"`` — resolves to the output of the named agent
        - ``{"$node_output": "<node_id>"}`` — resolves to the output of the node
        """
        node = self.nodes[node_id]
        return {k: self._resolve_value(v, node_id) for k, v in (node.input or {}).items()}

    # ------------------------------------------------------------------
    # Single-node runner (used by parallel execution)
    # ------------------------------------------------------------------

    async def _run_node(
        self,
        node_id: str,
        session_id: str,
        tenant_id: str,
        configs: dict[str, dict[str, Any]],
        on_checkpoint: Callable[[ExecutionGraph], Awaitable[None]] | None = None,
    ) -> None:
        """Execute a single node, updating its status and output."""
        node = self.nodes[node_id]
        agent_type = node.agent_type

        node.status = "running"
        node.started_at = datetime.now(UTC).isoformat()
        logger.info("Running node %s (%s)", node_id, agent_type)
        if on_checkpoint is not None:
            await on_checkpoint(self._execution_snapshot())

        try:
            agent = agent_registry.create(
                agent_type=agent_type,
                session_id=session_id,
                tenant_id=tenant_id,
                config=configs.get(agent_type),
            )
            resolved_input = self._resolve_node_inputs(node_id)
            output = await agent.run(resolved_input)
            node.output = output
            node.status = "completed"
            node.completed_at = datetime.now(UTC).isoformat()
            logger.info("Completed node %s (%s)", node_id, agent_type)
        except Exception as e:
            logger.exception("Node %s (%s) failed: %s", node_id, agent_type, e)
            node.status = "failed"
            node.error = str(e)
            node.completed_at = datetime.now(UTC).isoformat()
        if on_checkpoint is not None:
            await on_checkpoint(self._execution_snapshot())

    def _execution_snapshot(self) -> ExecutionGraph:
        return ExecutionGraph(
            id=self.graph_id,
            session_id=self.session_id,
            nodes=copy.deepcopy(list(self.nodes.values())),
            edges=copy.deepcopy(self.edges),
            status=self.overall_status,
        )

    # ------------------------------------------------------------------
    # Parallel graph execution
    # ------------------------------------------------------------------

    async def execute(
        self,
        session_id: str,
        tenant_id: str,
        agent_configs: dict[str, dict[str, Any]] | None = None,
        on_checkpoint: Callable[[ExecutionGraph], Awaitable[None]] | None = None,
        graph_id: str | None = None,
        execution_status: str = "running",
    ) -> ExecutionGraph:
        """Execute the graph with parallel execution for independent nodes.

        Nodes at the same topological level run concurrently via
        ``asyncio.gather``.  If any node in a level fails, downstream
        nodes are marked ``skipped``.
        """
        self.session_id = session_id
        self.graph_id = graph_id or str(uuid.uuid4())
        self.overall_status = execution_status
        configs = agent_configs or {}

        levels = self._compute_levels()
        completed: set[str] = {node_id for node_id, node in self.nodes.items() if node.status == "completed"}
        failed: set[str] = set()
        skipped: set[str] = set()
        has_real_failure = False
        if on_checkpoint is not None:
            await on_checkpoint(self._execution_snapshot())

        for level in levels:
            # Determine which nodes in this level are ready to run
            runnable: list[str] = []
            for node_id in level:
                if node_id in completed or node_id in failed or node_id in skipped:
                    continue
                prevs = self.get_prev_nodes(node_id)
                if any(p in failed for p in prevs):
                    # Upstream failed – skip this node
                    node = self.nodes[node_id]
                    node.status = "skipped"
                    node.error = "Upstream dependency failed"
                    node.completed_at = datetime.now(UTC).isoformat()
                    skipped.add(node_id)
                elif all(p in completed or p in failed or p in skipped for p in prevs):
                    runnable.append(node_id)
                else:
                    # Should not happen in a correct topological sort
                    runnable.append(node_id)

            if not runnable:
                if on_checkpoint is not None:
                    await on_checkpoint(self._execution_snapshot())
                continue

            if len(runnable) == 1:
                # Single node – run directly without gather overhead
                await self._run_node(runnable[0], session_id, tenant_id, configs, on_checkpoint)
            else:
                # Multiple independent nodes – run in parallel
                logger.info("Running level with %d parallel nodes: %s", len(runnable), runnable)
                await asyncio.gather(
                    *[self._run_node(nid, session_id, tenant_id, configs, on_checkpoint) for nid in runnable],
                    return_exceptions=False,
                )

            # Update completed/failed sets after the level
            for node_id in runnable:
                node = self.nodes[node_id]
                if node.status == "completed":
                    completed.add(node_id)
                else:
                    failed.add(node_id)
                    has_real_failure = True

            if on_checkpoint is not None:
                await on_checkpoint(self._execution_snapshot())

            # Propagate skipped status to downstream nodes of failed nodes
            for node_id in runnable:
                if self.nodes[node_id].status == "failed":
                    self._mark_downstream_skipped(node_id, skipped)

        self.overall_status = "failed" if has_real_failure else "completed"
        logger.info("Graph %s finished: %s", self.graph_id, self.overall_status)
        result = self._execution_snapshot()
        if on_checkpoint is not None:
            await on_checkpoint(result)
        return result

    def _mark_downstream_skipped(self, node_id: str, skipped: set[str]) -> None:
        """Recursively mark all downstream nodes as skipped when upstream fails."""
        for next_id in self.get_next_nodes(node_id):
            if next_id not in skipped:
                node = self.nodes[next_id]
                node.status = "skipped"
                node.error = "Upstream dependency failed"
                node.completed_at = datetime.now(UTC).isoformat()
                skipped.add(next_id)
                self._mark_downstream_skipped(next_id, skipped)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "session_id": self.session_id,
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
                for n in self.nodes.values()
            ],
            "edges": [{"source": e.source, "target": e.target, "condition": e.condition} for e in self.edges],
            "status": self.overall_status,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)
