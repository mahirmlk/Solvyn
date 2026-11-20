"""Track current GitHub work-item state and immutable webhook transitions.

Revision ID: 0062_github_work_item_lifecycle
Revises: 0061_agent_evaluation_history
Create Date: 2026-11-20 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062_github_work_item_lifecycle"
down_revision: str | None = "0061_agent_evaluation_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def _tenant_policy(table: str) -> None:
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


def upgrade() -> None:
    op.create_table(
        "github_work_item_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("repository_key", sa.String(length=255), nullable=False),
        sa.Column("repository_name", sa.String(length=255), nullable=False),
        sa.Column("item_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=False),
        sa.Column("branch", sa.String(length=255), server_default="", nullable=False),
        sa.Column("details_url", sa.String(length=512), server_default="", nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_delivery_id", sa.String(length=128), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "project_id", "event_type", "repository_key", "item_number",
            name="uq_github_work_item_states_identity",
        ),
        sa.CheckConstraint("event_type IN ('issues', 'pull_request')", name="ck_github_work_item_states_type"),
        sa.CheckConstraint("lifecycle_state IN ('open', 'closed', 'merged')", name="ck_github_work_item_states_state"),
        sa.CheckConstraint("item_number > 0", name="ck_github_work_item_states_number"),
    )
    op.create_index(
        "ix_github_work_item_states_project_updated",
        "github_work_item_states",
        ["organization_id", "project_id", "updated_at"],
    )

    op.create_table(
        "proactive_event_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("delivery_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("repository_key", sa.String(length=255), nullable=False),
        sa.Column("repository_name", sa.String(length=255), nullable=False),
        sa.Column("item_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("lifecycle_state", sa.String(length=16), nullable=False),
        sa.Column("branch", sa.String(length=255), server_default="", nullable=False),
        sa.Column("details_url", sa.String(length=512), server_default="", nullable=False),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_to_current", sa.Boolean(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "organization_id", "project_id", "delivery_id", name="uq_proactive_event_transitions_delivery"
        ),
        sa.CheckConstraint("event_type IN ('issues', 'pull_request')", name="ck_proactive_event_transitions_type"),
        sa.CheckConstraint(
            "action IN ('opened', 'edited', 'closed', 'reopened', 'synchronize')",
            name="ck_proactive_event_transitions_action",
        ),
        sa.CheckConstraint("lifecycle_state IN ('open', 'closed', 'merged')", name="ck_proactive_event_transitions_state"),
        sa.CheckConstraint("item_number > 0", name="ck_proactive_event_transitions_number"),
    )
    op.create_index(
        "ix_proactive_event_transitions_item_received",
        "proactive_event_transitions",
        ["organization_id", "project_id", "event_type", "repository_key", "item_number", "received_at"],
    )

    for table in ("github_work_item_states", "proactive_event_transitions"):
        _tenant_policy(table)

    op.execute(
        """CREATE FUNCTION reject_proactive_event_transition_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'proactive event transition history is append-only';
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER proactive_event_transitions_immutable
        BEFORE UPDATE OR DELETE ON proactive_event_transitions
        FOR EACH ROW EXECUTE FUNCTION reject_proactive_event_transition_mutation()"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE proactive_event_transitions NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM proactive_event_transitions) THEN
                RAISE EXCEPTION 'proactive lifecycle history exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("ALTER TABLE github_work_item_states NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM github_work_item_states) THEN
                RAISE EXCEPTION 'GitHub work-item state exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS proactive_event_transitions_immutable ON proactive_event_transitions")
    op.execute("DROP FUNCTION IF EXISTS reject_proactive_event_transition_mutation()")
    for table in ("proactive_event_transitions", "github_work_item_states"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_proactive_event_transitions_item_received", table_name="proactive_event_transitions")
    op.drop_table("proactive_event_transitions")
    op.drop_index("ix_github_work_item_states_project_updated", table_name="github_work_item_states")
    op.drop_table("github_work_item_states")
