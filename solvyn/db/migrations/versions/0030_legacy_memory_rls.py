"""Apply tenant RLS to legacy agent memory, checkpoints, and audit records.

Revision ID: 0030_legacy_memory_rls
Revises: 0029_task_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030_legacy_memory_rls"
down_revision: str | None = "0029_task_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """DO $$
        DECLARE scoped_tenant uuid;
        BEGIN
            FOR scoped_tenant IN SELECT id FROM tenants LOOP
                PERFORM set_config('app.tenant_id', scoped_tenant::text, true);
                IF EXISTS (
                    SELECT 1 FROM memory memory_row
                    WHERE memory_row.tenant_id = scoped_tenant
                      AND memory_row.session_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions session_row
                          WHERE session_row.id = memory_row.session_id
                            AND session_row.tenant_id = memory_row.tenant_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'memory for tenant % contains a session owned by another tenant; repair rows before enabling memory RLS',
                        scoped_tenant;
                END IF;
            END LOOP;
        END;
        $$"""
    )
    op.execute("ALTER TABLE memory ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON memory
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL OR EXISTS (
                    SELECT 1 FROM sessions
                    WHERE sessions.id = memory.session_id
                      AND sessions.tenant_id = memory.tenant_id
                )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL OR EXISTS (
                    SELECT 1 FROM sessions
                    WHERE sessions.id = memory.session_id
                      AND sessions.tenant_id = memory.tenant_id
                )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON memory")
    op.execute("ALTER TABLE memory NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory DISABLE ROW LEVEL SECURITY")
