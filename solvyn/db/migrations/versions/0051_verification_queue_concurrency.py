"""Add organization verification concurrency limits.

Revision ID: 0051_verification_queue_concurrency
Revises: 0050_verification_queue_controls
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0051_verification_queue_concurrency"
down_revision: str | None = "0050_verification_queue_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verification_queue_controls",
        sa.Column("max_concurrent_runs", sa.Integer(), server_default=sa.text("2"), nullable=False),
    )
    op.create_check_constraint(
        "ck_verification_queue_controls_concurrency",
        "verification_queue_controls",
        "max_concurrent_runs BETWEEN 1 AND 5",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_verification_queue_controls_concurrency",
        "verification_queue_controls",
        type_="check",
    )
    op.drop_column("verification_queue_controls", "max_concurrent_runs")
