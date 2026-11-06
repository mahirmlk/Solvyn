"""Apply tenant RLS to token usage records.

Revision ID: 0031_token_usage_rls
Revises: 0030_legacy_memory_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031_token_usage_rls"
down_revision: str | None = "0030_legacy_memory_rls"
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
                    SELECT 1 FROM token_usage usage_row
                    WHERE usage_row.tenant_id = scoped_tenant
                      AND usage_row.session_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions session_row
                          WHERE session_row.id = usage_row.session_id
                            AND session_row.tenant_id = usage_row.tenant_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'token usage for tenant % contains a session owned by another tenant; repair rows before enabling token usage RLS',
                        scoped_tenant;
                END IF;
            END LOOP;
        END;
        $$"""
    )
    op.execute("ALTER TABLE token_usage ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE token_usage FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON token_usage
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL OR EXISTS (
                    SELECT 1 FROM sessions
                    WHERE sessions.id = token_usage.session_id
                      AND sessions.tenant_id = token_usage.tenant_id
                )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL OR EXISTS (
                    SELECT 1 FROM sessions
                    WHERE sessions.id = token_usage.session_id
                      AND sessions.tenant_id = token_usage.tenant_id
                )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON token_usage")
    op.execute("ALTER TABLE token_usage NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE token_usage DISABLE ROW LEVEL SECURITY")
