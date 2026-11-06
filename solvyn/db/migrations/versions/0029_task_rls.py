"""Apply tenant RLS to workflow tasks.

Revision ID: 0029_task_rls
Revises: 0028_execution_graph_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029_task_rls"
down_revision: str | None = "0028_execution_graph_rls"
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
                    SELECT 1 FROM tasks task_row
                    WHERE task_row.tenant_id = scoped_tenant
                      AND task_row.session_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM sessions session_row
                          WHERE session_row.id = task_row.session_id
                            AND session_row.tenant_id = task_row.tenant_id
                      )
                ) OR EXISTS (
                    SELECT 1 FROM tasks task_row
                    WHERE task_row.tenant_id = scoped_tenant
                      AND task_row.execution_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM execution_graphs graph_row
                          WHERE graph_row.id = task_row.execution_id
                            AND graph_row.session_id = task_row.session_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'tasks for tenant % contain invalid session/execution references; repair rows before enabling task RLS',
                        scoped_tenant;
                END IF;
            END LOOP;
        END;
        $$"""
    )
    op.create_unique_constraint("uq_tasks_tenant_id_id", "tasks", ["tenant_id", "id"])
    op.create_foreign_key(
        "fk_tasks_parent_same_tenant",
        "tasks",
        "tasks",
        ["tenant_id", "parent_task_id"],
        ["tenant_id", "id"],
    )
    op.execute("ALTER TABLE tasks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tasks FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON tasks
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL OR EXISTS (
                    SELECT 1 FROM sessions
                    WHERE sessions.id = tasks.session_id
                      AND sessions.tenant_id = tasks.tenant_id
                )
            )
            AND (
                execution_id IS NULL OR EXISTS (
                    SELECT 1 FROM execution_graphs
                    WHERE execution_graphs.id = tasks.execution_id
                      AND execution_graphs.session_id = tasks.session_id
                )
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND (
                session_id IS NULL OR EXISTS (
                    SELECT 1 FROM sessions
                    WHERE sessions.id = tasks.session_id
                      AND sessions.tenant_id = tasks.tenant_id
                )
            )
            AND (
                execution_id IS NULL OR EXISTS (
                    SELECT 1 FROM execution_graphs
                    WHERE execution_graphs.id = tasks.execution_id
                      AND execution_graphs.session_id = tasks.session_id
                )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON tasks")
    op.execute("ALTER TABLE tasks NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tasks DISABLE ROW LEVEL SECURITY")
    op.drop_constraint("fk_tasks_parent_same_tenant", "tasks", type_="foreignkey")
    op.drop_constraint("uq_tasks_tenant_id_id", "tasks", type_="unique")
