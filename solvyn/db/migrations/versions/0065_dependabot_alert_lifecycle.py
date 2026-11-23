"""Persist the latest Dependabot alert lifecycle marker.

Revision ID: 0065_dependabot_alert_lifecycle
Revises: 0064_github_app_scope
Create Date: 2026-11-21 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0065_dependabot_alert_lifecycle"
down_revision: str | None = "0064_github_app_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "github_dependabot_alert_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("repository_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("repository_key", sa.String(length=255), nullable=False),
        sa.Column("repository_name", sa.String(length=255), nullable=False),
        sa.Column("alert_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("severity", sa.String(length=50), nullable=False),
        sa.Column("branch", sa.String(length=255), server_default="", nullable=False),
        sa.Column("details_url", sa.String(length=512), server_default="", nullable=False),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_reopen_marker", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "project_id", "repository_id", "alert_number",
            name="uq_github_dependabot_alert_states_identity",
        ),
        sa.CheckConstraint("alert_number > 0", name="ck_github_dependabot_alert_states_number"),
        sa.CheckConstraint(
            "lifecycle_state IN ('open', 'dismissed', 'fixed')",
            name="ck_github_dependabot_alert_states_state",
        ),
    )
    op.create_index(
        "ix_github_dependabot_alert_states_project_updated",
        "github_dependabot_alert_states",
        ["organization_id", "project_id", "updated_at"],
    )
    op.execute("ALTER TABLE github_dependabot_alert_states ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE github_dependabot_alert_states FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON github_dependabot_alert_states
        USING (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = github_dependabot_alert_states.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = github_dependabot_alert_states.project_id
                  AND project_row.organization_id = github_dependabot_alert_states.organization_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM repositories repository_row
                WHERE repository_row.id = github_dependabot_alert_states.repository_id
                  AND repository_row.project_id = github_dependabot_alert_states.project_id
                  AND repository_row.provider = 'github'
                  AND repository_row.integration_id IS NOT NULL
                  AND repository_row.github_repository_id IS NOT NULL
            )
        )
        WITH CHECK (
            EXISTS (
                SELECT 1 FROM organizations organization_row
                WHERE organization_row.id = github_dependabot_alert_states.organization_id
                  AND organization_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM projects project_row
                WHERE project_row.id = github_dependabot_alert_states.project_id
                  AND project_row.organization_id = github_dependabot_alert_states.organization_id
                  AND project_row.tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            )
            AND EXISTS (
                SELECT 1 FROM repositories repository_row
                WHERE repository_row.id = github_dependabot_alert_states.repository_id
                  AND repository_row.project_id = github_dependabot_alert_states.project_id
                  AND repository_row.provider = 'github'
                  AND repository_row.integration_id IS NOT NULL
                  AND repository_row.github_repository_id IS NOT NULL
            )
        )"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE github_dependabot_alert_states NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM github_dependabot_alert_states) THEN
                RAISE EXCEPTION 'Dependabot lifecycle state exists; export/archive it before downgrading';
            END IF;
        END;
        $$;"""
    )
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON github_dependabot_alert_states")
    op.execute("ALTER TABLE github_dependabot_alert_states DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_github_dependabot_alert_states_project_updated", table_name="github_dependabot_alert_states")
    op.drop_table("github_dependabot_alert_states")

