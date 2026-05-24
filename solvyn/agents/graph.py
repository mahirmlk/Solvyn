import json
import logging
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

from solvyn.agents.registry import agent_registry
from solvyn.agents.schema import ExecutionEdge, ExecutionGraph, ExecutionNode

logger = logging.getLogger(__name__)


class AgentGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, ExecutionNode] = {}
        self.edges: list[ExecutionEdge] = []
        self.session_id: str = ""
        self.graph_id: str = ""
        self.overall_status = "pending"

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

    def _resolve_node_inputs(self, node_id: str) -> dict[str, Any]:
        """Resolve .output references in node inputs using predecessor outputs."""
        node = self.nodes[node_id]
        resolved: dict[str, Any] = {}
        for key, value in (node.input or {}).items():
            if isinstance(value, str) and value.endswith(".output"):
                source_type = value.replace(".output", "")
                for prev_id in reversed(self.get_prev_nodes(node_id)):
                    prev_node = self.nodes[prev_id]
                    if prev_node.agent_type == source_type and prev_node.status == "completed" and prev_node.output is not None:
                        resolved[key] = prev_node.output
                        break
                else:
                    resolved[key] = value
            else:
                resolved[key] = value
        return resolved

    async def execute(
        self,
        session_id: str,
        tenant_id: str,
        agent_configs: dict[str, dict[str, Any]] | None = None,
    ) -> ExecutionGraph:
        self.session_id = session_id
        self.graph_id = str(uuid.uuid4())
        configs = agent_configs or {}

        start_nodes = self.get_start_nodes()
        completed: set[str] = set()
        failed: set[str] = set()

        def _ready(nid: str) -> bool:
            prev = self.get_prev_nodes(nid)
            if all(p in completed or p in failed for p in prev):
                if any(p in failed for p in prev):
                    failed.add(nid)
                    node = self.nodes[nid]
                    node.status = "failed"
                    node.error = "Upstream dependency failed"
                    node.completed_at = datetime.now(UTC).isoformat()
                    return False
                return True
            return False

        queue = deque(start_nodes)

        while queue:
            node_id = queue.popleft()
            if node_id in completed or node_id in failed:
                continue
            if not _ready(node_id):
                if node_id not in failed:
                    queue.append(node_id)
                continue

            node = self.nodes[node_id]
            agent_type = node.agent_type

            try:
                agent = agent_registry.create(
                    agent_type=agent_type,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    config=configs.get(agent_type),
                )

                node.status = "running"
                node.started_at = datetime.now(UTC).isoformat()
                logger.info(f"Running node {node_id} ({agent_type})")

                resolved_input = self._resolve_node_inputs(node_id)
                output = await agent.run(resolved_input)
                node.output = output
                node.status = "completed"
                node.completed_at = datetime.now(UTC).isoformat()
                completed.add(node_id)

            except Exception as e:
                logger.exception(f"Node {node_id} ({agent_type}) failed: {e}")
                node.status = "failed"
                node.error = str(e)
                node.completed_at = datetime.now(UTC).isoformat()
                failed.add(node_id)

            next_nodes = self.get_next_nodes(node_id)
            for nn in next_nodes:
                if nn not in completed and nn not in failed:
                    queue.append(nn)

        self.overall_status = "failed" if failed else "completed"
        logger.info(f"Graph {self.graph_id} finished: {self.overall_status}")

        return ExecutionGraph(
            id=self.graph_id,
            session_id=session_id,
            nodes=list(self.nodes.values()),
            edges=self.edges,
            status=self.overall_status,
        )

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
