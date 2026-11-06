"""Add fenced leases and bounded retries to verification runs.

Revision ID: 0042_verification_run_recovery
Revises: 0041_legacy_table_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0042_verification_run_recovery"
down_revision: str | None = "0041_legacy_table_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verification_runs",
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "verification_runs",
        sa.Column("attempt_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("verification_runs", sa.Column("lease_owner", UUID(as_uuid=True), nullable=True))
    op.add_column(
        "verification_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_verification_runs_recovery",
        "verification_runs",
        ["status", "attempt_started_at", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_verification_runs_recovery", table_name="verification_runs")
    op.drop_column("verification_runs", "lease_expires_at")
    op.drop_column("verification_runs", "lease_owner")
    op.drop_column("verification_runs", "attempt_started_at")
    op.drop_column("verification_runs", "attempt_count")
