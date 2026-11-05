"""Add structured task acceptance criteria and human decisions.

Revision ID: 0010_task_acceptance_criteria
Revises: 0009_verification_evidence
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0010_task_acceptance_criteria"
down_revision: str | None = "0009_verification_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("acceptance_criteria_defined", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_table(
        "task_acceptance_criteria",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("reviewer_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reviewer_organization_id", UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'rejected')", name="ck_task_acceptance_criteria_status"),
    )
    op.create_index("ix_task_acceptance_criteria_tenant_id", "task_acceptance_criteria", ["tenant_id"])
    op.create_index("ix_task_acceptance_criteria_task_id", "task_acceptance_criteria", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_acceptance_criteria_task_id", table_name="task_acceptance_criteria")
    op.drop_index("ix_task_acceptance_criteria_tenant_id", table_name="task_acceptance_criteria")
    op.drop_table("task_acceptance_criteria")
    op.drop_column("tasks", "acceptance_criteria_defined")
