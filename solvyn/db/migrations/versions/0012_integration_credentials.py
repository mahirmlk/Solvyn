"""Store encrypted organization integration credentials.

Revision ID: 0012_integration_credentials
Revises: 0011_verification_source_digest
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0012_integration_credentials"
down_revision: str | None = "0011_verification_source_digest"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "integration_credentials",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "integration_id",
            UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("account_login", sa.String(length=255), nullable=False),
        sa.Column("updated_by", UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
def downgrade() -> None:
    op.drop_table("integration_credentials")
