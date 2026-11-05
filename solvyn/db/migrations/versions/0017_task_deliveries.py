"""Persist digest-approved GitHub delivery attempts on workflow tasks.

Revision ID: 0017_task_deliveries
Revises: 0016_teammate_tool_calls
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0017_task_deliveries"
down_revision: str | None = "0016_teammate_tool_calls"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "task_deliveries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", UUID(as_uuid=True), sa.ForeignKey("task_workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("repository_id", UUID(as_uuid=True), sa.ForeignKey("repositories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_commit_sha", sa.String(length=64), nullable=False),
        sa.Column("base_branch", sa.String(length=255), nullable=False),
        sa.Column("diff_digest", sa.String(length=64), nullable=False),
        sa.Column("branch_name", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="starting", nullable=False),
        sa.Column("branch_sha", sa.String(length=64), nullable=True),
        sa.Column("commit_sha", sa.String(length=64), nullable=True),
        sa.Column("pull_request_title", sa.String(length=256), nullable=True),
        sa.Column("pull_request_number", sa.Integer(), nullable=True),
        sa.Column("pull_request_url", sa.String(length=512), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "task_id", "diff_digest", name="uq_task_delivery_digest"),
        sa.CheckConstraint(
            "status IN ('starting', 'branch_created', 'committed', 'pull_request_open', 'unknown')",
            name="ck_task_deliveries_status",
        ),
    )
    op.create_index("ix_task_deliveries_tenant_id", "task_deliveries", ["tenant_id"])
    op.create_index("ix_task_deliveries_task_id", "task_deliveries", ["task_id"])
    op.create_index("ix_task_deliveries_workspace_id", "task_deliveries", ["workspace_id"])
    op.create_index("ix_task_deliveries_repository_id", "task_deliveries", ["repository_id"])
    op.create_index("ix_task_deliveries_task_created", "task_deliveries", ["tenant_id", "task_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_task_deliveries_task_created", table_name="task_deliveries")
    op.drop_index("ix_task_deliveries_repository_id", table_name="task_deliveries")
    op.drop_index("ix_task_deliveries_workspace_id", table_name="task_deliveries")
    op.drop_index("ix_task_deliveries_task_id", table_name="task_deliveries")
    op.drop_index("ix_task_deliveries_tenant_id", table_name="task_deliveries")
    op.drop_table("task_deliveries")
