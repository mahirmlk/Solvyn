"""Record pre-run workspace digests for verification runs.

Revision ID: 0011_verification_source_digest
Revises: 0010_task_acceptance_criteria
Create Date: 2026-11-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_verification_source_digest"
down_revision: str | None = "0010_task_acceptance_criteria"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("verification_runs", sa.Column("source_digest", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("verification_runs", "source_digest")
