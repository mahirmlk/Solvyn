"""Apply tenant RLS to persistent teammate memory.

Revision ID: 0026_teammate_memory_rls
Revises: 0025_task_handoff_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0026_teammate_memory_rls"
down_revision: str | None = "0025_task_handoff_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE teammate_memory ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teammate_memory FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON teammate_memory
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                WHERE teammate_row.id = teammate_memory.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = teammate_memory.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = teammate_memory.project_id
                WHERE teammate_row.id = teammate_memory.teammate_id
                  AND teammate_row.organization_id = project_row.organization_id
            )
            AND EXISTS (
                SELECT 1 FROM teammate_project_assignments assignment_row
                WHERE assignment_row.teammate_id = teammate_memory.teammate_id
                  AND assignment_row.project_id = teammate_memory.project_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                WHERE teammate_row.id = teammate_memory.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = teammate_memory.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = teammate_memory.project_id
                WHERE teammate_row.id = teammate_memory.teammate_id
                  AND teammate_row.organization_id = project_row.organization_id
            )
            AND EXISTS (
                SELECT 1 FROM teammate_project_assignments assignment_row
                WHERE assignment_row.teammate_id = teammate_memory.teammate_id
                  AND assignment_row.project_id = teammate_memory.project_id
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON teammate_memory")
    op.execute("ALTER TABLE teammate_memory NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teammate_memory DISABLE ROW LEVEL SECURITY")
