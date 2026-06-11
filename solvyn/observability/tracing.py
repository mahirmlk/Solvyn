import logging
import uuid
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class TracingManager:
    def __init__(self) -> None:
        self._enabled = False
        self._project_name = "solvyn"
        self._traces: deque[dict[str, Any]] = deque(maxlen=10000)

    def enable(self, api_key: str | None = None) -> None:
        self._enabled = True
        logger.info("Tracing enabled")

    def disable(self) -> None:
        self._enabled = False

    def create_trace(
        self,
        name: str,
        agent_type: str,
        session_id: str | None = None,
        tenant_id: str = "default",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        trace_id = f"trace_{uuid.uuid4()}"
        self._traces.append(
            {
                "id": trace_id,
                "name": name,
                "agent_type": agent_type,
                "session_id": session_id,
                "tenant_id": tenant_id,
                "metadata": metadata or {},
                "spans": [],
            }
        )
        return trace_id

    def add_span(
        self,
        trace_id: str,
        name: str,
        span_type: str = "llm",
        input_data: dict[str, Any] | None = None,
        output_data: dict[str, Any] | None = None,
        duration_ms: float = 0.0,
        token_count: int = 0,
        model: str = "",
        provider: str = "",
        error: str | None = None,
    ) -> None:
        trace = next((t for t in self._traces if t["id"] == trace_id), None)
        if trace is None:
            return
        if len(trace["spans"]) >= 1000:
            return
        trace["spans"].append(
            {
                "name": name,
                "type": span_type,
                "input": input_data,
                "output": output_data,
                "duration_ms": duration_ms,
                "token_count": token_count,
                "model": model,
                "provider": provider,
                "error": error,
            }
        )

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return next((t for t in self._traces if t["id"] == trace_id), None)

    def list_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._traces)[-limit:]

    def get_session_traces(self, session_id: str) -> list[dict[str, Any]]:
        return [t for t in self._traces if t.get("session_id") == session_id]


tracing_manager = TracingManager()
