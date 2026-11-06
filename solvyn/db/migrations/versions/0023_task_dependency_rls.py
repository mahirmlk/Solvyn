"""Apply tenant RLS to task dependency edges.

Revision ID: 0023_task_dependency_rls
Revises: 0022_workflow_worker_heartbeats
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023_task_dependency_rls"
down_revision: str | None = "0022_workflow_worker_heartbeats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_dependencies ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_dependencies FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_dependencies
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_dependencies.task_id
                  AND task_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM tasks dependency_row
                WHERE dependency_row.id = task_dependencies.depends_on_task_id
                  AND dependency_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_dependencies.task_id
                  AND task_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM tasks dependency_row
                WHERE dependency_row.id = task_dependencies.depends_on_task_id
                  AND dependency_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_dependencies")
    op.execute("ALTER TABLE task_dependencies NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_dependencies DISABLE ROW LEVEL SECURITY")
