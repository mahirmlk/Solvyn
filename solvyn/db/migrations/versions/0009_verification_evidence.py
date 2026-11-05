"""Persist task workspaces and deterministic verification evidence.

Revision ID: 0009_verification_evidence
Revises: 0008_scheduled_workflows
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0009_verification_evidence"
down_revision: str | None = "0008_scheduled_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_workspaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_task_workspace_owner"),
    )
    op.create_index("ix_task_workspaces_tenant_id", "task_workspaces", ["tenant_id"])
    op.create_index("ix_task_workspaces_task_id", "task_workspaces", ["task_id"])

    op.create_table(
        "verification_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("execution_id", UUID(as_uuid=True), sa.ForeignKey("execution_graphs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("task_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="running", nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("output_truncated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stdout", sa.Text(), server_default="", nullable=False),
        sa.Column("stderr", sa.Text(), server_default="", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("runner_version", sa.String(length=100), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_verification_runs_tenant_id", "verification_runs", ["tenant_id"])
    op.create_index("ix_verification_runs_task_id", "verification_runs", ["task_id"])
    op.create_index("ix_verification_runs_execution_id", "verification_runs", ["execution_id"])


def downgrade() -> None:
    op.drop_index("ix_verification_runs_execution_id", table_name="verification_runs")
    op.drop_index("ix_verification_runs_task_id", table_name="verification_runs")
    op.drop_index("ix_verification_runs_tenant_id", table_name="verification_runs")
    op.drop_table("verification_runs")
    op.drop_index("ix_task_workspaces_task_id", table_name="task_workspaces")
    op.drop_index("ix_task_workspaces_tenant_id", table_name="task_workspaces")
    op.drop_table("task_workspaces")
