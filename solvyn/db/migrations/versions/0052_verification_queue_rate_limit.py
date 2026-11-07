"""Add organization verification hourly rate limits.

Revision ID: 0052_verif_queue_rate_limit
Revises: 0051_verif_queue_concurrency
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052_verif_queue_rate_limit"
down_revision: str | None = "0051_verif_queue_concurrency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verification_queue_controls",
        sa.Column("max_runs_per_hour", sa.Integer(), server_default=sa.text("30"), nullable=False),
    )
    op.create_check_constraint(
        "ck_verification_queue_controls_hourly_rate",
        "verification_queue_controls",
        "max_runs_per_hour BETWEEN 1 AND 120",
    )
    op.create_index(
        "ix_verification_attempt_events_tenant_started_time",
        "verification_attempt_events",
        ["tenant_id", "occurred_at", "verification_run_id"],
        postgresql_where=sa.text("event_type = 'started'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_verification_attempt_events_tenant_started_time",
        table_name="verification_attempt_events",
    )
    op.drop_constraint(
        "ck_verification_queue_controls_hourly_rate",
        "verification_queue_controls",
        type_="check",
    )
    op.drop_column("verification_queue_controls", "max_runs_per_hour")
