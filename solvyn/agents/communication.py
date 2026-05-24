import logging
import uuid
from typing import Any

from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentMessage
from solvyn.message_bus.redis import message_bus

logger = logging.getLogger(__name__)


class AgentCommunication:
    def __init__(self) -> None:
        self._handlers: dict[str, type[BaseAgent]] = {}

    def register_handler(self, agent_type: str, agent_class: type[BaseAgent]) -> None:
        self._handlers[agent_type] = agent_class

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

    async def handle_incoming(
        self,
        raw: dict[str, Any],
        session_map: dict[str, list[BaseAgent]],
    ) -> None:
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
                    agent.handle_message(msg)
                    logger.debug(f"Routed message to {to_agent}")
                    matched = True
            if not matched:
                logger.warning(f"No handler for message: type={message_type}, to={to_agent}")
            return


agent_communication = AgentCommunication()
