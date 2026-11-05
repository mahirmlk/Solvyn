from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from solvyn.tools.exceptions import ToolExecutionError


class MCPToolParameter(BaseModel):
    name: str
    type: str
    description: str = ""
    required: bool = True
    default: Any = None
    enum: list[Any] | None = None


class MCPToolSchema(BaseModel):
    """Standardized schema for an MCP-exposed tool."""

    name: str
    description: str = ""
    version: str = "1.0.0"
    parameters: list[MCPToolParameter] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    timeout_seconds: float = 300.0
    # Descriptive metadata only. Runtime isolation is provided separately by
    # solvyn.sandbox; setting this field does not wrap a handler in a sandbox.
    sandboxed: bool = False

    def to_prompt_schema(self) -> str:
        lines = [f"Tool: {self.name}", f"Description: {self.description}"]
        if self.parameters:
            lines.append("Parameters:")
            for p in self.parameters:
                req = "required" if p.required else "optional"
                lines.append(f"  - {p.name} ({p.type}, {req}): {p.description}")
        return "\n".join(lines)


@dataclass
class MCPTool:
    """A registered MCP tool with execution capabilities."""

    schema: MCPToolSchema
    handler: Any = None
    auth_required: bool = False
    auth_provider: str = ""
    sandboxed: bool = False
    _call_count: int = field(default=0, init=False)
    _total_latency_ms: float = field(default=0.0, init=False)
    _error_count: int = field(default=0, init=False)
    _last_called_at: float = field(default=0.0, init=False)

    async def execute(self, parameters: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
        start = time.monotonic()
        self._call_count += 1
        self._last_called_at = time.monotonic()

        validated = self._validate_parameters(parameters)

        if self.schema.sandboxed or self.sandboxed:
            raise ToolExecutionError(
                "Sandboxed tool dispatch is unavailable; refusing to execute the handler directly",
                tool_name=self.schema.name,
            )

        if self.handler is None:
            raise NotImplementedError(f"Tool '{self.schema.name}' has no handler registered")

        try:
            result = self.handler(validated, context or {})
            if asyncio.iscoroutine(result):
                result = await asyncio.wait_for(result, timeout=self.schema.timeout_seconds)
            latency = (time.monotonic() - start) * 1000
            self._total_latency_ms += latency
            return {
                "success": True,
                "tool_name": self.schema.name,
                "result": result,
                "latency_ms": latency,
            }
        except Exception:
            latency = (time.monotonic() - start) * 1000
            self._total_latency_ms += latency
            self._error_count += 1
            raise

    def _validate_parameters(self, parameters: dict[str, Any]) -> dict[str, Any]:
        validated: dict[str, Any] = dict(parameters)
        for param in self.schema.parameters:
            if param.name not in validated:
                if param.required and param.default is not None:
                    validated[param.name] = param.default
                elif param.required:
                    from solvyn.tools.exceptions import ToolValidationError
                    raise ToolValidationError(
                        f"Missing required parameter '{param.name}' for tool '{self.schema.name}'",
                        tool_name=self.schema.name,
                    )
                elif param.default is not None:
                    validated[param.name] = param.default
        return validated

    def get_stats(self) -> dict[str, Any]:
        avg_latency = self._total_latency_ms / self._call_count if self._call_count > 0 else 0.0
        return {
            "tool_name": self.schema.name,
            "call_count": self._call_count,
            "error_count": self._error_count,
            "avg_latency_ms": avg_latency,
            "last_called_at": self._last_called_at,
        }
