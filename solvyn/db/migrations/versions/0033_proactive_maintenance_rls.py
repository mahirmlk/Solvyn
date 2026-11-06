"""Apply tenant RLS to proactive events and maintenance schedules.

Revision ID: 0033_proactive_maintenance_rls
Revises: 0032_approval_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033_proactive_maintenance_rls"
down_revision: str | None = "0032_approval_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM proactive_events event_row
                LEFT JOIN organizations organization_row ON organization_row.id = event_row.organization_id
                LEFT JOIN projects project_row ON project_row.id = event_row.project_id
                WHERE organization_row.id IS NULL
                   OR project_row.id IS NULL
                   OR project_row.organization_id <> event_row.organization_id
                   OR project_row.tenant_id <> organization_row.tenant_id
            ) THEN
                RAISE EXCEPTION
                    'proactive events contain invalid organization/project links; repair rows before enabling RLS';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM maintenance_schedules schedule_row
                LEFT JOIN organizations organization_row ON organization_row.id = schedule_row.organization_id
                LEFT JOIN projects project_row ON project_row.id = schedule_row.project_id
                WHERE organization_row.id IS NULL
                   OR project_row.id IS NULL
                   OR project_row.organization_id <> schedule_row.organization_id
                   OR project_row.tenant_id <> organization_row.tenant_id
            ) THEN
                RAISE EXCEPTION
                    'maintenance schedules contain invalid organization/project links; repair rows before enabling RLS';
            END IF;
        END;
        $$"""
    )
    for table in ("proactive_events", "maintenance_schedules"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""CREATE POLICY tenant_isolation ON {table}
            USING (
                EXISTS (
                    SELECT 1 FROM organizations organization_row
                    WHERE organization_row.id = {table}.organization_id
                      AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
                AND EXISTS (
                    SELECT 1 FROM projects project_row
                    WHERE project_row.id = {table}.project_id
                      AND project_row.organization_id = {table}.organization_id
                      AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
            )
            WITH CHECK (
                EXISTS (
                    SELECT 1 FROM organizations organization_row
                    WHERE organization_row.id = {table}.organization_id
                      AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
                AND EXISTS (
                    SELECT 1 FROM projects project_row
                    WHERE project_row.id = {table}.project_id
                      AND project_row.organization_id = {table}.organization_id
                      AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                )
            )"""
        )


def downgrade() -> None:
    for table in ("maintenance_schedules", "proactive_events"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
