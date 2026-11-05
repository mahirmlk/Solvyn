import logging
import uuid
from typing import Any

from solvyn.a2a.protocol import A2AEnvelope, TaskRequest
from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentMessage
from solvyn.message_bus.redis import message_bus

logger = logging.getLogger(__name__)


class AgentCommunication:
    def __init__(self) -> None:
        self._a2a_handlers: dict[str, Any] = {}
        self._active_agents: dict[str, BaseAgent] = {}

    def register_handler(self, agent_type: str, agent_class: type[BaseAgent]) -> None:
        self._a2a_handlers[agent_type] = self._create_a2a_handler(agent_type)

    def register_agent_instance(self, agent: BaseAgent) -> None:
        """Register a live agent instance so A2A messages can be routed to it."""
        self._active_agents[agent.agent_id] = agent
        agent_type = agent.metadata.agent_type
        if agent_type not in self._a2a_handlers:
            self._a2a_handlers[agent_type] = self._create_a2a_handler(agent_type)

    def unregister_agent_instance(self, agent_id: str) -> None:
        """Remove a live agent instance."""
        self._active_agents.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Legacy message API (still supported for backward compatibility)
    # ------------------------------------------------------------------

    async def send_message(self, message: AgentMessage) -> str:
        channel = f"agent:{message.to_agent}" if message.to_agent else f"session:{message.session_id}"
        return await message_bus.publish(
            channel=channel,
            message={
                "type": "agent_message",
                "payload": {
                    "id": message.id,
                    "from_agent": message.from_agent,
                    "to_agent": message.to_agent,
                    "session_id": message.session_id,
                    "message_type": message.type,
                    "content": message.content,
                    "correlation_id": message.correlation_id,
                    "timestamp": message.timestamp,
                },
            },
            tenant_id=message.tenant_id,
        )

    async def broadcast_to_session(self, session_id: str, message: AgentMessage) -> str:
        return await message_bus.publish(
            channel=f"session:{session_id}",
            message={
                "type": "broadcast",
                "payload": {
                    "from_agent": message.from_agent,
                    "message_type": message.type,
                    "content": message.content,
                },
            },
            tenant_id=message.tenant_id,
        )

    # ------------------------------------------------------------------
    # A2A protocol API
    # ------------------------------------------------------------------

    async def send_a2a_message(self, envelope: A2AEnvelope, task: TaskRequest) -> str:
        """Send a standardized A2A message using the new protocol."""
        from solvyn.a2a.broker import A2ABroker
        broker = A2ABroker()
        return await broker.send_task(envelope, task)

    async def send_a2a_task(
        self,
        from_agent: str,
        to_agent: str,
        input_data: dict[str, Any],
        session_id: str = "",
        tenant_id: str = "default",
        task_type: str = "general",
        priority: int = 0,
        timeout_seconds: float = 300.0,
    ) -> A2AEnvelope:
        """Convenience method: create envelope + task and send via A2A broker.

        Returns the envelope used so callers can track correlation_id.
        """
        from solvyn.a2a.broker import A2ABroker

        broker = A2ABroker()
        envelope = broker.create_envelope(
            from_agent=from_agent,
            to_agent=to_agent,
            session_id=session_id,
            tenant_id=tenant_id,
        )
        task = TaskRequest(
            task_type=task_type,
            input_data=input_data,
            priority=priority,
            timeout_seconds=timeout_seconds,
        )
        await broker.send_task(envelope, task)
        return envelope

    # ------------------------------------------------------------------
    # Incoming message handling
    # ------------------------------------------------------------------

    async def handle_incoming(
        self,
        raw: dict[str, Any],
        session_map: dict[str, list[BaseAgent]],
    ) -> None:
        # A2A messages
        if raw.get("type") == "a2a_task_request":
            from solvyn.a2a.broker import A2ABroker
            broker = A2ABroker()
            response = await broker.handle_incoming(raw)
            if response:
                logger.debug("A2A message processed: %s", response.envelope.message_id)
            return

        # Legacy agent messages
        payload = raw.get("payload", {})
        to_agent = payload.get("to_agent")
        session_id = payload.get("session_id")
        message_type = payload.get("message_type", "unknown")

        msg = AgentMessage(
            id=payload.get("id", str(uuid.uuid4())),
            from_agent=payload.get("from_agent", "unknown"),
            to_agent=to_agent,
            session_id=session_id or "",
            tenant_id=raw.get("tenant_id", "default"),
            type=message_type,
            content=payload.get("content", {}),
            correlation_id=payload.get("correlation_id"),
            timestamp=payload.get("timestamp", ""),
        )

        if to_agent:
            agents = session_map.get(session_id or "", [])
            matched = False
            for agent in agents:
                if agent.agent_id == to_agent or agent.metadata.agent_type == to_agent:
                    result = agent.handle_message(msg)
                    logger.debug("Routed message to %s (result=%s)", to_agent, type(result).__name__)
                    matched = True
            if not matched:
                logger.warning("No handler for message: type=%s, to=%s", message_type, to_agent)
            return

        # Broadcast: deliver to all agents in the session
        agents = session_map.get(session_id or "", [])
        for agent in agents:
            agent.handle_message(msg)

    # ------------------------------------------------------------------
    # A2A handler factory
    # ------------------------------------------------------------------

    def _create_a2a_handler(self, agent_type: str) -> Any:
        """Create a handler for A2A protocol that routes to agent instances.

        The handler looks up a live agent instance by type, executes it
        with the task input data, and returns the result.
        """

        async def handler(task: TaskRequest) -> dict[str, Any]:
            # Find a live agent instance for this type
            agent = self._find_agent_by_type(agent_type)
            if agent is None:
                raise ValueError(f"No agent instance available for type: {agent_type}")

            # Execute the agent
            result = await agent.run(task.input_data)
            return result if isinstance(result, dict) else {"result": result}

        return handler

    def _find_agent_by_type(self, agent_type: str) -> BaseAgent | None:
        """Find a live agent instance by type."""
        for agent in self._active_agents.values():
            if agent.metadata.agent_type == agent_type:
                return agent
        return None


agent_communication = AgentCommunication()
