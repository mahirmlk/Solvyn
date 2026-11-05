"""Link human approvals to exact sandbox verification requests.

Revision ID: 0018_verification_approvals
Revises: 0017_task_deliveries
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0018_verification_approvals"
down_revision: str | None = "0017_task_deliveries"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("approvals", sa.Column("verification_run_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_approvals_verification_run_id_verification_runs",
        "approvals",
        "verification_runs",
        ["verification_run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_unique_constraint("uq_approvals_verification_run", "approvals", ["verification_run_id"])


def downgrade() -> None:
    op.drop_constraint("uq_approvals_verification_run", "approvals", type_="unique")
    op.drop_constraint("fk_approvals_verification_run_id_verification_runs", "approvals", type_="foreignkey")
    op.drop_column("approvals", "verification_run_id")
