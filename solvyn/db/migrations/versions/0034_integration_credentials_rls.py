"""Apply tenant RLS to organization integrations and encrypted credentials.

Revision ID: 0034_integration_credentials_rls
Revises: 0033_proactive_maintenance_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034_integration_credentials_rls"
down_revision: str | None = "0033_proactive_maintenance_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM integrations integration_row
                LEFT JOIN organizations organization_row ON organization_row.id = integration_row.organization_id
                WHERE organization_row.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'integrations contain a missing organization link; repair rows before enabling integration RLS';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM integration_credentials credential_row
                LEFT JOIN integrations integration_row ON integration_row.id = credential_row.integration_id
                LEFT JOIN organizations organization_row ON organization_row.id = integration_row.organization_id
                WHERE integration_row.id IS NULL OR organization_row.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'integration credentials contain a missing organization link; repair rows before enabling credential RLS';
            END IF;
        END;
        $$"""
    )

    op.execute("ALTER TABLE integrations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integrations FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON integrations
        USING (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = integrations.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = integrations.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )

    op.execute("ALTER TABLE integration_credentials ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integration_credentials FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON integration_credentials
        USING (
            EXISTS (
                SELECT 1
                FROM integrations integration_row
                JOIN organizations organization_row ON organization_row.id = integration_row.organization_id
                WHERE integration_row.id = integration_credentials.integration_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM integrations integration_row
                JOIN organizations organization_row ON organization_row.id = integration_row.organization_id
                WHERE integration_row.id = integration_credentials.integration_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON integration_credentials")
    op.execute("ALTER TABLE integration_credentials NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integration_credentials DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON integrations")
    op.execute("ALTER TABLE integrations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE integrations DISABLE ROW LEVEL SECURITY")
