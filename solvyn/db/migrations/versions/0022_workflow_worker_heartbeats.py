"""Persist workflow scheduler worker health.

Revision ID: 0022_workflow_worker_heartbeats
Revises: 0021_task_workspace_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_workflow_worker_heartbeats"
down_revision: str | None = "0021_task_workspace_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_worker_heartbeats",
        sa.Column("worker_id", sa.Uuid(), primary_key=True),
        sa.Column("worker_name", sa.String(length=100), nullable=False),
        sa.Column("host_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workflow_worker_heartbeats_last_seen_at", "workflow_worker_heartbeats", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_worker_heartbeats_last_seen_at", table_name="workflow_worker_heartbeats")
    op.drop_table("workflow_worker_heartbeats")
