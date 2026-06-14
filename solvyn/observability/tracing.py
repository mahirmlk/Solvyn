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
        # When True, ``create_trace`` / ``add_span`` also emit observations
        # to LangFuse (if it is enabled). Mirroring is a no-op when LangFuse
        # is not configured, so it is safe to leave on in development.
        self._mirror_to_langfuse: bool = False

    def enable(self, api_key: str | None = None) -> None:
        self._enabled = True
        logger.info("Tracing enabled")

    def disable(self) -> None:
        self._enabled = False

    def set_langfuse_mirror(self, enabled: bool) -> None:
        """Toggle mirroring of in-memory traces to LangFuse.

        When enabled, every call to :meth:`create_trace` and
        :meth:`add_span` will also emit the corresponding observation to
        LangFuse (when LangFuse is itself configured). Mirroring is best
        effort and never breaks the in-memory trace path.
        """
        self._mirror_to_langfuse = bool(enabled)
        logger.info("LangFuse mirroring %s", "enabled" if self._mirror_to_langfuse else "disabled")

    def is_langfuse_mirroring(self) -> bool:
        return self._mirror_to_langfuse

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
        if self._mirror_to_langfuse:
            self._emit_trace_to_langfuse(
                trace_id=trace_id,
                name=name,
                agent_type=agent_type,
                session_id=session_id,
                tenant_id=tenant_id,
                metadata=metadata,
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
        if self._mirror_to_langfuse:
            self._emit_span_to_langfuse(
                trace=trace,
                name=name,
                span_type=span_type,
                input_data=input_data,
                output_data=output_data,
                duration_ms=duration_ms,
                token_count=token_count,
                model=model,
                provider=provider,
                error=error,
            )

    @staticmethod
    def _emit_trace_to_langfuse(
        trace_id: str,
        name: str,
        agent_type: str,
        session_id: str | None,
        tenant_id: str,
        metadata: dict[str, Any] | None,
    ) -> None:
        try:
            from solvyn.observability.langfuse import record_event

            merged_metadata: dict[str, Any] = {
                "agent_type": agent_type,
                "solvyn_trace_id": trace_id,
            }
            if metadata:
                merged_metadata.update(metadata)
            record_event(
                name=name,
                as_type="span",
                metadata=merged_metadata,
                tenant_id=tenant_id,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 — mirroring must never break tracing
            logger.debug("LangFuse trace mirror failed: %s", exc)

    @staticmethod
    def _emit_span_to_langfuse(
        trace: dict[str, Any],
        name: str,
        span_type: str,
        input_data: dict[str, Any] | None,
        output_data: dict[str, Any] | None,
        duration_ms: float,
        token_count: int,
        model: str,
        provider: str,
        error: str | None,
    ) -> None:
        try:
            from solvyn.observability.langfuse import record_event

            as_type = "generation" if span_type == "llm" else "span"
            metadata: dict[str, Any] = {
                "agent_type": trace.get("agent_type", ""),
                "span_type": span_type,
                "duration_ms": duration_ms,
                "solvyn_trace_id": trace.get("id", ""),
            }
            if model:
                metadata["model"] = model
            if provider:
                metadata["provider"] = provider
            usage_details: dict[str, int] | None = None
            if token_count:
                usage_details = {"total": int(token_count)}
            record_event(
                name=name,
                as_type=as_type,
                input_data=input_data,
                output_data=output_data,
                metadata=metadata,
                tenant_id=str(trace.get("tenant_id", "default")),
                session_id=trace.get("session_id"),
                model=model,
                usage_details=usage_details,
                error=error,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("LangFuse span mirror failed: %s", exc)

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        return next((t for t in self._traces if t["id"] == trace_id), None)

    def list_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._traces)[-limit:]

    def get_session_traces(self, session_id: str) -> list[dict[str, Any]]:
        return [t for t in self._traces if t.get("session_id") == session_id]


tracing_manager = TracingManager()
