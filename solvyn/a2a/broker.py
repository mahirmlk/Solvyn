from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from solvyn.a2a.protocol import (
    A2AEnvelope,
    A2AMessageType,
    A2AResponse,
    TaskRequest,
    TaskResponse,
    TaskStatus,
)
from solvyn.message_bus.redis import message_bus

logger = logging.getLogger(__name__)


class A2ABroker:
    """Standardized agent-to-agent message broker with versioning and structured envelopes."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[..., Any]] = {}

    def register_handler(self, agent_type: str, handler: Callable[..., Any]) -> None:
        self._handlers[agent_type] = handler
        logger.debug(f"Registered A2A handler for {agent_type}")

    async def send_task(self, envelope: A2AEnvelope, task: TaskRequest) -> str:
        payload = {
            "a2a_version": envelope.version,
            "envelope": envelope.to_dict(),
            "task": task.to_dict(),
        }
        channel = f"agent:{envelope.to_agent}" if envelope.to_agent else f"session:{envelope.session_id}"
        return await message_bus.publish(
            channel=channel,
            message={"type": "a2a_task_request", "payload": payload},
            tenant_id=envelope.tenant_id,
        )

    async def send_response(self, envelope: A2AEnvelope, response: TaskResponse) -> str:
        payload = {
            "a2a_version": envelope.version,
            "envelope": envelope.to_dict(),
            "response": response.to_dict(),
        }
        channel = f"agent:{envelope.to_agent}" if envelope.to_agent else f"session:{envelope.session_id}"
        return await message_bus.publish(
            channel=channel,
            message={"type": "a2a_task_response", "payload": payload},
            tenant_id=envelope.tenant_id,
        )

    async def handle_incoming(self, raw: dict[str, Any]) -> A2AResponse | None:
        payload = raw.get("payload", {})
        a2a_version = payload.get("a2a_version", "1.0.0")
        envelope_data = payload.get("envelope", {})
        envelope = A2AEnvelope.from_dict(envelope_data)

        message_type = envelope_data.get("message_type", "")

        if message_type == A2AMessageType.TASK_REQUEST:
            task_data = payload.get("task", {})
            task = TaskRequest.from_dict(task_data)
            return await self._route_task(envelope, task, a2a_version)
        if message_type == A2AMessageType.TASK_RESPONSE:
            response_data = payload.get("response", {})
            response = TaskResponse.from_dict(response_data)
            logger.info(f"A2A response received: task={response.task_id} status={response.status}")
            return A2AResponse(success=True, envelope=envelope, payload=response.to_dict())

        logger.warning(f"Unknown A2A message type: {message_type}")
        return None

    async def _route_task(self, envelope: A2AEnvelope, task: TaskRequest, a2a_version: str) -> A2AResponse:
        to_agent = envelope.to_agent
        handler = self._handlers.get(to_agent)

        if handler is None:
            logger.error(f"No A2A handler registered for agent: {to_agent}")
            return A2AResponse(
                success=False,
                envelope=envelope,
                payload={"error": f"No handler for agent '{to_agent}'"},
            )

        try:
            result = await handler(task)
            response = TaskResponse(
                task_id=task.task_id,
                status=TaskStatus.COMPLETED,
                output_data=result if isinstance(result, dict) else {"result": result},
            )
            return A2AResponse(success=True, envelope=envelope, payload=response.to_dict())
        except Exception as e:
            logger.exception(f"A2A handler failed for {to_agent}: {e}")
            response = TaskResponse(
                task_id=task.task_id,
                status=TaskStatus.FAILED,
                error=str(e),
            )
            return A2AResponse(success=False, envelope=envelope, payload=response.to_dict())

    def create_envelope(
        self,
        from_agent: str,
        to_agent: str,
        session_id: str,
        tenant_id: str = "default",
        correlation_id: str = "",
    ) -> A2AEnvelope:
        return A2AEnvelope(
            message_id=str(uuid.uuid4()),
            message_type=A2AMessageType.TASK_REQUEST,
            from_agent=from_agent,
            to_agent=to_agent,
            session_id=session_id,
            tenant_id=tenant_id,
            correlation_id=correlation_id or str(uuid.uuid4()),
        )


a2a_broker = A2ABroker()
