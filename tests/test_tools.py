from __future__ import annotations

import pytest

from solvyn.tools.exceptions import (
    ToolAuthError,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolRateLimitError,
    ToolTimeoutError,
    ToolValidationError,
)
from solvyn.tools.handler import ToolErrorHandler, tool_error_handler


class TestToolExceptions:
    def test_tool_error_base(self) -> None:
        err = ToolError("test error", tool_name="test-tool")
        assert str(err) == "test error"
        assert err.tool_name == "test-tool"
        assert err.details == {}

    def test_tool_error_with_details(self) -> None:
        err = ToolError("err", details={"key": "val"})
        assert err.details == {"key": "val"}

    def test_tool_execution_error(self) -> None:
        err = ToolExecutionError("exec failed", tool_name="my-tool")
        assert err.tool_name == "my-tool"
        assert isinstance(err, ToolError)

    def test_tool_timeout_error(self) -> None:
        err = ToolTimeoutError("timeout", tool_name="slow-tool", timeout_seconds=30.0)
        assert err.timeout_seconds == 30.0
        assert err.details["timeout_seconds"] == 30.0

    def test_tool_rate_limit_error(self) -> None:
        err = ToolRateLimitError("rate limited", tool_name="api-tool", retry_after=60.0)
        assert err.retry_after == 60.0
        assert err.details["retry_after"] == 60.0

    def test_tool_auth_error(self) -> None:
        err = ToolAuthError("unauthorized", tool_name="secure-tool", auth_provider="github")
        assert err.auth_provider == "github"
        assert err.details["auth_provider"] == "github"

    def test_tool_validation_error(self) -> None:
        err = ToolValidationError("invalid", tool_name="tool", validation_errors=[{"field": "name"}])
        assert len(err.validation_errors) == 1

    def test_tool_not_found_error(self) -> None:
        err = ToolNotFoundError("missing-tool")
        assert "missing-tool" in str(err)
        assert err.tool_name == "missing-tool"


class TestToolErrorHandler:
    def test_handle_rate_limit(self) -> None:
        err = ToolRateLimitError("rate limited", tool_name="t", retry_after=30.0)
        result = tool_error_handler.handle_error(err, "t")
        assert result["success"] is False
        assert result["error_type"] == "rate_limit"
        assert result["retryable"] is True
        assert result["retry_after"] == 30.0

    def test_handle_timeout(self) -> None:
        err = ToolTimeoutError("timeout", tool_name="t", timeout_seconds=10.0)
        result = tool_error_handler.handle_error(err, "t")
        assert result["error_type"] == "timeout"
        assert result["retryable"] is True

    def test_handle_auth_error(self) -> None:
        err = ToolAuthError("unauthorized", tool_name="t")
        result = tool_error_handler.handle_error(err, "t")
        assert result["error_type"] == "auth"
        assert result["retryable"] is False

    def test_handle_execution_error(self) -> None:
        err = ToolExecutionError("failed", tool_name="t")
        result = tool_error_handler.handle_error(err, "t")
        assert result["error_type"] == "execution"
        assert result["retryable"] is False

    def test_handle_unknown_error(self) -> None:
        err = ValueError("something weird")
        result = tool_error_handler.handle_error(err, "t")
        assert result["error_type"] == "unknown"
        assert result["retryable"] is False

    def test_is_retryable(self) -> None:
        assert tool_error_handler.is_retryable(ToolRateLimitError("r"))
        assert tool_error_handler.is_retryable(ToolTimeoutError("t"))
        assert not tool_error_handler.is_retryable(ToolAuthError("a"))
        assert not tool_error_handler.is_retryable(ToolExecutionError("e"))

    def test_format_for_agent(self) -> None:
        result = {"tool_name": "t", "error_type": "rate_limit", "message": "limited", "retryable": True}
        formatted = tool_error_handler.format_for_agent(result)
        assert "RETRYABLE" in formatted
        assert "rate_limit" in formatted
