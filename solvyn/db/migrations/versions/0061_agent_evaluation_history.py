"""Store tenant-scoped, immutable agent evaluation summaries.

Revision ID: 0061_agent_evaluation_history
Revises: 0060_conversation_task_bridge
Create Date: 2026-11-15 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061_agent_evaluation_history"
down_revision: str | None = "0060_conversation_task_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "agent_evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_type", sa.String(length=100), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_version", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("length(agent_type) > 0", name="ck_agent_evaluation_runs_agent_type_nonempty"),
        sa.CheckConstraint("length(dataset_id) > 0", name="ck_agent_evaluation_runs_dataset_id_nonempty"),
        sa.CheckConstraint("length(dataset_version) > 0", name="ck_agent_evaluation_runs_dataset_version_nonempty"),
        sa.CheckConstraint("jsonb_typeof(summary) = 'object'", name="ck_agent_evaluation_runs_summary_object"),
    )
    op.create_index(
        "ix_agent_evaluation_runs_tenant_created",
        "agent_evaluation_runs",
        ["tenant_id", "created_at", "id"],
    )
    op.create_index(
        "ix_agent_evaluation_runs_tenant_task_created",
        "agent_evaluation_runs",
        ["tenant_id", "task_id", "created_at"],
    )
    op.execute("ALTER TABLE agent_evaluation_runs ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE agent_evaluation_runs FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON agent_evaluation_runs
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )
    op.execute(
        """CREATE FUNCTION reject_agent_evaluation_run_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'agent evaluation history is append-only';
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER agent_evaluation_runs_immutable
        BEFORE UPDATE OR DELETE ON agent_evaluation_runs
        FOR EACH ROW EXECUTE FUNCTION reject_agent_evaluation_run_mutation()"""
    )


def downgrade() -> None:
    # Downgrading must not silently erase evaluation history. Removing the
    # forced owner bypass only inside this migration transaction lets the
    # table owner check for any rows before deciding whether it is safe.
    op.execute("ALTER TABLE agent_evaluation_runs NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM agent_evaluation_runs) THEN
                RAISE EXCEPTION 'agent evaluation history exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS agent_evaluation_runs_immutable ON agent_evaluation_runs")
    op.execute("DROP FUNCTION IF EXISTS reject_agent_evaluation_run_mutation()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON agent_evaluation_runs")
    op.execute("ALTER TABLE agent_evaluation_runs DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_agent_evaluation_runs_tenant_task_created", table_name="agent_evaluation_runs")
    op.drop_index("ix_agent_evaluation_runs_tenant_created", table_name="agent_evaluation_runs")
    op.drop_table("agent_evaluation_runs")
