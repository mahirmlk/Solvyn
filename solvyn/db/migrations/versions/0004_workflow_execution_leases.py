"""Add worker leases for recoverable workflow execution.

Revision ID: 0004_workflow_execution_leases
Revises: 0003_teammate_memory
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0004_workflow_execution_leases"
down_revision: str | None = "0003_teammate_memory"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("execution_graphs", sa.Column("lease_owner", UUID(as_uuid=True), nullable=True))
    op.add_column("execution_graphs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("execution_graphs", "lease_expires_at")
    op.drop_column("execution_graphs", "lease_owner")
