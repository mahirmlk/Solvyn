"""Bind acceptance criteria to tasks in the same tenant.

Revision ID: 0047_tac_same_tenant
Revises: 0046_workflow_execution_history
Create Date: 2026-11-07 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0047_tac_same_tenant"
down_revision: str | None = "0046_workflow_execution_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tasks are FORCE-RLS protected, so inspect existing rows one tenant at a
    # time. A task from another tenant is invisible here and is treated as a
    # mismatch before the new composite foreign key is installed.
    op.execute(
        """DO $$
        DECLARE tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);
                IF EXISTS (
                    SELECT 1
                    FROM task_acceptance_criteria criterion
                    LEFT JOIN tasks task_row
                      ON task_row.id = criterion.task_id
                     AND task_row.tenant_id = criterion.tenant_id
                    WHERE criterion.tenant_id = tenant_row.id
                      AND task_row.id IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'acceptance criteria contain cross-tenant task links for tenant %; repair rows before adding same-tenant integrity',
                        tenant_row.id;
                END IF;
            END LOOP;
        END;
        $$"""
    )

    op.drop_constraint(
        "task_acceptance_criteria_task_id_fkey",
        "task_acceptance_criteria",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_tac_same_tenant_task",
        "task_acceptance_criteria",
        "tasks",
        ["tenant_id", "task_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )

    op.execute("DROP POLICY tenant_isolation ON task_acceptance_criteria")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_acceptance_criteria
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_acceptance_criteria.task_id
                  AND task_row.tenant_id = task_acceptance_criteria.tenant_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_acceptance_criteria.task_id
                  AND task_row.tenant_id = task_acceptance_criteria.tenant_id
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_acceptance_criteria")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_acceptance_criteria
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )
    op.drop_constraint("fk_tac_same_tenant_task", "task_acceptance_criteria", type_="foreignkey")
    op.create_foreign_key(
        "task_acceptance_criteria_task_id_fkey",
        "task_acceptance_criteria",
        "tasks",
        ["task_id"],
        ["id"],
        ondelete="CASCADE",
    )
