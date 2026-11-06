"""Apply tenant RLS to verification run evidence.

Revision ID: 0024_verification_run_rls
Revises: 0023_task_dependency_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024_verification_run_rls"
down_revision: str | None = "0023_task_dependency_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE verification_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verification_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON verification_runs
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_runs.task_id
                  AND task_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM task_workspaces workspace_row
                WHERE workspace_row.id = verification_runs.workspace_id
                  AND workspace_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND workspace_row.task_id = verification_runs.task_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_runs.task_id
                  AND task_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM task_workspaces workspace_row
                WHERE workspace_row.id = verification_runs.workspace_id
                  AND workspace_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND workspace_row.task_id = verification_runs.task_id
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON verification_runs")
    op.execute("ALTER TABLE verification_runs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verification_runs DISABLE ROW LEVEL SECURITY")
