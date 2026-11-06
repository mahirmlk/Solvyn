"""Scope per-tenant API keys with transaction-local tenant context.

Revision ID: 0039_api_key_rls
Revises: 0038_organization_membership_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0039_api_key_rls"
down_revision: str | None = "0038_organization_membership_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON api_keys
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON api_keys")
    op.execute("ALTER TABLE api_keys NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE api_keys DISABLE ROW LEVEL SECURITY")
