"""Persist structured teammate task handoffs.

Revision ID: 0006_structured_task_handoffs
Revises: 0005_workflow_tasks
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0006_structured_task_handoffs"
down_revision: str | None = "0005_workflow_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_handoffs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("parent_task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("child_task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", UUID(as_uuid=True), nullable=False),
        sa.Column("teammate_id", UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("delegated_from", sa.String(length=100), nullable=False),
        sa.Column("delegated_to", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("input_data", sa.Text(), nullable=False),
        sa.Column("output_data", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("lease_owner", UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("child_task_id", name="uq_task_handoff_child"),
        sa.UniqueConstraint("tenant_id", "parent_task_id", "idempotency_key", name="uq_task_handoff_idempotency"),
    )
    op.create_index("ix_task_handoffs_tenant_id", "task_handoffs", ["tenant_id"])
    op.create_index("ix_task_handoffs_parent_task_id", "task_handoffs", ["parent_task_id"])
    op.create_index("ix_task_handoffs_teammate_id", "task_handoffs", ["teammate_id"])
    op.create_index("ix_task_handoffs_project_id", "task_handoffs", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_task_handoffs_project_id", table_name="task_handoffs")
    op.drop_index("ix_task_handoffs_teammate_id", table_name="task_handoffs")
    op.drop_index("ix_task_handoffs_parent_task_id", table_name="task_handoffs")
    op.drop_index("ix_task_handoffs_tenant_id", table_name="task_handoffs")
    op.drop_table("task_handoffs")
