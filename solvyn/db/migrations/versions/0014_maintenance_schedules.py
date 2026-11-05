"""Add recurring, human-reviewed maintenance triggers.

Revision ID: 0014_maintenance_schedules
Revises: 0013_proactive_events
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0014_maintenance_schedules"
down_revision: str | None = "0013_proactive_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "maintenance_schedules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("maintenance_type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "maintenance_type IN ('code_quality', 'dependency_review', 'documentation_review')",
            name="ck_maintenance_schedules_type",
        ),
        sa.CheckConstraint("interval_days BETWEEN 1 AND 30", name="ck_maintenance_schedules_interval"),
    )
    op.create_index("ix_maintenance_schedules_organization_id", "maintenance_schedules", ["organization_id"])
    op.create_index("ix_maintenance_schedules_project_id", "maintenance_schedules", ["project_id"])
    op.create_index("ix_maintenance_schedules_next_run_at", "maintenance_schedules", ["next_run_at"])


def downgrade() -> None:
    op.drop_index("ix_maintenance_schedules_next_run_at", table_name="maintenance_schedules")
    op.drop_index("ix_maintenance_schedules_project_id", table_name="maintenance_schedules")
    op.drop_index("ix_maintenance_schedules_organization_id", table_name="maintenance_schedules")
    op.drop_table("maintenance_schedules")
