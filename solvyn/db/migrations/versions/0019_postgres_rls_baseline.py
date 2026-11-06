"""Add a first, request-scoped PostgreSQL RLS baseline.

Revision ID: 0019_postgres_rls_baseline
Revises: 0018_verification_approvals
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_postgres_rls_baseline"
down_revision: str | None = "0018_verification_approvals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_POLICY_TABLES = ("task_acceptance_criteria", "task_deliveries")


def upgrade() -> None:
    for table in _POLICY_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE applies policies to the table owner too. Deployment roles must
        # therefore set app.tenant_id for tenant-scoped work, while unscoped
        # access to these records remains denied.
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
            WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
        )


def downgrade() -> None:
    for table in reversed(_POLICY_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
