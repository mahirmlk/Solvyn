"""Allow approval-gated documentation integrity schedules.

Revision ID: 0059_documentation_profile
Revises: 0058_teammate_conversations
Create Date: 2026-11-13 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0059_documentation_profile"
down_revision: str | None = "0058_teammate_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_maintenance_schedules_verification_kind",
        "maintenance_schedules",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_schedules_verification_kind",
        "maintenance_schedules",
        "verification_kind IN ('test', 'build', 'lint', 'typecheck', 'security', 'documentation')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE maintenance_schedules SET verification_kind = 'lint' "
        "WHERE verification_kind = 'documentation'"
    )
    op.drop_constraint(
        "ck_maintenance_schedules_verification_kind",
        "maintenance_schedules",
        type_="check",
    )
    op.create_check_constraint(
        "ck_maintenance_schedules_verification_kind",
        "maintenance_schedules",
        "verification_kind IN ('test', 'build', 'lint', 'typecheck', 'security')",
    )
