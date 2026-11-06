"""Apply tenant RLS to structured task handoffs.

Revision ID: 0025_task_handoff_rls
Revises: 0024_verification_run_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025_task_handoff_rls"
down_revision: str | None = "0024_verification_run_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_handoffs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_handoffs FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_handoffs
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks parent_row
                WHERE parent_row.id = task_handoffs.parent_task_id
                  AND parent_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM tasks child_row
                WHERE child_row.id = task_handoffs.child_task_id
                  AND child_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND child_row.parent_task_id = task_handoffs.parent_task_id
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = task_handoffs.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = task_handoffs.project_id
                WHERE teammate_row.id = task_handoffs.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND teammate_row.organization_id = project_row.organization_id
            )
            AND EXISTS (
                SELECT 1 FROM teammate_project_assignments assignment_row
                WHERE assignment_row.teammate_id = task_handoffs.teammate_id
                  AND assignment_row.project_id = task_handoffs.project_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks parent_row
                WHERE parent_row.id = task_handoffs.parent_task_id
                  AND parent_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM tasks child_row
                WHERE child_row.id = task_handoffs.child_task_id
                  AND child_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND child_row.parent_task_id = task_handoffs.parent_task_id
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = task_handoffs.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = task_handoffs.project_id
                WHERE teammate_row.id = task_handoffs.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND teammate_row.organization_id = project_row.organization_id
            )
            AND EXISTS (
                SELECT 1 FROM teammate_project_assignments assignment_row
                WHERE assignment_row.teammate_id = task_handoffs.teammate_id
                  AND assignment_row.project_id = task_handoffs.project_id
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_handoffs")
    op.execute("ALTER TABLE task_handoffs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_handoffs DISABLE ROW LEVEL SECURITY")
