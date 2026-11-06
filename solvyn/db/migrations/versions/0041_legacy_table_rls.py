"""Protect legacy tenant-owned tables with forced RLS.

Revision ID: 0041_legacy_table_rls
Revises: 0040_task_event_history
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041_legacy_table_rls"
down_revision: str | None = "0040_task_event_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Artifacts carry tenant_id directly but may also reference a session.
    # Validate that link while each owning tenant's forced session policy is
    # active, so malformed historical rows fail migration instead of becoming
    # inaccessible or crossing tenant boundaries.
    op.execute(
        """DO $$
        DECLARE
            tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);

                IF EXISTS (
                    SELECT 1
                    FROM artifacts artifact_row
                    WHERE artifact_row.tenant_id = tenant_row.id
                      AND artifact_row.session_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions session_row
                          WHERE session_row.id = artifact_row.session_id
                            AND session_row.tenant_id = tenant_row.id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'artifacts contain a session outside their tenant for tenant %; repair rows before enabling RLS',
                        tenant_row.id;
                END IF;
            END LOOP;
        END;
        $$"""
    )

    for table in ("agent_instances", "artifacts", "messages", "project_memberships"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """CREATE POLICY tenant_isolation ON agent_instances
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON artifacts
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM sessions session_row
                    WHERE session_row.id = artifacts.session_id
                      AND session_row.tenant_id = artifacts.tenant_id
                )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM sessions session_row
                    WHERE session_row.id = artifacts.session_id
                      AND session_row.tenant_id = artifacts.tenant_id
                )
            )
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON messages
        USING (
            EXISTS (
                SELECT 1 FROM sessions session_row
                WHERE session_row.id = messages.session_id
                  AND session_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM sessions session_row
                WHERE session_row.id = messages.session_id
                  AND session_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON project_memberships
        USING (
            EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = project_memberships.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = project_memberships.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )


def downgrade() -> None:
    for table in ("project_memberships", "messages", "artifacts", "agent_instances"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
