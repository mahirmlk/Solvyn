"""Attribute LLM usage to tasks for durable execution history.

Revision ID: 0054_task_scoped_token_usage
Revises: 0053_sched_verif_profiles
Create Date: 2026-11-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0054_task_scoped_token_usage"
down_revision: str | None = "0053_sched_verif_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("token_usage", sa.Column("task_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_token_usage_task_id_tasks",
        "token_usage",
        "tasks",
        ["task_id"],
        ["id"],
    )
    op.create_index(
        "ix_token_usage_tenant_task_created_at",
        "token_usage",
        ["tenant_id", "task_id", "created_at"],
    )
    op.execute(
        """DO $$
        DECLARE scoped_tenant uuid;
        BEGIN
            FOR scoped_tenant IN SELECT id FROM tenants LOOP
                PERFORM set_config('app.tenant_id', scoped_tenant::text, true);
                IF EXISTS (
                    SELECT 1 FROM token_usage usage_row
                    WHERE usage_row.tenant_id = scoped_tenant
                      AND usage_row.task_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM tasks task_row
                          WHERE task_row.id = usage_row.task_id
                            AND task_row.tenant_id = usage_row.tenant_id
                      )
                ) THEN
                    RAISE EXCEPTION
                        'token usage for tenant % references a task owned by another tenant; repair rows before enabling task usage RLS',
                        scoped_tenant;
                END IF;
            END LOOP;
        END;
        $$"""
    )
    op.execute("DROP POLICY tenant_isolation ON token_usage")
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
            AND (
                task_id IS NULL OR EXISTS (
                    SELECT 1 FROM tasks
                    WHERE tasks.id = token_usage.task_id
                      AND tasks.tenant_id = token_usage.tenant_id
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
            AND (
                task_id IS NULL OR EXISTS (
                    SELECT 1 FROM tasks
                    WHERE tasks.id = token_usage.task_id
                      AND tasks.tenant_id = token_usage.tenant_id
                )
            )
        )"""
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON token_usage")
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
    op.drop_index("ix_token_usage_tenant_task_created_at", table_name="token_usage")
    op.drop_constraint("fk_token_usage_task_id_tasks", "token_usage", type_="foreignkey")
    op.drop_column("token_usage", "task_id")
