"""Keep immutable file and diff evidence for task delivery attempts.

Revision ID: 0056_task_delivery_evidence
Revises: 0055_task_outcome_history
Create Date: 2026-11-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_task_delivery_evidence"
down_revision: str | None = "0055_task_outcome_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("task_deliveries", sa.Column("evidence_snapshot", sa.Text(), nullable=True))
    op.execute(
        """CREATE FUNCTION prevent_task_delivery_evidence_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.evidence_snapshot IS DISTINCT FROM OLD.evidence_snapshot THEN
                RAISE EXCEPTION 'task delivery evidence is immutable';
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_delivery_evidence_immutable
        BEFORE UPDATE ON task_deliveries
        FOR EACH ROW EXECUTE FUNCTION prevent_task_delivery_evidence_mutation()"""
    )


def downgrade() -> None:
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM task_deliveries WHERE evidence_snapshot IS NOT NULL) THEN
                RAISE EXCEPTION 'task delivery evidence exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS task_delivery_evidence_immutable ON task_deliveries")
    op.execute("DROP FUNCTION IF EXISTS prevent_task_delivery_evidence_mutation()")
    op.drop_column("task_deliveries", "evidence_snapshot")
