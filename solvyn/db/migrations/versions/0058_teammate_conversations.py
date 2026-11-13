"""Add tenant-scoped persistent teammate conversations.

Revision ID: 0058_teammate_conversations
Revises: 0057_task_completion_gates
Create Date: 2026-11-12 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0058_teammate_conversations"
down_revision: str | None = "0057_task_completion_gates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "teammate_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("teammate_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("teammate_profiles.id"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False, unique=True),
        sa.Column("title", sa.String(255), nullable=False, server_default="New conversation"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_teammate_conversations_tenant_id", "teammate_conversations", ["tenant_id"])
    op.create_index("ix_teammate_conversations_organization_id", "teammate_conversations", ["organization_id"])
    op.create_index("ix_teammate_conversations_teammate_id", "teammate_conversations", ["teammate_id"])
    op.create_index("ix_teammate_conversations_project_id", "teammate_conversations", ["project_id"])
    op.create_index("ix_teammate_conversations_session_id", "teammate_conversations", ["session_id"])
    op.create_index(
        "ix_teammate_conversations_scope",
        "teammate_conversations",
        ["tenant_id", "teammate_id", "project_id", "updated_at"],
    )

    op.execute("ALTER TABLE teammate_conversations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teammate_conversations FORCE ROW LEVEL SECURITY")
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


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON teammate_conversations")
    op.execute("ALTER TABLE teammate_conversations NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE teammate_conversations DISABLE ROW LEVEL SECURITY")
    op.drop_table("teammate_conversations")
