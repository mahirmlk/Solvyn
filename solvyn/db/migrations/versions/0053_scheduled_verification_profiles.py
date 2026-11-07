"""Select a fixed verification profile for scheduled quality checks.

Revision ID: 0053_sched_verif_profiles
Revises: 0052_verif_queue_rate_limit
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053_sched_verif_profiles"
down_revision: str | None = "0052_verif_queue_rate_limit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "maintenance_schedules",
        sa.Column("verification_kind", sa.String(length=32), server_default="lint", nullable=False),
    )
    op.create_check_constraint(
        "ck_maintenance_schedules_verification_kind",
        "maintenance_schedules",
        "verification_kind IN ('test', 'build', 'lint', 'typecheck', 'security')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_maintenance_schedules_verification_kind",
        "maintenance_schedules",
        type_="check",
    )
    op.drop_column("maintenance_schedules", "verification_kind")
