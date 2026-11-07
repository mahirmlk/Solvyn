"""Link maintenance schedules to approval-bound verification runs.

Revision ID: 0049_scheduled_checks
Revises: 0048_verification_redrive
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0049_scheduled_checks"
down_revision: str | None = "0048_verification_redrive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("maintenance_schedules", sa.Column("tenant_id", UUID(as_uuid=True), nullable=True))
    op.add_column("maintenance_schedules", sa.Column("task_id", UUID(as_uuid=True), nullable=True))

    # maintenance_schedules already uses forced RLS. Backfill one tenant at a
    # time so the non-bypass migration role only updates rows visible under the
    # existing organization/project policy.
    op.execute(
        """DO $$
        DECLARE tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);
                UPDATE maintenance_schedules schedule_row
                SET tenant_id = tenant_row.id
                FROM organizations organization_row
                WHERE schedule_row.organization_id = organization_row.id
                  AND organization_row.tenant_id = tenant_row.id;
            END LOOP;
        END;
        $$"""
    )
    op.alter_column("maintenance_schedules", "tenant_id", nullable=False)
    op.create_foreign_key(
        "fk_maintenance_schedules_tenant_id_tenants",
        "maintenance_schedules",
        "tenants",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_maintenance_schedules_tenant_id_id", "maintenance_schedules", ["tenant_id", "id"]
    )
    op.create_foreign_key(
        "fk_maintenance_schedules_task_same_tenant",
        "maintenance_schedules",
        "tasks",
        ["tenant_id", "task_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_maintenance_schedules_tenant_id", "maintenance_schedules", ["tenant_id"])
    op.create_index("ix_maintenance_schedules_task_id", "maintenance_schedules", ["task_id"])

    op.add_column("verification_runs", sa.Column("maintenance_schedule_id", UUID(as_uuid=True), nullable=True))
    op.add_column("verification_runs", sa.Column("maintenance_scheduled_for", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint(
        "uq_verification_runs_maintenance_occurrence",
        "verification_runs",
        ["tenant_id", "maintenance_schedule_id", "maintenance_scheduled_for"],
    )
    op.create_foreign_key(
        "fk_verification_runs_maintenance_same_tenant",
        "verification_runs",
        "maintenance_schedules",
        ["tenant_id", "maintenance_schedule_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_verification_runs_maintenance_occurrence_pair",
        "verification_runs",
        "(maintenance_schedule_id IS NULL) = (maintenance_scheduled_for IS NULL)",
    )
    op.create_index("ix_verification_runs_maintenance_schedule_id", "verification_runs", ["maintenance_schedule_id"])

    op.execute("DROP POLICY tenant_isolation ON maintenance_schedules")
    op.execute(
        """CREATE POLICY tenant_isolation ON maintenance_schedules
        USING (
            maintenance_schedules.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = maintenance_schedules.organization_id
                  AND organization_row.tenant_id = maintenance_schedules.tenant_id
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = maintenance_schedules.project_id
                  AND project_row.organization_id = maintenance_schedules.organization_id
                  AND project_row.tenant_id = maintenance_schedules.tenant_id
            )
            AND (
                maintenance_schedules.task_id IS NULL OR (
                    EXISTS (
                        SELECT 1 FROM tasks task_row
                        WHERE task_row.id = maintenance_schedules.task_id
                          AND task_row.tenant_id = maintenance_schedules.tenant_id
                    )
                    AND EXISTS (
                        SELECT 1 FROM task_workspaces workspace_row
                        WHERE workspace_row.task_id = maintenance_schedules.task_id
                          AND workspace_row.tenant_id = maintenance_schedules.tenant_id
                    )
                    AND EXISTS (
                        SELECT 1 FROM task_handoffs handoff_row
                        WHERE handoff_row.child_task_id = maintenance_schedules.task_id
                          AND handoff_row.project_id = maintenance_schedules.project_id
                          AND handoff_row.tenant_id = maintenance_schedules.tenant_id
                    )
                )
            )
        )
        WITH CHECK (
            maintenance_schedules.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = maintenance_schedules.organization_id
                  AND organization_row.tenant_id = maintenance_schedules.tenant_id
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = maintenance_schedules.project_id
                  AND project_row.organization_id = maintenance_schedules.organization_id
                  AND project_row.tenant_id = maintenance_schedules.tenant_id
            )
            AND (
                maintenance_schedules.task_id IS NULL OR (
                    EXISTS (
                        SELECT 1 FROM tasks task_row
                        WHERE task_row.id = maintenance_schedules.task_id
                          AND task_row.tenant_id = maintenance_schedules.tenant_id
                    )
                    AND EXISTS (
                        SELECT 1 FROM task_workspaces workspace_row
                        WHERE workspace_row.task_id = maintenance_schedules.task_id
                          AND workspace_row.tenant_id = maintenance_schedules.tenant_id
                    )
                    AND EXISTS (
                        SELECT 1 FROM task_handoffs handoff_row
                        WHERE handoff_row.child_task_id = maintenance_schedules.task_id
                          AND handoff_row.project_id = maintenance_schedules.project_id
                          AND handoff_row.tenant_id = maintenance_schedules.tenant_id
                    )
                )
            )
        )"""
    )

    op.execute("DROP POLICY tenant_isolation ON verification_runs")
    op.execute(
        """CREATE POLICY tenant_isolation ON verification_runs
        USING (
            verification_runs.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_runs.task_id
                  AND task_row.tenant_id = verification_runs.tenant_id
            )
            AND EXISTS (
                SELECT 1 FROM task_workspaces workspace_row
                WHERE workspace_row.id = verification_runs.workspace_id
                  AND workspace_row.tenant_id = verification_runs.tenant_id
                  AND workspace_row.task_id = verification_runs.task_id
            )
            AND (
                verification_runs.maintenance_schedule_id IS NULL OR EXISTS (
                    SELECT 1 FROM maintenance_schedules schedule_row
                    WHERE schedule_row.id = verification_runs.maintenance_schedule_id
                      AND schedule_row.tenant_id = verification_runs.tenant_id
                      AND schedule_row.task_id = verification_runs.task_id
                )
            )
        )
        WITH CHECK (
            verification_runs.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_runs.task_id
                  AND task_row.tenant_id = verification_runs.tenant_id
            )
            AND EXISTS (
                SELECT 1 FROM task_workspaces workspace_row
                WHERE workspace_row.id = verification_runs.workspace_id
                  AND workspace_row.tenant_id = verification_runs.tenant_id
                  AND workspace_row.task_id = verification_runs.task_id
            )
            AND (
                verification_runs.maintenance_schedule_id IS NULL OR EXISTS (
                    SELECT 1 FROM maintenance_schedules schedule_row
                    WHERE schedule_row.id = verification_runs.maintenance_schedule_id
                      AND schedule_row.tenant_id = verification_runs.tenant_id
                      AND schedule_row.task_id = verification_runs.task_id
                )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON maintenance_schedules")
    op.execute(
        """CREATE POLICY tenant_isolation ON maintenance_schedules
        USING (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = maintenance_schedules.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = maintenance_schedules.project_id
                  AND project_row.organization_id = maintenance_schedules.organization_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = maintenance_schedules.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = maintenance_schedules.project_id
                  AND project_row.organization_id = maintenance_schedules.organization_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
        )"""
    )
    op.execute("DROP POLICY tenant_isolation ON verification_runs")
    op.execute(
        """CREATE POLICY tenant_isolation ON verification_runs
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_runs.task_id
                  AND task_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM task_workspaces workspace_row
                WHERE workspace_row.id = verification_runs.workspace_id
                  AND workspace_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND workspace_row.task_id = verification_runs.task_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_runs.task_id
                  AND task_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM task_workspaces workspace_row
                WHERE workspace_row.id = verification_runs.workspace_id
                  AND workspace_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
                  AND workspace_row.task_id = verification_runs.task_id
            )
        )"""
    )

    op.drop_index("ix_verification_runs_maintenance_schedule_id", table_name="verification_runs")
    op.drop_constraint("ck_verification_runs_maintenance_occurrence_pair", "verification_runs", type_="check")
    op.drop_constraint("fk_verification_runs_maintenance_same_tenant", "verification_runs", type_="foreignkey")
    op.drop_constraint("uq_verification_runs_maintenance_occurrence", "verification_runs", type_="unique")
    op.drop_column("verification_runs", "maintenance_scheduled_for")
    op.drop_column("verification_runs", "maintenance_schedule_id")

    op.drop_index("ix_maintenance_schedules_task_id", table_name="maintenance_schedules")
    op.drop_index("ix_maintenance_schedules_tenant_id", table_name="maintenance_schedules")
    op.drop_constraint("fk_maintenance_schedules_task_same_tenant", "maintenance_schedules", type_="foreignkey")
    op.drop_constraint("uq_maintenance_schedules_tenant_id_id", "maintenance_schedules", type_="unique")
    op.drop_constraint("fk_maintenance_schedules_tenant_id_tenants", "maintenance_schedules", type_="foreignkey")
    op.drop_column("maintenance_schedules", "task_id")
    op.drop_column("maintenance_schedules", "tenant_id")

