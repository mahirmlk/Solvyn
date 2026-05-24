import logging
from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

logger = logging.getLogger(__name__)


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, type[BaseAgent]] = {}
        self._metadata: dict[str, AgentMetadata] = {}

    def register(self, agent_class: type[BaseAgent]) -> None:
        metadata = agent_class(None, None, None).metadata
        self._agents[metadata.agent_type] = agent_class
        self._metadata[metadata.agent_type] = metadata
        caps = ", ".join(c.value for c in metadata.capabilities)
        logger.info(f"  Registered agent: {metadata.agent_type} [{caps}]")

    def create(
        self,
        agent_type: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        tenant_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> BaseAgent:
        cls = self._agents.get(agent_type)
        if cls is None:
            raise ValueError(f"Unknown agent type: {agent_type}. Available: {list(self._agents.keys())}")
        return cls(agent_id=agent_id, session_id=session_id, tenant_id=tenant_id, config=config)

    def list_agents(self) -> dict[str, AgentMetadata]:
        return dict(self._metadata)

    def get_metadata(self, agent_type: str) -> AgentMetadata | None:
        return self._metadata.get(agent_type)

    def find_by_capability(self, capability: AgentCapability) -> list[AgentMetadata]:
        return [m for m in self._metadata.values() if capability in m.capabilities]


agent_registry = AgentRegistry()
