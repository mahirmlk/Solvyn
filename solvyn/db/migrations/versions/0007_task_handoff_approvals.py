"""Link teammate handoffs to durable human approvals.

Revision ID: 0007_task_handoff_approvals
Revises: 0006_structured_task_handoffs
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0007_task_handoff_approvals"
down_revision: str | None = "0006_structured_task_handoffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "task_handoffs",
        sa.Column("requires_approval", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("approvals", sa.Column("task_handoff_id", UUID(as_uuid=True), nullable=True))
    op.create_unique_constraint("uq_approvals_task_handoff", "approvals", ["task_handoff_id"])
    op.create_foreign_key(
        "fk_approvals_task_handoff_id_task_handoffs",
        "approvals",
        "task_handoffs",
        ["task_handoff_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_approvals_task_handoff_id_task_handoffs", "approvals", type_="foreignkey")
    op.drop_constraint("uq_approvals_task_handoff", "approvals", type_="unique")
    op.drop_column("approvals", "task_handoff_id")
    op.drop_column("task_handoffs", "requires_approval")
