"""Apply tenant RLS to task workspace metadata.

Revision ID: 0021_task_workspace_rls
Revises: 0020_tool_invocation_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021_task_workspace_rls"
down_revision: str | None = "0020_tool_invocation_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE task_workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_workspaces
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_workspaces")
    op.execute("ALTER TABLE task_workspaces NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_workspaces DISABLE ROW LEVEL SECURITY")
