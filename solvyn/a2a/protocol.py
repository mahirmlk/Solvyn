from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class A2AMessageType(StrEnum):
    TASK_REQUEST = "task_request"
    TASK_RESPONSE = "task_response"
    STATUS_UPDATE = "status_update"
    ERROR_REPORT = "error_report"
    DELEGATION = "delegation"


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class A2AEnvelope:
    """Standard envelope for all A2A messages."""

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    message_type: A2AMessageType = A2AMessageType.TASK_REQUEST
    from_agent: str = ""
    to_agent: str = ""
    session_id: str = ""
    tenant_id: str = ""
    correlation_id: str = ""
    version: str = "1.0.0"
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "message_type": self.message_type,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "correlation_id": self.correlation_id,
            "version": self.version,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> A2AEnvelope:
        return cls(
            message_id=data.get("message_id", str(uuid.uuid4())),
            message_type=data.get("message_type", A2AMessageType.TASK_REQUEST),
            from_agent=data.get("from_agent", ""),
            to_agent=data.get("to_agent", ""),
            session_id=data.get("session_id", ""),
            tenant_id=data.get("tenant_id", ""),
            correlation_id=data.get("correlation_id", ""),
            version=data.get("version", "1.0.0"),
            timestamp=data.get("timestamp", datetime.now(UTC).isoformat()),
            metadata=data.get("metadata", {}),
        )


@dataclass
class TaskRequest:
    """Request to delegate a task to another agent."""

    task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_type: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    timeout_seconds: float = 300.0
    retry_policy: dict[str, Any] = field(default_factory=lambda: {"max_retries": 3, "backoff_factor": 2.0})
    required_capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "input_data": self.input_data,
            "priority": self.priority,
            "timeout_seconds": self.timeout_seconds,
            "retry_policy": self.retry_policy,
            "required_capabilities": self.required_capabilities,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRequest:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class TaskResponse:
    """Response from an agent after completing a task."""

    task_id: str = ""
    status: TaskStatus = TaskStatus.COMPLETED
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: float = 0.0
    token_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "output_data": self.output_data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "token_count": self.token_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskResponse:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class StatusUpdate:
    """Status update from an agent mid-execution."""

    task_id: str = ""
    status: TaskStatus = TaskStatus.RUNNING
    progress: float = 0.0
    message: str = ""
    intermediate_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "intermediate_data": self.intermediate_data,
        }


@dataclass
class ErrorReport:
    """Structured error report from a failed agent execution."""

    task_id: str = ""
    error_type: str = ""
    error_code: str = ""
    message: str = ""
    stack_trace: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "error_type": self.error_type,
            "error_code": self.error_code,
            "message": self.message,
            "stack_trace": self.stack_trace,
            "context": self.context,
            "retryable": self.retryable,
        }


@dataclass
class TaskDelegation:
    """Delegation of a sub-task from one agent to another."""

    parent_task_id: str = ""
    sub_task_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    delegated_to: str = ""
    delegated_from: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_task_id": self.parent_task_id,
            "sub_task_id": self.sub_task_id,
            "delegated_to": self.delegated_to,
            "delegated_from": self.delegated_from,
            "input_data": self.input_data,
            "reason": self.reason,
        }


@dataclass
class A2AResponse:
    """Typed wrapper around A2A response payloads."""

    success: bool = True
    envelope: A2AEnvelope = field(default_factory=A2AEnvelope)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "envelope": self.envelope.to_dict(),
            "payload": self.payload,
        }
