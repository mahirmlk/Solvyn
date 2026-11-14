"""Link teammate conversations to approval-gated workflow tasks.

Revision ID: 0060_conversation_task_bridge
Revises: 0059_documentation_profile
Create Date: 2026-11-13 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0060_conversation_task_bridge"
down_revision: str | None = "0059_documentation_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_POLICY = """CREATE POLICY tenant_isolation ON teammate_conversations
USING (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND EXISTS (
        SELECT 1
        FROM teammate_profiles teammate_row
        JOIN projects project_row ON project_row.id = teammate_conversations.project_id
        JOIN sessions session_row ON session_row.id = teammate_conversations.session_id
        JOIN teammate_project_assignments assignment_row
          ON assignment_row.teammate_id = teammate_row.id
         AND assignment_row.project_id = project_row.id
        WHERE teammate_row.id = teammate_conversations.teammate_id
          AND teammate_row.organization_id = teammate_conversations.organization_id
          AND teammate_row.tenant_id = teammate_conversations.tenant_id
          AND project_row.organization_id = teammate_conversations.organization_id
          AND project_row.tenant_id = teammate_conversations.tenant_id
          AND session_row.tenant_id = teammate_conversations.tenant_id
    )
    AND (
        root_task_id IS NULL OR EXISTS (
            SELECT 1
            FROM tasks task_row
            JOIN execution_graphs graph_row ON graph_row.id = task_row.execution_id
            JOIN sessions task_session ON task_session.id = graph_row.session_id
            WHERE task_row.id = teammate_conversations.root_task_id
              AND task_row.tenant_id = teammate_conversations.tenant_id
              AND task_row.session_id = teammate_conversations.session_id
              AND graph_row.session_id = teammate_conversations.session_id
              AND task_session.tenant_id = teammate_conversations.tenant_id
        )
    )
)
WITH CHECK (
    tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
    AND EXISTS (
        SELECT 1
        FROM teammate_profiles teammate_row
        JOIN projects project_row ON project_row.id = teammate_conversations.project_id
        JOIN sessions session_row ON session_row.id = teammate_conversations.session_id
        JOIN teammate_project_assignments assignment_row
          ON assignment_row.teammate_id = teammate_row.id
         AND assignment_row.project_id = project_row.id
        WHERE teammate_row.id = teammate_conversations.teammate_id
          AND teammate_row.organization_id = teammate_conversations.organization_id
          AND teammate_row.tenant_id = teammate_conversations.tenant_id
          AND project_row.organization_id = teammate_conversations.organization_id
          AND project_row.tenant_id = teammate_conversations.tenant_id
          AND session_row.tenant_id = teammate_conversations.tenant_id
    )
    AND (
        root_task_id IS NULL OR EXISTS (
            SELECT 1
            FROM tasks task_row
            JOIN execution_graphs graph_row ON graph_row.id = task_row.execution_id
            JOIN sessions task_session ON task_session.id = graph_row.session_id
            WHERE task_row.id = teammate_conversations.root_task_id
              AND task_row.tenant_id = teammate_conversations.tenant_id
              AND task_row.session_id = teammate_conversations.session_id
              AND graph_row.session_id = teammate_conversations.session_id
              AND task_session.tenant_id = teammate_conversations.tenant_id
        )
    )
)"""


def upgrade() -> None:
    op.add_column(
        "teammate_conversations",
        sa.Column("root_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_teammate_conversations_root_task_same_tenant",
        "teammate_conversations",
        "tasks",
        ["tenant_id", "root_task_id"],
        ["tenant_id", "id"],
    )
    op.create_unique_constraint(
        "uq_teammate_conversations_root_task",
        "teammate_conversations",
        ["tenant_id", "root_task_id"],
    )
    op.create_index(
        "ix_teammate_conversations_root_task_id",
        "teammate_conversations",
        ["root_task_id"],
    )
    op.execute("DROP POLICY tenant_isolation ON teammate_conversations")
    op.execute(_POLICY)


def downgrade() -> None:
    op.execute("DROP POLICY tenant_isolation ON teammate_conversations")
    op.execute(
        """CREATE POLICY tenant_isolation ON teammate_conversations
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1
                FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = teammate_conversations.project_id
                JOIN sessions session_row ON session_row.id = teammate_conversations.session_id
                JOIN teammate_project_assignments assignment_row
                  ON assignment_row.teammate_id = teammate_row.id
                 AND assignment_row.project_id = project_row.id
                WHERE teammate_row.id = teammate_conversations.teammate_id
                  AND teammate_row.organization_id = teammate_conversations.organization_id
                  AND teammate_row.tenant_id = teammate_conversations.tenant_id
                  AND project_row.organization_id = teammate_conversations.organization_id
                  AND project_row.tenant_id = teammate_conversations.tenant_id
                  AND session_row.tenant_id = teammate_conversations.tenant_id
            )
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1
                FROM teammate_profiles teammate_row
                JOIN projects project_row ON project_row.id = teammate_conversations.project_id
                JOIN sessions session_row ON session_row.id = teammate_conversations.session_id
                JOIN teammate_project_assignments assignment_row
                  ON assignment_row.teammate_id = teammate_row.id
                 AND assignment_row.project_id = project_row.id
                WHERE teammate_row.id = teammate_conversations.teammate_id
                  AND teammate_row.organization_id = teammate_conversations.organization_id
                  AND teammate_row.tenant_id = teammate_conversations.tenant_id
                  AND project_row.organization_id = teammate_conversations.organization_id
                  AND project_row.tenant_id = teammate_conversations.tenant_id
                  AND session_row.tenant_id = teammate_conversations.tenant_id
            )
        )"""
    )
    op.drop_index("ix_teammate_conversations_root_task_id", table_name="teammate_conversations")
    op.drop_constraint("uq_teammate_conversations_root_task", "teammate_conversations", type_="unique")
    op.drop_constraint(
        "fk_teammate_conversations_root_task_same_tenant",
        "teammate_conversations",
        type_="foreignkey",
    )
    op.drop_column("teammate_conversations", "root_task_id")
