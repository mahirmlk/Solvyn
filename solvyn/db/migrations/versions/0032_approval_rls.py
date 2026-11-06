"""Apply tenant and organization RLS to human approval records.

Revision ID: 0032_approval_rls
Revises: 0031_token_usage_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032_approval_rls"
down_revision: str | None = "0031_token_usage_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """DO $$
        DECLARE approval_scope record;
        BEGIN
            FOR approval_scope IN
                SELECT approvals.id, organizations.tenant_id
                FROM approvals
                LEFT JOIN organizations ON organizations.id = approvals.organization_id
            LOOP
                IF approval_scope.tenant_id IS NULL THEN
                    RAISE EXCEPTION
                        'approval % references a missing organization; repair the approval before enabling approval RLS',
                        approval_scope.id;
                END IF;

                PERFORM set_config('app.tenant_id', approval_scope.tenant_id::text, true);
                IF EXISTS (
                    SELECT 1 FROM approvals approval_row
                    WHERE approval_row.id = approval_scope.id
                      AND (
                        (approval_row.project_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM projects project_row
                            WHERE project_row.id = approval_row.project_id
                              AND project_row.organization_id = approval_row.organization_id
                              AND project_row.tenant_id = approval_scope.tenant_id
                        ))
                        OR (approval_row.teammate_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM teammate_profiles teammate_row
                            WHERE teammate_row.id = approval_row.teammate_id
                              AND teammate_row.organization_id = approval_row.organization_id
                              AND teammate_row.tenant_id = approval_scope.tenant_id
                        ))
                        OR (approval_row.task_handoff_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM task_handoffs handoff_row
                            JOIN projects project_row ON project_row.id = handoff_row.project_id
                            JOIN teammate_profiles teammate_row ON teammate_row.id = handoff_row.teammate_id
                            WHERE handoff_row.id = approval_row.task_handoff_id
                              AND handoff_row.tenant_id = approval_scope.tenant_id
                              AND handoff_row.project_id = approval_row.project_id
                              AND handoff_row.teammate_id = approval_row.teammate_id
                              AND project_row.organization_id = approval_row.organization_id
                              AND teammate_row.organization_id = approval_row.organization_id
                        ))
                        OR (approval_row.verification_run_id IS NOT NULL AND NOT EXISTS (
                            SELECT 1 FROM verification_runs run_row
                            JOIN task_handoffs handoff_row ON handoff_row.child_task_id = run_row.task_id
                            JOIN projects project_row ON project_row.id = handoff_row.project_id
                            WHERE run_row.id = approval_row.verification_run_id
                              AND run_row.tenant_id = approval_scope.tenant_id
                              AND handoff_row.tenant_id = approval_scope.tenant_id
                              AND handoff_row.project_id = approval_row.project_id
                              AND handoff_row.teammate_id = approval_row.teammate_id
                              AND project_row.organization_id = approval_row.organization_id
                        ))
                      )
                ) THEN
                    RAISE EXCEPTION
                        'approval % has linked rows outside its organization; repair before enabling approval RLS',
                        approval_scope.id;
                END IF;
            END LOOP;
        END;
        $$"""
    )
    op.execute("ALTER TABLE approvals ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approvals FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON approvals
        USING (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = approvals.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND (
                approvals.project_id IS NULL OR EXISTS (
                    SELECT 1 FROM projects project_row
                    WHERE project_row.id = approvals.project_id
                      AND project_row.organization_id = approvals.organization_id
                      AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
            )
            AND (
                approvals.teammate_id IS NULL OR EXISTS (
                    SELECT 1 FROM teammate_profiles teammate_row
                    WHERE teammate_row.id = approvals.teammate_id
                      AND teammate_row.organization_id = approvals.organization_id
                      AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
            )
            AND (
                approvals.task_handoff_id IS NULL OR EXISTS (
                    SELECT 1 FROM task_handoffs handoff_row
                    JOIN projects project_row ON project_row.id = handoff_row.project_id
                    JOIN teammate_profiles teammate_row ON teammate_row.id = handoff_row.teammate_id
                    WHERE handoff_row.id = approvals.task_handoff_id
                      AND handoff_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                      AND handoff_row.project_id = approvals.project_id
                      AND handoff_row.teammate_id = approvals.teammate_id
                      AND project_row.organization_id = approvals.organization_id
                      AND teammate_row.organization_id = approvals.organization_id
                )
            )
            AND (
                approvals.verification_run_id IS NULL OR EXISTS (
                    SELECT 1 FROM verification_runs run_row
                    JOIN task_handoffs handoff_row ON handoff_row.child_task_id = run_row.task_id
                    JOIN projects project_row ON project_row.id = handoff_row.project_id
                    WHERE run_row.id = approvals.verification_run_id
                      AND run_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                      AND handoff_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                      AND handoff_row.project_id = approvals.project_id
                      AND handoff_row.teammate_id = approvals.teammate_id
                      AND project_row.organization_id = approvals.organization_id
                )
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = approvals.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND (
                approvals.project_id IS NULL OR EXISTS (
                    SELECT 1 FROM projects project_row
                    WHERE project_row.id = approvals.project_id
                      AND project_row.organization_id = approvals.organization_id
                      AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
            )
            AND (
                approvals.teammate_id IS NULL OR EXISTS (
                    SELECT 1 FROM teammate_profiles teammate_row
                    WHERE teammate_row.id = approvals.teammate_id
                      AND teammate_row.organization_id = approvals.organization_id
                      AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
            )
            AND (
                approvals.task_handoff_id IS NULL OR EXISTS (
                    SELECT 1 FROM task_handoffs handoff_row
                    JOIN projects project_row ON project_row.id = handoff_row.project_id
                    JOIN teammate_profiles teammate_row ON teammate_row.id = handoff_row.teammate_id
                    WHERE handoff_row.id = approvals.task_handoff_id
                      AND handoff_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                      AND handoff_row.project_id = approvals.project_id
                      AND handoff_row.teammate_id = approvals.teammate_id
                      AND project_row.organization_id = approvals.organization_id
                      AND teammate_row.organization_id = approvals.organization_id
                )
            )
            AND (
                approvals.verification_run_id IS NULL OR EXISTS (
                    SELECT 1 FROM verification_runs run_row
                    JOIN task_handoffs handoff_row ON handoff_row.child_task_id = run_row.task_id
                    JOIN projects project_row ON project_row.id = handoff_row.project_id
                    WHERE run_row.id = approvals.verification_run_id
                      AND run_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                      AND handoff_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                      AND handoff_row.project_id = approvals.project_id
                      AND handoff_row.teammate_id = approvals.teammate_id
                      AND project_row.organization_id = approvals.organization_id
                )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON approvals")
    op.execute("ALTER TABLE approvals NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE approvals DISABLE ROW LEVEL SECURITY")
