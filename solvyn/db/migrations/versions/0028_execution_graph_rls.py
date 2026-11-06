"""Apply tenant RLS to workflow execution graphs.

Revision ID: 0028_execution_graph_rls
Revises: 0027_session_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_execution_graph_rls"
down_revision: str | None = "0027_session_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE execution_graphs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE execution_graphs FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON execution_graphs
        USING (
            EXISTS (
                SELECT 1 FROM sessions
                WHERE sessions.id = execution_graphs.session_id
                  AND sessions.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM sessions
                WHERE sessions.id = execution_graphs.session_id
                  AND sessions.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON execution_graphs")
    op.execute("ALTER TABLE execution_graphs NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE execution_graphs DISABLE ROW LEVEL SECURITY")
