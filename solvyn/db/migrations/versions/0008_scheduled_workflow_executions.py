"""Add durable scheduled workflow executions.

Revision ID: 0008_scheduled_workflows
Revises: 0007_task_handoff_approvals
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_scheduled_workflows"
down_revision: str | None = "0007_task_handoff_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("execution_graphs", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_execution_graphs_scheduled_at", "execution_graphs", ["scheduled_at"])


def downgrade() -> None:
    op.drop_index("ix_execution_graphs_scheduled_at", table_name="execution_graphs")
    op.drop_column("execution_graphs", "scheduled_at")
