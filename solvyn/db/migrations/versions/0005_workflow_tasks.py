"""Persist workflow nodes as tenant-scoped tasks with dependencies.

Revision ID: 0005_workflow_tasks
Revises: 0004_workflow_execution_leases
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0005_workflow_tasks"
down_revision: str | None = "0004_workflow_execution_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("execution_id", UUID(as_uuid=True), sa.ForeignKey("execution_graphs.id"), nullable=True))
    op.add_column("tasks", sa.Column("node_id", sa.String(length=100), nullable=True))
    op.add_column("tasks", sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("tasks", sa.Column("max_attempts", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("result", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("tasks", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_tasks_execution_id", "tasks", ["execution_id"])
    op.create_unique_constraint("uq_task_execution_node", "tasks", ["execution_id", "node_id"])

    op.create_table(
        "task_dependencies",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("depends_on_task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("task_id", "depends_on_task_id", name="uq_task_dependency_edge"),
    )
    op.create_index("ix_task_dependencies_tenant_id", "task_dependencies", ["tenant_id"])
    op.create_index("ix_task_dependencies_task_id", "task_dependencies", ["task_id"])
    op.create_index("ix_task_dependencies_depends_on_task_id", "task_dependencies", ["depends_on_task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_dependencies_depends_on_task_id", table_name="task_dependencies")
    op.drop_index("ix_task_dependencies_task_id", table_name="task_dependencies")
    op.drop_index("ix_task_dependencies_tenant_id", table_name="task_dependencies")
    op.drop_table("task_dependencies")
    op.drop_constraint("uq_task_execution_node", "tasks", type_="unique")
    op.drop_index("ix_tasks_execution_id", table_name="tasks")
    op.drop_column("tasks", "completed_at")
    op.drop_column("tasks", "started_at")
    op.drop_column("tasks", "scheduled_at")
    op.drop_column("tasks", "error")
    op.drop_column("tasks", "result")
    op.drop_column("tasks", "max_attempts")
    op.drop_column("tasks", "attempt_count")
    op.drop_column("tasks", "node_id")
    op.drop_column("tasks", "execution_id")
