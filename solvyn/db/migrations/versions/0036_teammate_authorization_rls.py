"""Apply tenant RLS to teammate profiles and authorization records.

Revision ID: 0036_teammate_authorization_rls
Revises: 0035_project_repository_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0036_teammate_authorization_rls"
down_revision: str | None = "0035_project_repository_rls"
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
                FROM teammate_profiles teammate_row
                LEFT JOIN organizations organization_row
                    ON organization_row.id = teammate_row.organization_id
                WHERE organization_row.id IS NULL
                   OR organization_row.tenant_id <> teammate_row.tenant_id
            ) THEN
                RAISE EXCEPTION
                    'teammate profiles contain an invalid organization or tenant link; repair rows before enabling RLS';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM teammate_permissions permission_row
                LEFT JOIN teammate_profiles teammate_row
                    ON teammate_row.id = permission_row.teammate_id
                WHERE teammate_row.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'teammate permissions contain a missing profile link; repair rows before enabling RLS';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM teammate_project_assignments assignment_row
                LEFT JOIN teammate_profiles teammate_row
                    ON teammate_row.id = assignment_row.teammate_id
                WHERE teammate_row.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'teammate assignments contain a missing profile link; repair rows before enabling RLS';
            END IF;

            -- Projects already have forced RLS. Bind each tenant before
            -- validating assignments so hidden cross-tenant links fail closed.
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);

                IF EXISTS (
                    SELECT 1
                    FROM teammate_project_assignments assignment_row
                    LEFT JOIN teammate_profiles teammate_row
                        ON teammate_row.id = assignment_row.teammate_id
                    LEFT JOIN projects project_row
                        ON project_row.id = assignment_row.project_id
                    WHERE teammate_row.tenant_id = tenant_row.id
                      AND (
                          project_row.id IS NULL
                          OR project_row.tenant_id <> teammate_row.tenant_id
                          OR project_row.organization_id <> teammate_row.organization_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'teammate assignments contain an invalid project link for tenant %; repair rows before enabling RLS',
                        tenant_row.id;
                END IF;

            END LOOP;
        END;
        $$"""
    )

    for table in (
        "teammate_profiles",
        "teammate_permissions",
        "teammate_project_assignments",
    ):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """CREATE POLICY tenant_isolation ON teammate_profiles
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = teammate_profiles.organization_id
                  AND organization_row.tenant_id = teammate_profiles.tenant_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = teammate_profiles.organization_id
                  AND organization_row.tenant_id = teammate_profiles.tenant_id
            )
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON teammate_permissions
        USING (
            EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                WHERE teammate_row.id = teammate_permissions.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM teammate_profiles teammate_row
                WHERE teammate_row.id = teammate_permissions.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON teammate_project_assignments
        USING (
            EXISTS (
                SELECT 1
                FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = teammate_project_assignments.project_id
                WHERE teammate_row.id = teammate_project_assignments.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND project_row.tenant_id = teammate_row.tenant_id
                  AND project_row.organization_id = teammate_row.organization_id
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1
                FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = teammate_project_assignments.project_id
                WHERE teammate_row.id = teammate_project_assignments.teammate_id
                  AND teammate_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND project_row.tenant_id = teammate_row.tenant_id
                  AND project_row.organization_id = teammate_row.organization_id
            )
        )"""
    )


def downgrade() -> None:
    for table in (
        "teammate_project_assignments",
        "teammate_permissions",
        "teammate_profiles",
    ):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
