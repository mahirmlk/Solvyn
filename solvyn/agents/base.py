import logging
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from solvyn.agents.schema import AgentMessage, AgentMetadata, AgentStatus, Checkpoint, ExecutionNode
from solvyn.llm.providers.base import LLMResponse
from solvyn.llm.router import llm_router

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(
        self,
        agent_id: str | None = None,
        session_id: str | None = None,
        tenant_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.agent_id = agent_id or str(uuid.uuid4())
        self.session_id = session_id
        self.tenant_id = tenant_id
        self.config = config or {}
        self.status = AgentStatus.IDLE
        self.state: dict[str, Any] = {}
        self.execution_node: ExecutionNode | None = None
        self._llm_provider: str | None = None
        self._llm_model: str | None = None

    @property
    @abstractmethod
    def metadata(self) -> AgentMetadata: ...

    async def run(self, input_data: dict[str, Any]) -> dict[str, Any]:
        self.status = AgentStatus.RUNNING
        self.execution_node = ExecutionNode(id=str(uuid.uuid4()), agent_type=self.metadata.agent_type, input=input_data)
        self.state["input"] = input_data

        try:
            self.execution_node.started_at = datetime.now(UTC).isoformat()
            output = await self.execute(input_data)
            self.state["output"] = output
            self.execution_node.output = output
            self.execution_node.status = "completed"
            self.execution_node.completed_at = datetime.now(UTC).isoformat()
            self.status = AgentStatus.COMPLETED
            return output
        except Exception as e:
            logger.exception(f"Agent {self.agent_id} failed: {e}")
            self.execution_node.status = "failed"
            self.execution_node.error = str(e)
            self.execution_node.completed_at = datetime.now(UTC).isoformat()
            self.status = AgentStatus.FAILED
            raise

    @abstractmethod
    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]: ...

    async def llm_generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        return await llm_router.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            provider=self._llm_provider,
            model=self._llm_model,
            **kwargs,
        )

    async def request_approval(
        self,
        summary: str,
        state_snapshot: dict[str, Any] | None = None,
    ) -> Checkpoint:
        self.status = AgentStatus.WAITING_APPROVAL
        checkpoint = Checkpoint(
            id=str(uuid.uuid4()),
            session_id=self.session_id or "",
            agent_type=self.metadata.agent_type,
            state=state_snapshot or self.state,
        )
        self.state["checkpoint"] = {
            "id": checkpoint.id,
            "summary": summary,
            "created_at": checkpoint.created_at,
        }
        logger.info(f"Checkpoint {checkpoint.id} created, waiting approval")
        return checkpoint

    def resume_from_checkpoint(self, resolution: str, approved: bool) -> None:
        if approved:
            self.status = AgentStatus.RUNNING
            self.state.pop("checkpoint", None)
            if self.execution_node is not None:
                self.execution_node.status = "running"
        else:
            self.status = AgentStatus.PAUSED
            self.state["rejected_reason"] = resolution

    def handle_message(self, message: AgentMessage) -> dict[str, Any] | None:
        logger.debug(f"Agent {self.agent_id} received message: {message.type}")
        return None

    def reset(self) -> None:
        self.status = AgentStatus.IDLE
        self.state = {}
        self.execution_node = None
