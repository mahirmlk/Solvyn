from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentCapability(StrEnum):
    CODE_GENERATION = "code_generation"
    CODE_REVIEW = "code_review"
    ARCHITECTURE = "architecture"
    TESTING = "testing"
    SECURITY = "security"
    DEVOPS = "devops"
    DOCUMENTATION = "documentation"
    PRODUCT_MANAGEMENT = "product_management"
    SYSTEM_DESIGN = "system_design"


@dataclass
class AgentMetadata:
    agent_type: str
    name: str
    description: str
    capabilities: list[AgentCapability]
    version: str = "0.1.0"
    config_schema: dict[str, Any] | None = None


@dataclass
class AgentMessage:
    id: str
    from_agent: str
    to_agent: str | None
    session_id: str
    tenant_id: str
    type: str
    content: dict[str, Any]
    correlation_id: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class Checkpoint:
    id: str
    session_id: str
    agent_type: str
    state: dict[str, Any]
    status: str = "pending"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    resolved_at: str | None = None
    resolution: str | None = None


@dataclass
class ExecutionNode:
    id: str
    agent_type: str
    status: str = "pending"
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None


@dataclass
class ExecutionEdge:
    source: str
    target: str
    condition: str | None = None


@dataclass
class ExecutionGraph:
    id: str
    session_id: str
    nodes: list[ExecutionNode]
    edges: list[ExecutionEdge]
    status: str = "running"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
