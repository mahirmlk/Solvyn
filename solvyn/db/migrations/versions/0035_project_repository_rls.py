"""Apply tenant RLS to projects, repositories, and project knowledge.

Revision ID: 0035_project_repository_rls
Revises: 0034_integration_credentials_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035_project_repository_rls"
down_revision: str | None = "0034_integration_credentials_rls"
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
                FROM projects project_row
                LEFT JOIN organizations organization_row ON organization_row.id = project_row.organization_id
                LEFT JOIN workspaces workspace_row ON workspace_row.id = project_row.workspace_id
                WHERE organization_row.id IS NULL
                   OR project_row.tenant_id <> organization_row.tenant_id
                   OR (project_row.workspace_id IS NOT NULL AND workspace_row.id IS NULL)
                   OR (project_row.workspace_id IS NOT NULL AND workspace_row.organization_id <> project_row.organization_id)
            ) THEN
                RAISE EXCEPTION
                    'projects contain invalid organization, tenant, or workspace links; repair rows before enabling RLS';
            END IF;

            IF EXISTS (
                SELECT 1
                FROM project_knowledge knowledge_row
                LEFT JOIN projects project_row ON project_row.id = knowledge_row.project_id
                WHERE project_row.id IS NULL
            ) THEN
                RAISE EXCEPTION
                    'project knowledge contains a missing project link; repair rows before enabling RLS';
            END IF;

            -- These related tables already have FORCE RLS. Validate their links
            -- tenant by tenant because the migration role cannot bypass policies.
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);

                IF EXISTS (
                    SELECT 1
                    FROM repositories repository_row
                    JOIN projects project_row ON project_row.id = repository_row.project_id
                    LEFT JOIN integrations integration_row ON integration_row.id = repository_row.integration_id
                    WHERE project_row.tenant_id = tenant_row.id
                      AND repository_row.integration_id IS NOT NULL
                      AND (
                          integration_row.id IS NULL
                          OR integration_row.organization_id <> project_row.organization_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'repositories contain invalid project or integration links for tenant %; repair rows before enabling RLS',
                        tenant_row.id;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM task_workspaces workspace_row
                    LEFT JOIN tasks task_row ON task_row.id = workspace_row.task_id
                    LEFT JOIN repositories repository_row ON repository_row.id = workspace_row.repository_id
                    LEFT JOIN projects project_row ON project_row.id = repository_row.project_id
                    WHERE workspace_row.tenant_id = tenant_row.id
                      AND workspace_row.repository_id IS NOT NULL
                      AND (
                          task_row.id IS NULL
                          OR task_row.tenant_id <> tenant_row.id
                          OR repository_row.id IS NULL
                          OR project_row.tenant_id <> tenant_row.id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'task workspaces contain a repository outside their tenant for tenant %; repair rows before tightening RLS',
                        tenant_row.id;
                END IF;

                IF EXISTS (
                    SELECT 1
                    FROM task_deliveries delivery_row
                    LEFT JOIN tasks task_row ON task_row.id = delivery_row.task_id
                    LEFT JOIN task_workspaces workspace_row ON workspace_row.id = delivery_row.workspace_id
                    LEFT JOIN repositories repository_row ON repository_row.id = delivery_row.repository_id
                    LEFT JOIN projects project_row ON project_row.id = repository_row.project_id
                    WHERE delivery_row.tenant_id = tenant_row.id
                      AND (
                          task_row.id IS NULL
                          OR task_row.tenant_id <> tenant_row.id
                          OR workspace_row.id IS NULL
                          OR workspace_row.task_id <> delivery_row.task_id
                          OR workspace_row.tenant_id <> delivery_row.tenant_id
                          OR workspace_row.repository_id IS DISTINCT FROM delivery_row.repository_id
                          OR repository_row.id IS NULL
                          OR project_row.tenant_id <> tenant_row.id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'task deliveries contain repository/workspace mismatches for tenant %; repair rows before tightening RLS',
                        tenant_row.id;
                END IF;
            END LOOP;
        END;
        $$"""
    )

    for table in ("projects", "repositories", "project_knowledge"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    op.execute(
        """CREATE POLICY tenant_isolation ON projects
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = projects.organization_id
                  AND organization_row.tenant_id = projects.tenant_id
            )
            AND (
                projects.workspace_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM workspaces workspace_row
                    WHERE workspace_row.id = projects.workspace_id
                      AND workspace_row.organization_id = projects.organization_id
                )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = projects.organization_id
                  AND organization_row.tenant_id = projects.tenant_id
            )
            AND (
                projects.workspace_id IS NULL
                OR EXISTS (
                    SELECT 1 FROM workspaces workspace_row
                    WHERE workspace_row.id = projects.workspace_id
                      AND workspace_row.organization_id = projects.organization_id
                )
            )
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON repositories
        USING (
            EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = repositories.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND (
                repositories.integration_id IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM integrations integration_row
                    JOIN projects project_row ON project_row.id = repositories.project_id
                    WHERE integration_row.id = repositories.integration_id
                      AND integration_row.organization_id = project_row.organization_id
                )
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = repositories.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND (
                repositories.integration_id IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM integrations integration_row
                    JOIN projects project_row ON project_row.id = repositories.project_id
                    WHERE integration_row.id = repositories.integration_id
                      AND integration_row.organization_id = project_row.organization_id
                )
            )
        )"""
    )
    op.execute(
        """CREATE POLICY tenant_isolation ON project_knowledge
        USING (
            EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = project_knowledge.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = project_knowledge.project_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )

    # Keep repository-backed workspace metadata inside its tenant without
    # depending on a currently active teammate assignment.
    op.execute("DROP POLICY tenant_isolation ON task_workspaces")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_workspaces
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_workspaces.task_id
                  AND task_row.tenant_id = task_workspaces.tenant_id
            )
            AND (
                task_workspaces.repository_id IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM repositories repository_row
                    JOIN projects project_row ON project_row.id = repository_row.project_id
                    WHERE repository_row.id = task_workspaces.repository_id
                      AND project_row.tenant_id = task_workspaces.tenant_id
                )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_workspaces.task_id
                  AND task_row.tenant_id = task_workspaces.tenant_id
            )
            AND (
                task_workspaces.repository_id IS NULL
                OR EXISTS (
                    SELECT 1
                    FROM repositories repository_row
                    JOIN projects project_row ON project_row.id = repository_row.project_id
                    WHERE repository_row.id = task_workspaces.repository_id
                      AND project_row.tenant_id = task_workspaces.tenant_id
                )
            )
        )"""
    )

    # Keep delivery history bound to its task, workspace, and repository.
    op.execute("DROP POLICY tenant_isolation ON task_deliveries")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_deliveries
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_deliveries.task_id
                  AND task_row.tenant_id = task_deliveries.tenant_id
            )
            AND EXISTS (
                SELECT 1
                FROM task_workspaces workspace_row
                JOIN tasks task_row ON task_row.id = task_deliveries.task_id
                JOIN repositories repository_row ON repository_row.id = task_deliveries.repository_id
                WHERE workspace_row.id = task_deliveries.workspace_id
                  AND workspace_row.task_id = task_deliveries.task_id
                  AND workspace_row.tenant_id = task_deliveries.tenant_id
                  AND workspace_row.repository_id = task_deliveries.repository_id
                  AND task_row.tenant_id = task_deliveries.tenant_id
                  AND EXISTS (
                      SELECT 1 FROM projects project_row
                      WHERE project_row.id = repository_row.project_id
                        AND project_row.tenant_id = task_deliveries.tenant_id
                  )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = task_deliveries.task_id
                  AND task_row.tenant_id = task_deliveries.tenant_id
            )
            AND EXISTS (
                SELECT 1
                FROM task_workspaces workspace_row
                JOIN tasks task_row ON task_row.id = task_deliveries.task_id
                JOIN repositories repository_row ON repository_row.id = task_deliveries.repository_id
                WHERE workspace_row.id = task_deliveries.workspace_id
                  AND workspace_row.task_id = task_deliveries.task_id
                  AND workspace_row.tenant_id = task_deliveries.tenant_id
                  AND workspace_row.repository_id = task_deliveries.repository_id
                  AND task_row.tenant_id = task_deliveries.tenant_id
                  AND EXISTS (
                      SELECT 1 FROM projects project_row
                      WHERE project_row.id = repository_row.project_id
                        AND project_row.tenant_id = task_deliveries.tenant_id
                  )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_deliveries")
    op.execute("ALTER TABLE task_deliveries NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_deliveries DISABLE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_workspaces")
    op.execute("ALTER TABLE task_workspaces NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_workspaces DISABLE ROW LEVEL SECURITY")
    for table in ("project_knowledge", "repositories", "projects"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute(
        """CREATE POLICY tenant_isolation ON task_deliveries
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )
    op.execute("ALTER TABLE task_workspaces ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_workspaces FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_workspaces
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )
