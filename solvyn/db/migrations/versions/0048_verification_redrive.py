"""Persist same-tenant verification dead-letter redrive lineage.

Revision ID: 0048_verification_redrive
Revises: 0047_tac_same_tenant
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0048_verification_redrive"
down_revision: str | None = "0047_tac_same_tenant"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verification_runs",
        sa.Column("redriven_from_run_id", UUID(as_uuid=True), nullable=True),
    )
    # The composite target and unique source constraint make redrive
    # tenant-safe and idempotent even when two admins submit simultaneously.
    op.create_unique_constraint(
        "uq_verification_runs_tenant_id_id", "verification_runs", ["tenant_id", "id"]
    )
    op.create_unique_constraint(
        "uq_verification_runs_redrive_source", "verification_runs", ["tenant_id", "redriven_from_run_id"]
    )
    op.create_foreign_key(
        "fk_verification_runs_redrive_same_tenant",
        "verification_runs",
        "verification_runs",
        ["tenant_id", "redriven_from_run_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_verification_runs_redrive_same_tenant", "verification_runs", type_="foreignkey")
    op.drop_constraint("uq_verification_runs_redrive_source", "verification_runs", type_="unique")
    op.drop_constraint("uq_verification_runs_tenant_id_id", "verification_runs", type_="unique")
    op.drop_column("verification_runs", "redriven_from_run_id")
