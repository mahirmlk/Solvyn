"""Apply tenant RLS to organizations and workspaces.

Revision ID: 0037_organization_workspace_rls
Revises: 0036_teammate_authorization_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037_organization_workspace_rls"
down_revision: str | None = "0036_teammate_authorization_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """DO $$
        DECLARE
            tenant_row record;
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM workspaces workspace_row
                LEFT JOIN organizations organization_row
                    ON organization_row.id = workspace_row.organization_id
                WHERE organization_row.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'workspaces contain a missing organization link; repair rows before enabling RLS';
            END IF;

            -- Projects already have forced RLS, so validate their workspace
            -- links under each tenant's scope before tightening organizations.
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);

                IF EXISTS (
                    SELECT 1
                    FROM projects project_row
                    JOIN workspaces workspace_row
                        ON workspace_row.id = project_row.workspace_id
                    WHERE project_row.tenant_id = tenant_row.id
                      AND workspace_row.organization_id <> project_row.organization_id
                ) THEN
                    RAISE EXCEPTION
                        'projects contain a workspace owned by another organization for tenant %; repair rows before enabling RLS',
                        tenant_row.id;
                END IF;
            END LOOP;
        END;
        $$"""
    )

    for table in ("organizations", "workspaces"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """CREATE POLICY tenant_isolation ON organizations
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON workspaces
        USING (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = workspaces.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = workspaces.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )


def downgrade() -> None:
    for table in ("workspaces", "organizations"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
