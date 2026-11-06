"""Scope organization memberships through their owning organization.

Revision ID: 0038_organization_membership_rls
Revises: 0037_organization_workspace_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038_organization_membership_rls"
down_revision: str | None = "0037_organization_workspace_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE organization_memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_memberships FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON organization_memberships
        USING (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = organization_memberships.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = organization_memberships.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON organization_memberships")
    op.execute("ALTER TABLE organization_memberships NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE organization_memberships DISABLE ROW LEVEL SECURITY")
