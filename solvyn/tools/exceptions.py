from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """Base exception for all tool-related errors."""

    def __init__(self, message: str, tool_name: str = "", details: dict[str, Any] | None = None) -> None:
        self.tool_name = tool_name
        self.details = details or {}
        super().__init__(message)


class ToolExecutionError(ToolError):
    """Raised when a tool fails during execution (non-retryable logic error)."""

    def __init__(self, message: str, tool_name: str = "", details: dict[str, Any] | None = None) -> None:
        super().__init__(message, tool_name, details)


class ToolTimeoutError(ToolError):
    """Raised when a tool execution exceeds its time limit."""

    def __init__(self, message: str, tool_name: str = "", timeout_seconds: float = 0.0) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(message, tool_name, {"timeout_seconds": timeout_seconds})


class ToolRateLimitError(ToolError):
    """Raised when a tool or external API rate limit is hit. Retryable after backoff."""

    def __init__(self, message: str, tool_name: str = "", retry_after: float = 60.0) -> None:
        self.retry_after = retry_after
        super().__init__(message, tool_name, {"retry_after": retry_after})


class ToolAuthError(ToolError):
    """Raised when a tool fails due to authentication/authorization issues. Not retryable."""

    def __init__(self, message: str, tool_name: str = "", auth_provider: str = "") -> None:
        self.auth_provider = auth_provider
        super().__init__(message, tool_name, {"auth_provider": auth_provider})


class ToolValidationError(ToolError):
    """Raised when tool input fails schema validation."""

    def __init__(self, message: str, tool_name: str = "", validation_errors: list[dict[str, Any]] | None = None) -> None:
        self.validation_errors = validation_errors or []
        super().__init__(message, tool_name, {"validation_errors": self.validation_errors})


class ToolNotFoundError(ToolError):
    """Raised when a requested tool is not registered in the MCP registry."""

    def __init__(self, tool_name: str) -> None:
        super().__init__(f"Tool '{tool_name}' not found in MCP registry", tool_name)
