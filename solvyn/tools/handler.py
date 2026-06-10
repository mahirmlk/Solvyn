from __future__ import annotations

import logging
from typing import Any

from solvyn.tools.exceptions import ToolAuthError, ToolExecutionError, ToolRateLimitError, ToolTimeoutError

logger = logging.getLogger(__name__)

RETRYABLE_ERRORS: tuple[type[Exception], ...] = (ToolRateLimitError, ToolTimeoutError)
NON_RETRYABLE_ERRORS: tuple[type[Exception], ...] = (ToolAuthError, ToolExecutionError)


class ToolErrorHandler:
    """Maps tool errors to structured responses for agent consumption."""

    def handle_error(self, error: Exception, tool_name: str) -> dict[str, Any]:
        if isinstance(error, ToolRateLimitError):
            return {
                "success": False,
                "error_type": "rate_limit",
                "tool_name": tool_name,
                "message": str(error),
                "retryable": True,
                "retry_after": error.retry_after,
                "details": error.details,
            }
        if isinstance(error, ToolTimeoutError):
            return {
                "success": False,
                "error_type": "timeout",
                "tool_name": tool_name,
                "message": str(error),
                "retryable": True,
                "retry_after": min(error.timeout_seconds, 30.0),
                "details": error.details,
            }
        if isinstance(error, ToolAuthError):
            return {
                "success": False,
                "error_type": "auth",
                "tool_name": tool_name,
                "message": str(error),
                "retryable": False,
                "auth_provider": error.auth_provider,
                "details": error.details,
            }
        if isinstance(error, ToolExecutionError):
            return {
                "success": False,
                "error_type": "execution",
                "tool_name": tool_name,
                "message": str(error),
                "retryable": False,
                "details": error.details,
            }
        return {
            "success": False,
            "error_type": "unknown",
            "tool_name": tool_name,
            "message": str(error),
            "retryable": False,
            "details": {},
        }

    def is_retryable(self, error: Exception) -> bool:
        return isinstance(error, RETRYABLE_ERRORS)

    def format_for_agent(self, error_result: dict[str, Any]) -> str:
        retryable = "RETRYABLE" if error_result.get("retryable") else "FATAL"
        return f"[{retryable}] Tool '{error_result['tool_name']}' failed: [{error_result['error_type']}] {error_result['message']}"


tool_error_handler = ToolErrorHandler()
