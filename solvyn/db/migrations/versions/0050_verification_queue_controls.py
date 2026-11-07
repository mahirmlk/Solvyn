"""Add tenant-scoped organization verification queue pause controls.

Revision ID: 0050_verification_queue_controls
Revises: 0049_scheduled_checks
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0050_verification_queue_controls"
down_revision: str | None = "0049_scheduled_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_queue_controls",
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_by_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE",
            name="fk_verification_queue_controls_organization",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE",
            name="fk_verification_queue_controls_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"], ["users.id"], ondelete="SET NULL",
            name="fk_verification_queue_controls_updated_by_user",
        ),
        sa.PrimaryKeyConstraint("organization_id", name="pk_verification_queue_controls"),
    )
    op.create_index(
        "ix_verification_queue_controls_tenant_id",
        "verification_queue_controls",
        ["tenant_id"],
    )
    op.execute("ALTER TABLE verification_queue_controls ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verification_queue_controls FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON verification_queue_controls
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = verification_queue_controls.organization_id
                  AND organization_row.tenant_id = verification_queue_controls.tenant_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = verification_queue_controls.organization_id
                  AND organization_row.tenant_id = verification_queue_controls.tenant_id
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON verification_queue_controls")
    op.execute("ALTER TABLE verification_queue_controls NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verification_queue_controls DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_verification_queue_controls_tenant_id", table_name="verification_queue_controls")
    op.drop_table("verification_queue_controls")
