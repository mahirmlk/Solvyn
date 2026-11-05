"""Persist bounded, content-free teammate workspace tool audit records.

Revision ID: 0016_teammate_tool_calls
Revises: 0015_task_ws_repo_source
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0016_teammate_tool_calls"
down_revision: str | None = "0015_task_ws_repo_source"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "teammate_tool_invocations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("task_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("teammate_id", UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("input_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_bytes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'denied')",
            name="ck_teammate_tool_invocations_status",
        ),
    )
    op.create_index("ix_teammate_tool_invocations_tenant_id", "teammate_tool_invocations", ["tenant_id"])
    op.create_index("ix_teammate_tool_invocations_task_id", "teammate_tool_invocations", ["task_id"])
    op.create_index("ix_teammate_tool_invocations_workspace_id", "teammate_tool_invocations", ["workspace_id"])
    op.create_index("ix_teammate_tool_invocations_teammate_id", "teammate_tool_invocations", ["teammate_id"])
    op.create_index(
        "ix_teammate_tool_invocations_task_started",
        "teammate_tool_invocations",
        ["tenant_id", "task_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_teammate_tool_invocations_task_started", table_name="teammate_tool_invocations")
    op.drop_index("ix_teammate_tool_invocations_teammate_id", table_name="teammate_tool_invocations")
    op.drop_index("ix_teammate_tool_invocations_workspace_id", table_name="teammate_tool_invocations")
    op.drop_index("ix_teammate_tool_invocations_task_id", table_name="teammate_tool_invocations")
    op.drop_index("ix_teammate_tool_invocations_tenant_id", table_name="teammate_tool_invocations")
    op.drop_table("teammate_tool_invocations")
