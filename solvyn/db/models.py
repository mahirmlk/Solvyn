import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class TenantRole(StrEnum):
    """Roles for multi-tenant access control."""
    ADMIN = "admin"
    USER = "user"
    VIEWER = "viewer"


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(50), default=TenantRole.USER)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    sessions: Mapped[list["Session"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    agents: Mapped[list["AgentInstance"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(50), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tenant: Mapped["Tenant"] = relationship(back_populates="sessions")
    messages: Mapped[list["Message"]] = relationship(back_populates="session", cascade="all, delete-orphan")
    execution_graphs: Mapped[list["ExecutionGraph"]] = relationship(back_populates="session", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped["Session"] = relationship(back_populates="messages")


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="idle")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    tenant: Mapped["Tenant"] = relationship(back_populates="agents")


class Memory(Base):
    __tablename__ = "memory"
    __table_args__ = (UniqueConstraint("tenant_id", "agent_type", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Vector | None] = mapped_column(Vector(1536), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("execution_id", "node_id", name="uq_task_execution_node"),
        UniqueConstraint("tenant_id", "id", name="uq_tasks_tenant_id_id"),
        ForeignKeyConstraint(
            ("tenant_id", "parent_task_id"),
            ("tasks.tenant_id", "tasks.id"),
            name="fk_tasks_parent_same_tenant",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    assigned_agent: Mapped[str] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("execution_graphs.id"), nullable=True, index=True)
    node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    acceptance_criteria_defined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class TaskEvent(Base):
    """Append-only lifecycle history captured atomically with task writes."""

    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Deliberately no foreign keys: immutable history survives task/tenant
    # deletion and stores relationship IDs as historical snapshots.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, object] | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentEvaluationRun(Base):
    """Append-only, tenant-scoped summary of a versioned agent evaluation run."""

    __tablename__ = "agent_evaluation_runs"
    __table_args__ = (
        CheckConstraint("length(agent_type) > 0", name="ck_agent_evaluation_runs_agent_type_nonempty"),
        CheckConstraint("length(dataset_id) > 0", name="ck_agent_evaluation_runs_dataset_id_nonempty"),
        CheckConstraint("length(dataset_version) > 0", name="ck_agent_evaluation_runs_dataset_version_nonempty"),
        Index("ix_agent_evaluation_runs_tenant_created", "tenant_id", "created_at", "id"),
        Index("ix_agent_evaluation_runs_tenant_task_created", "tenant_id", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Historical task reference is intentionally not a foreign key so evaluation
    # history remains available after the task is removed.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Store bounded aggregate/case scores only; never prompts or model outputs.
    summary: Mapped[dict[str, object]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OrchestrationEvent(Base):
    """Append-only task, approval, handoff, and dependency history."""

    __tablename__ = "orchestration_events"
    __table_args__ = (
        CheckConstraint(
            "entity_type IN ('approval', 'handoff', 'acceptance_criterion', 'dependency', 'workflow_execution')",
            name="ck_orchestration_events_entity_type",
        ),
        CheckConstraint(
            "event_type IN ('created', 'status_changed', 'updated', 'deleted', 'claimed')",
            name="ck_orchestration_events_event_type",
        ),
        Index("ix_orchestration_events_tenant_task_time", "tenant_id", "task_id", "occurred_at", "id"),
        Index("ix_orchestration_events_tenant_execution_time", "tenant_id", "execution_id", "occurred_at", "id"),
        Index(
            "ix_orchestration_events_tenant_entity_time",
            "tenant_id",
            "entity_type",
            "entity_id",
            "occurred_at",
            "id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Historical references intentionally have no foreign keys so events
    # survive deletion of their task, approval, handoff, or tenant.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    payload: Mapped[dict[str, object] | None] = mapped_column(
        JSON().with_variant(JSONB, "postgresql"), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TaskWorkspace(Base):
    """Server-managed isolated source tree owned by one tenant task."""

    __tablename__ = "task_workspaces"
    __table_args__ = (UniqueConstraint("tenant_id", "task_id", name="uq_task_workspace_owner"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    repository_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    source_commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_paths: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskDelivery(Base):
    """Durable state for one digest-approved GitHub delivery attempt."""

    __tablename__ = "task_deliveries"
    __table_args__ = (
        UniqueConstraint("tenant_id", "task_id", "diff_digest", name="uq_task_delivery_digest"),
        CheckConstraint(
            "status IN ('starting', 'branch_created', 'committed', 'pull_request_open', 'unknown')",
            name="ck_task_deliveries_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("task_workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    repository_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False, index=True)
    source_commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_branch: Mapped[str] = mapped_column(String(255), nullable=False)
    diff_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    branch_name: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="starting", server_default="starting")
    branch_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pull_request_title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pull_request_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TeammateToolInvocation(Base):
    """Durable metadata for one model-requested workspace tool call."""

    __tablename__ = "teammate_tool_invocations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'denied')",
            name="ck_teammate_tool_invocations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("task_workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    teammate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teammate_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running", server_default="running")
    input_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    output_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VerificationRun(Base):
    """Durable, bounded output from a deterministic sandbox verification command."""

    __tablename__ = "verification_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_verification_runs_tenant_id_id"),
        UniqueConstraint("tenant_id", "redriven_from_run_id", name="uq_verification_runs_redrive_source"),
        UniqueConstraint(
            "tenant_id",
            "maintenance_schedule_id",
            "maintenance_scheduled_for",
            name="uq_verification_runs_maintenance_occurrence",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "redriven_from_run_id"),
            ("verification_runs.tenant_id", "verification_runs.id"),
            name="fk_verification_runs_redrive_same_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "maintenance_schedule_id"),
            ("maintenance_schedules.tenant_id", "maintenance_schedules.id"),
            name="fk_verification_runs_maintenance_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(maintenance_schedule_id IS NULL) = (maintenance_scheduled_for IS NULL)",
            name="ck_verification_runs_maintenance_occurrence_pair",
        ),
        CheckConstraint(
            """(
                dependency_lock_sha256 IS NULL
                AND dependency_wheelhouse_sha256 IS NULL
                AND dependency_runtime_fingerprint IS NULL
                AND dependency_uv_version IS NULL
                AND dependency_pip_version IS NULL
                AND dependency_package_count IS NULL
                AND dependency_wheelhouse_bytes IS NULL
            ) OR (
                dependency_lock_sha256 IS NOT NULL
                AND length(dependency_lock_sha256) = 64
                AND dependency_wheelhouse_sha256 IS NOT NULL
                AND length(dependency_wheelhouse_sha256) = 64
                AND dependency_runtime_fingerprint IS NOT NULL
                AND length(dependency_runtime_fingerprint) = 64
                AND dependency_uv_version IS NOT NULL
                AND dependency_pip_version IS NOT NULL
                AND dependency_package_count IS NOT NULL
                AND dependency_package_count > 0
                AND dependency_wheelhouse_bytes IS NOT NULL
                AND dependency_wheelhouse_bytes > 0
            )""",
            name="ck_verification_runs_dependency_evidence_pair",
        ),
        CheckConstraint(
            """(
                dependency_ecosystem IS NULL
                AND dependency_lockfile_sha256 IS NULL
                AND dependency_artifact_sha256 IS NULL
                AND dependency_artifact_bytes IS NULL
                AND dependency_artifact_package_count IS NULL
                AND dependency_environment_fingerprint IS NULL
                AND dependency_runtime_version IS NULL
                AND dependency_installer_version IS NULL
                AND dependency_runtime_platform IS NULL
                AND dependency_runtime_architecture IS NULL
                AND (dependency_lock_sha256 IS NULL OR dependency_wheelhouse_sha256 IS NOT NULL)
            ) OR (
                dependency_ecosystem = 'npm'
                AND dependency_lockfile_sha256 IS NOT NULL
                AND length(dependency_lockfile_sha256) = 64
                AND dependency_artifact_sha256 IS NOT NULL
                AND length(dependency_artifact_sha256) = 64
                AND dependency_artifact_bytes IS NOT NULL
                AND dependency_artifact_bytes > 0
                AND dependency_artifact_package_count IS NOT NULL
                AND dependency_artifact_package_count > 0
                AND dependency_environment_fingerprint IS NOT NULL
                AND length(dependency_environment_fingerprint) = 64
                AND dependency_runtime_version IS NOT NULL
                AND dependency_installer_version IS NOT NULL
                AND dependency_runtime_platform IS NOT NULL
                AND dependency_runtime_platform = 'linux'
                AND dependency_runtime_architecture IS NOT NULL
                AND dependency_runtime_architecture IN ('x64', 'arm64')
                AND dependency_lock_sha256 IS NULL
                AND dependency_wheelhouse_sha256 IS NULL
                AND dependency_runtime_fingerprint IS NULL
                AND dependency_uv_version IS NULL
                AND dependency_pip_version IS NULL
                AND dependency_package_count IS NULL
                AND dependency_wheelhouse_bytes IS NULL
            )""",
            name="ck_verification_runs_generic_dependency_evidence",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("execution_graphs.id", ondelete="CASCADE"), nullable=True, index=True)
    redriven_from_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    maintenance_schedule_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    maintenance_scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("task_workspaces.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="running", server_default="running")
    required_for_completion: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    output_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    runner_version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_lock_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_wheelhouse_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_runtime_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_uv_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dependency_pip_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dependency_package_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dependency_wheelhouse_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dependency_ecosystem: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dependency_lockfile_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_artifact_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_artifact_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dependency_artifact_package_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dependency_environment_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dependency_runtime_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dependency_installer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dependency_runtime_platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    dependency_runtime_architecture: Mapped[str | None] = mapped_column(String(16), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VerificationAttemptEvent(Base):
    """Append-only start and outcome evidence for one verification attempt."""

    __tablename__ = "verification_attempt_events"
    __table_args__ = (
        CheckConstraint("attempt_number >= 0", name="ck_verification_attempt_events_attempt_number"),
        CheckConstraint(
            "event_type IN ('started', 'finished', 'snapshot')",
            name="ck_verification_attempt_events_type",
        ),
        Index(
            "ix_verification_attempt_events_tenant_task_time",
            "tenant_id",
            "task_id",
            "occurred_at",
            "id",
        ),
        Index(
            "ix_verification_attempt_events_run_attempt",
            "verification_run_id",
            "attempt_number",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    verification_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    verification_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    output_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    stdout: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    stderr: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(10, 3), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TaskAcceptanceCriterion(Base):
    """Human-reviewable acceptance criterion for one tenant-scoped task."""

    __tablename__ = "task_acceptance_criteria"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name="ck_task_acceptance_criteria_status"),
        ForeignKeyConstraint(
            ("tenant_id", "task_id"),
            ("tasks.tenant_id", "tasks.id"),
            name="fk_tac_same_tenant_task",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending", server_default="pending")
    reviewer_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewer_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decision_workspace_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskDependency(Base):
    """Durable dependency edge between task records."""

    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency_edge"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    depends_on_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TaskHandoff(Base):
    """Auditable request/result record for a teammate delegation."""

    __tablename__ = "task_handoffs"
    __table_args__ = (
        UniqueConstraint("child_task_id", name="uq_task_handoff_child"),
        UniqueConstraint("tenant_id", "parent_task_id", "idempotency_key", name="uq_task_handoff_idempotency"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    parent_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    child_task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    teammate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teammate_profiles.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    delegated_from: Mapped[str] = mapped_column(String(100), nullable=False)
    delegated_to: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    input_data: Mapped[str] = mapped_column(Text, nullable=False)
    output_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExecutionGraph(Base):
    __tablename__ = "execution_graphs"
    __table_args__ = (
        UniqueConstraint("session_id", "id", name="uq_execution_graphs_session_id_id"),
        UniqueConstraint("session_id", "redriven_from_execution_id", name="uq_execution_graphs_redrive_source"),
        ForeignKeyConstraint(
            ("session_id", "redriven_from_execution_id"),
            ("execution_graphs.session_id", "execution_graphs.id"),
            name="fk_execution_graphs_redrive_same_session",
            ondelete="CASCADE",
        ),
        CheckConstraint("lease_reclaim_count >= 0", name="ck_execution_graphs_lease_reclaim_count"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False, index=True)
    graph_data: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="running")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    lease_owner: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_reclaim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    dead_lettered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_letter_reason: Mapped[str | None] = mapped_column(String(2_000), nullable=True)
    redriven_from_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    session: Mapped["Session"] = relationship(back_populates="execution_graphs")


class TokenUsage(Base):
    __tablename__ = "token_usage"
    __table_args__ = (
        Index("ix_token_usage_tenant_task_created_at", "tenant_id", "task_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=True
    )
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost: Mapped[float] = mapped_column(Numeric(12, 6), default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ApiKey(Base):
    """Per-tenant API key.

    Only the SHA-256 hash of the key is stored. The plaintext key is shown to
    the user exactly once at creation time. A short prefix (first 12 chars)
    is kept for display/identification in the admin UI. Revoked keys are
    kept (soft revoke) so audit trails remain intact.
    """

    __tablename__ = "api_keys"
    # P0-003 (now db-enforced): key lookups hit the unique index instead of a full-table scan.
    __table_args__ = (UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# Phase 1 — Product foundation (feature/product-foundation)
#
# Identity chain: User → Organization → Membership → Workspace/Project.
# Tenant remains the isolation boundary for all existing tables; new tables
# are additionally scoped by organization_id / project_id.
#
# Permission levels (enforced by the tool gateway, never by prompts):
#   READ_ONLY → PROPOSE_CHANGES → MODIFY_FILES → EXECUTE_COMMANDS →
#   CREATE_BRANCH → CREATE_PULL_REQUEST → MERGE_PULL_REQUEST → DEPLOY
# ---------------------------------------------------------------------------


class PermissionLevel(StrEnum):
    READ_ONLY = "READ_ONLY"
    PROPOSE_CHANGES = "PROPOSE_CHANGES"
    MODIFY_FILES = "MODIFY_FILES"
    EXECUTE_COMMANDS = "EXECUTE_COMMANDS"
    CREATE_BRANCH = "CREATE_BRANCH"
    CREATE_PULL_REQUEST = "CREATE_PULL_REQUEST"
    MERGE_PULL_REQUEST = "MERGE_PULL_REQUEST"
    DEPLOY = "DEPLOY"


class OrgRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


class TeammateRole(StrEnum):
    TECH_LEAD = "Tech Lead"
    SOFTWARE_ENGINEER = "Software Engineer"
    QA_ENGINEER = "QA Engineer"
    SECURITY_ENGINEER = "Security Engineer"


class TeammateStatus(StrEnum):
    ACTIVE = "active"
    IDLE = "idle"
    BUSY = "busy"
    PAUSED = "paused"
    DISABLED = "disabled"


class KnowledgeCategory(StrEnum):
    ARCHITECTURE = "architecture"
    CONVENTION = "convention"
    API_STANDARD = "api_standard"
    DB_CONVENTION = "db_convention"
    TECH_CHOICE = "tech_choice"
    TECHNICAL_DEBT = "technical_debt"
    PROJECT_INSTRUCTION = "project_instruction"
    DEPENDENCY = "dependency"
    DECISION = "decision"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"
    STALE = "stale"


class ApprovalKind(StrEnum):
    PLAN = "plan"
    COMMAND = "command"
    FILE_CHANGE = "file_change"
    PULL_REQUEST = "pull_request"
    MERGE = "merge"
    DEPLOY = "deploy"
    VERIFICATION_RUN = "verification_run"


class User(Base):
    """A human account. Passwords are Argon2id hashes (pwdlib)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_superuser: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memberships: Mapped[list["OrganizationMembership"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class RefreshToken(Base):
    """Hashed refresh tokens. Rotated on every use; plaintext never stored."""

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("refresh_tokens.id"), nullable=True)

    user: Mapped["User"] = relationship()


class Organization(Base):
    """Billing/isolation unit above projects. Maps onto Tenant during migration."""

    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    memberships: Mapped[list["OrganizationMembership"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    workspaces: Mapped[list["Workspace"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    projects: Mapped[list["Project"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class OrganizationMembership(Base):
    __tablename__ = "organization_memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), default=OrgRole.MEMBER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")


class Workspace(Base):
    """A grouping of projects owned by an organization."""

    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    organization: Mapped["Organization"] = relationship(back_populates="workspaces")
    projects: Mapped[list["Project"]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class Project(Base):
    """A software project. Connects to one or more repositories (Phase 2)."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    organization: Mapped["Organization"] = relationship(back_populates="projects")
    workspace: Mapped["Workspace | None"] = relationship(back_populates="projects")


class ProjectMembership(Base):
    """Per-project access; complements org membership for scoped projects."""

    __tablename__ = "project_memberships"
    __table_args__ = (UniqueConstraint("user_id", "project_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), default=OrgRole.MEMBER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Integration(Base):
    """External integration connection; sensitive credentials live separately."""

    __tablename__ = "integrations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)  # github, gitlab, ...
    external_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # device id / app id
    github_installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    github_account_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "external_id"),
        UniqueConstraint("github_installation_id", name="uq_integrations_github_installation_id"),
        CheckConstraint(
            "github_installation_id IS NULL OR github_installation_id > 0",
            name="ck_integrations_github_installation_id_positive",
        ),
        CheckConstraint(
            "(github_installation_id IS NULL) = (github_account_id IS NULL)",
            name="ck_integrations_github_installation_account_pair",
        ),
        CheckConstraint(
            "github_account_id IS NULL OR github_account_id > 0",
            name="ck_integrations_github_account_id_positive",
        ),
    )


class IntegrationCredential(Base):
    """Encrypted integration credentials; plaintext is only present in process memory."""

    __tablename__ = "integration_credentials"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    integration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("integrations.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    account_login: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GitHubDependabotAlertState(Base):
    """Latest sanitized lifecycle marker for one connected repository alert."""

    __tablename__ = "github_dependabot_alert_states"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "project_id", "repository_id", "alert_number",
            name="uq_github_dependabot_alert_states_identity",
        ),
        CheckConstraint("alert_number > 0", name="ck_github_dependabot_alert_states_number"),
        Index(
            "ix_github_dependabot_alert_states_project_updated",
            "organization_id",
            "project_id",
            "updated_at",
        ),
        CheckConstraint(
            "lifecycle_state IN ('open', 'dismissed', 'fixed')",
            name="ck_github_dependabot_alert_states_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    repository_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("repositories.id"), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    alert_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(50), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    details_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_reopen_marker: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_delivery_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProactiveEvent(Base):
    """Verified external engineering signals queued for human triage."""

    __tablename__ = "proactive_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "project_id", "delivery_id", name="uq_proactive_events_project_delivery"),
        UniqueConstraint("organization_id", "project_id", "source_event_id", name="uq_proactive_events_project_source_event"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_event_id: Mapped[str] = mapped_column(String(320), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_name: Mapped[str] = mapped_column(String(255), nullable=False)
    conclusion: Mapped[str] = mapped_column(String(50), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), default="")
    details_url: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(50), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubWorkItemState(Base):
    """Latest sanitized GitHub lifecycle state for a connected issue or PR."""

    __tablename__ = "github_work_item_states"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "project_id", "event_type", "repository_key", "item_number",
            name="uq_github_work_item_states_identity",
        ),
        CheckConstraint("event_type IN ('issues', 'pull_request')", name="ck_github_work_item_states_type"),
        CheckConstraint("lifecycle_state IN ('open', 'closed', 'merged')", name="ck_github_work_item_states_state"),
        CheckConstraint("item_number > 0", name="ck_github_work_item_states_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    item_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    details_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    provider_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ProactiveEventTransition(Base):
    """Sanitized immutable webhook history for GitHub issue/PR lifecycle changes."""

    __tablename__ = "proactive_event_transitions"
    __table_args__ = (
        UniqueConstraint("organization_id", "project_id", "delivery_id", name="uq_proactive_event_transitions_delivery"),
        CheckConstraint("event_type IN ('issues', 'pull_request')", name="ck_proactive_event_transitions_type"),
        CheckConstraint("lifecycle_state IN ('open', 'closed', 'merged')", name="ck_proactive_event_transitions_state"),
        CheckConstraint("item_number > 0", name="ck_proactive_event_transitions_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    repository_key: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_name: Mapped[str] = mapped_column(String(255), nullable=False)
    item_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(16), nullable=False)
    branch: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    details_url: Mapped[str] = mapped_column(String(512), default="", nullable=False)
    provider_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_to_current: Mapped[bool] = mapped_column(default=False, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MaintenanceSchedule(Base):
    """Bounded recurring maintenance triggers that queue human-review events."""

    __tablename__ = "maintenance_schedules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_maintenance_schedules_tenant_id_id"),
        ForeignKeyConstraint(
            ("tenant_id", "task_id"),
            ("tasks.tenant_id", "tasks.id"),
            name="fk_maintenance_schedules_task_same_tenant",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "maintenance_type IN ('code_quality', 'dependency_review', 'documentation_review')",
            name="ck_maintenance_schedules_type",
        ),
        CheckConstraint(
            "verification_kind IN ('test', 'build', 'lint', 'typecheck', 'security', 'documentation')",
            name="ck_maintenance_schedules_verification_kind",
        ),
        CheckConstraint("interval_days BETWEEN 1 AND 30", name="ck_maintenance_schedules_interval"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    maintenance_type: Mapped[str] = mapped_column(String(50), nullable=False)
    verification_kind: Mapped[str] = mapped_column(String(32), default="lint", server_default="lint", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class VerificationQueueControl(Base):
    """Persistent organization-level pause state for new verification claims."""

    __tablename__ = "verification_queue_controls"
    __table_args__ = (
        CheckConstraint("max_concurrent_runs BETWEEN 1 AND 5", name="ck_verification_queue_controls_concurrency"),
        CheckConstraint("max_runs_per_hour BETWEEN 1 AND 120", name="ck_verification_queue_controls_hourly_rate"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    max_concurrent_runs: Mapped[int] = mapped_column(Integer, default=2, server_default=text("2"), nullable=False)
    max_runs_per_hour: Mapped[int] = mapped_column(Integer, default=30, server_default=text("30"), nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowWorkerHeartbeat(Base):
    """Latest liveness record for one scheduler worker process."""

    __tablename__ = "workflow_worker_heartbeats"

    worker_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    worker_name: Mapped[str] = mapped_column(String(100), nullable=False)
    host_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class Repository(Base):
    """A connected git repository; tokens live in encrypted integration storage."""

    __tablename__ = "repositories"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    integration_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("integrations.id"), nullable=True)
    github_repository_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)  # owner/repo
    provider: Mapped[str] = mapped_column(String(50), default="github")
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    url: Mapped[str] = mapped_column(String(512), default="")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("project_id", "full_name"),
        CheckConstraint(
            "github_repository_id IS NULL OR github_repository_id > 0",
            name="ck_repositories_github_repository_id_positive",
        ),
        CheckConstraint(
            "github_repository_id IS NULL OR (provider = 'github' AND integration_id IS NOT NULL)",
            name="ck_repositories_github_repository_binding",
        ),
    )


class TeammateProfile(Base):
    """Persistent AI teammate. Replaces ephemeral AgentInstance for new code."""

    __tablename__ = "teammate_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(100), nullable=False)  # TeammateRole value
    description: Mapped[str] = mapped_column(Text, default="")
    responsibilities: Mapped[str] = mapped_column(Text, default="")
    system_instructions: Mapped[str] = mapped_column(Text, default="")
    agent_type: Mapped[str] = mapped_column(String(100), nullable=False)  # registry key
    is_enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(50), default=TeammateStatus.IDLE)
    model_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    max_tokens_per_run: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_run_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    permissions: Mapped[list["TeammatePermission"]] = relationship(back_populates="teammate", cascade="all, delete-orphan")
    project_assignments: Mapped[list["TeammateProjectAssignment"]] = relationship(back_populates="teammate", cascade="all, delete-orphan")


class TeammatePermission(Base):
    """Least-privilege grants. Checked by the tool gateway, not by prompts."""

    __tablename__ = "teammate_permissions"
    __table_args__ = (UniqueConstraint("teammate_id", "permission"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teammate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teammate_profiles.id"), nullable=False, index=True)
    permission: Mapped[str] = mapped_column(String(50), nullable=False)  # PermissionLevel value
    granted: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    teammate: Mapped["TeammateProfile"] = relationship(back_populates="permissions")


class TeammateProjectAssignment(Base):
    __tablename__ = "teammate_project_assignments"
    __table_args__ = (UniqueConstraint("teammate_id", "project_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    teammate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teammate_profiles.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    teammate: Mapped["TeammateProfile"] = relationship(back_populates="project_assignments")


class TeammateConversation(Base):
    """Durable conversation bound to one teammate and assigned project."""

    __tablename__ = "teammate_conversations"
    __table_args__ = (
        UniqueConstraint("session_id"),
        UniqueConstraint("tenant_id", "root_task_id", name="uq_teammate_conversations_root_task"),
        ForeignKeyConstraint(
            ("tenant_id", "root_task_id"),
            ("tasks.tenant_id", "tasks.id"),
            name="fk_teammate_conversations_root_task_same_tenant",
        ),
        Index("ix_teammate_conversations_scope", "tenant_id", "teammate_id", "project_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    teammate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teammate_profiles.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("sessions.id"), nullable=False, index=True)
    root_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class TeammateMemory(Base):
    """Durable notes scoped to one teammate and one assigned project."""

    __tablename__ = "teammate_memory"
    __table_args__ = (
        UniqueConstraint("teammate_id", "project_id", "key", name="uq_teammate_memory_scope_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    teammate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("teammate_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class ProjectKnowledge(Base):
    """Validated, scoped engineering knowledge. Never stores secrets.

    Writes must pass schema validation + secret screening at the service layer.
    """

    __tablename__ = "project_knowledge"
    __table_args__ = (UniqueConstraint("project_id", "category", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Approval(Base):
    """Human checkpoint: plan, command, file change, PR, merge, or deploy."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("task_handoff_id", name="uq_approvals_task_handoff"),
        UniqueConstraint("verification_run_id", name="uq_approvals_verification_run"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=True)
    teammate_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("teammate_profiles.id"), nullable=True)
    task_handoff_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("task_handoffs.id", ondelete="SET NULL"), nullable=True
    )
    verification_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("verification_runs.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    status: Mapped[str] = mapped_column(String(50), default=ApprovalStatus.PENDING)
    requested_by: Mapped[str] = mapped_column(String(255), default="")  # teammate name / user display
    decided_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
