"""Mark verification runs that are required before task completion.

Revision ID: 0057_task_completion_verification_gates
Revises: 0056_task_delivery_evidence
Create Date: 2026-11-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_task_completion_verification_gates"
down_revision: str | None = "0056_task_delivery_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "verification_runs",
        sa.Column("required_for_completion", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "task_acceptance_criteria",
        sa.Column("decision_workspace_digest", sa.String(length=64), nullable=True),
    )
    # Affected tasks stay in review on downgrade: the status transition is
    # durable history and restoring completion could certify unreviewed output.
    op.execute(
        """DO $$
        DECLARE tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);
                UPDATE tasks AS task
                SET status = 'review_required', completed_at = NULL
                WHERE task.tenant_id = tenant_row.id
                  AND task.status = 'completed'
                  AND (
                      EXISTS (
                          SELECT 1 FROM task_workspaces AS workspace
                          WHERE workspace.tenant_id = task.tenant_id AND workspace.task_id = task.id
                            AND (
                                task.acceptance_criteria_defined IS TRUE
                                OR EXISTS (
                                    SELECT 1 FROM task_handoffs AS handoff
                                    WHERE handoff.tenant_id = task.tenant_id
                                      AND handoff.child_task_id = task.id
                                )
                            )
                      )
                      OR (
                          task.acceptance_criteria_defined IS TRUE
                          AND EXISTS (
                              SELECT 1 FROM task_acceptance_criteria AS criterion
                              WHERE criterion.tenant_id = task.tenant_id AND criterion.task_id = task.id
                                AND criterion.status <> 'accepted'
                          )
                      )
                  );
            END LOOP;
        END;
        $$"""
    )


def downgrade() -> None:
    op.execute(
        """DO $$
        DECLARE tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);
                IF EXISTS (
                    SELECT 1 FROM verification_runs WHERE required_for_completion IS TRUE
                ) THEN
                    RAISE EXCEPTION 'required verification history exists; preserve it before downgrading';
                END IF;
                IF EXISTS (
                    SELECT 1 FROM task_acceptance_criteria WHERE decision_workspace_digest IS NOT NULL
                ) THEN
                    RAISE EXCEPTION 'workspace-bound acceptance decisions exist; preserve them before downgrading';
                END IF;
            END LOOP;
        END;
        $$"""
    )
    op.drop_column("task_acceptance_criteria", "decision_workspace_digest")
    op.drop_column("verification_runs", "required_for_completion")
