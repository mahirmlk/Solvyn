"""Apply tenant RLS to workflow sessions.

Revision ID: 0027_session_rls
Revises: 0026_teammate_memory_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027_session_rls"
down_revision: str | None = "0026_teammate_memory_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON sessions
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON sessions")
    op.execute("ALTER TABLE sessions NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE sessions DISABLE ROW LEVEL SECURITY")
