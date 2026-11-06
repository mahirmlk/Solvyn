"""Apply tenant RLS to teammate tool invocation records.

Revision ID: 0020_tool_invocation_rls
Revises: 0019_postgres_rls_baseline
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_tool_invocation_rls"
down_revision: str | None = "0019_postgres_rls_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE teammate_tool_invocations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teammate_tool_invocations FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON teammate_tool_invocations
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON teammate_tool_invocations")
    op.execute("ALTER TABLE teammate_tool_invocations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teammate_tool_invocations DISABLE ROW LEVEL SECURITY")
