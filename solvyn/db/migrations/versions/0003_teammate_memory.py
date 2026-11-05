"""Add project-scoped persistent teammate memory.

Revision ID: 0003_teammate_memory
Revises: 0002_phase1_product_foundation
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0003_teammate_memory"
down_revision: str | None = "0002_phase1_product_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teammate_memory",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("teammate_id", UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("teammate_id", "project_id", "key", name="uq_teammate_memory_scope_key"),
    )
    op.create_index("ix_teammate_memory_tenant_id", "teammate_memory", ["tenant_id"])
    op.create_index("ix_teammate_memory_teammate_id", "teammate_memory", ["teammate_id"])
    op.create_index("ix_teammate_memory_project_id", "teammate_memory", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_teammate_memory_project_id", table_name="teammate_memory")
    op.drop_index("ix_teammate_memory_teammate_id", table_name="teammate_memory")
    op.drop_index("ix_teammate_memory_tenant_id", table_name="teammate_memory")
    op.drop_table("teammate_memory")
