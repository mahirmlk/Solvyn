"""Bound workflow lease recovery and retain retry lineage.

Revision ID: 0067_workflow_execution_recovery
Revises: 0066_npm_dependency_evidence
Create Date: 2026-11-24 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "0067_workflow_execution_recovery"
down_revision: str | None = "0066_npm_dependency_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "execution_graphs",
        sa.Column("lease_reclaim_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("execution_graphs", sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("execution_graphs", sa.Column("dead_letter_reason", sa.String(length=2000), nullable=True))
    op.add_column("execution_graphs", sa.Column("redriven_from_execution_id", UUID(as_uuid=True), nullable=True))
    op.create_check_constraint(
        "ck_execution_graphs_lease_reclaim_count", "execution_graphs", "lease_reclaim_count >= 0"
    )
    op.create_unique_constraint("uq_execution_graphs_session_id_id", "execution_graphs", ["session_id", "id"])
    op.create_unique_constraint(
        "uq_execution_graphs_redrive_source",
        "execution_graphs",
        ["session_id", "redriven_from_execution_id"],
    )
    op.create_foreign_key(
        "fk_execution_graphs_redrive_same_session",
        "execution_graphs",
        "execution_graphs",
        ["session_id", "redriven_from_execution_id"],
        ["session_id", "id"],
        ondelete="CASCADE",
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION capture_workflow_execution_history() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            event_tenant uuid;
            event_type text;
            prior_status text;
            event_source text;
            actor_setting text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                event_type := 'created';
            ELSIF OLD.status IS DISTINCT FROM NEW.status THEN
                event_type := 'status_changed';
                prior_status := OLD.status;
            ELSIF OLD.lease_owner IS DISTINCT FROM NEW.lease_owner AND NEW.lease_owner IS NOT NULL
                AND NEW.status IN ('running', 'resuming') THEN
                event_type := 'claimed';
            ELSE
                RETURN NEW;
            END IF;

            SELECT session_row.tenant_id
              INTO event_tenant
              FROM public.sessions session_row
             WHERE session_row.id = NEW.session_id;
            IF event_tenant IS NULL THEN
                RAISE EXCEPTION 'workflow execution session has no owning tenant';
            END IF;

            event_source := COALESCE(NULLIF(current_setting('app.task_event_source', true), ''), 'workflow_executor');
            actor_setting := NULLIF(current_setting('app.task_event_actor_user_id', true), '');
            INSERT INTO public.orchestration_events (
                tenant_id, task_id, execution_id, entity_type, entity_id,
                event_type, previous_status, status, source, actor_user_id, occurred_at
            ) VALUES (
                event_tenant, NULL, NEW.id, 'workflow_execution', NEW.id,
                event_type, prior_status, NEW.status, event_source, actor_setting::uuid, clock_timestamp()
            );
            RETURN NEW;
        END;
        $$"""
    )


def downgrade() -> None:
    op.execute(
        """CREATE OR REPLACE FUNCTION capture_workflow_execution_history() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            event_tenant uuid;
            event_type text;
            prior_status text;
        BEGIN
            IF TG_OP = 'INSERT' THEN
                event_type := 'created';
            ELSIF OLD.status IS DISTINCT FROM NEW.status THEN
                event_type := 'status_changed';
                prior_status := OLD.status;
            ELSIF OLD.lease_owner IS DISTINCT FROM NEW.lease_owner AND NEW.lease_owner IS NOT NULL
                AND NEW.status IN ('running', 'resuming') THEN
                event_type := 'claimed';
            ELSE
                RETURN NEW;
            END IF;
            SELECT session_row.tenant_id INTO event_tenant
              FROM public.sessions session_row WHERE session_row.id = NEW.session_id;
            IF event_tenant IS NULL THEN
                RAISE EXCEPTION 'workflow execution session has no owning tenant';
            END IF;
            INSERT INTO public.orchestration_events (
                tenant_id, task_id, execution_id, entity_type, entity_id,
                event_type, previous_status, status, source, occurred_at
            ) VALUES (
                event_tenant, NULL, NEW.id, 'workflow_execution', NEW.id,
                event_type, prior_status, NEW.status, 'workflow_executor', clock_timestamp()
            );
            RETURN NEW;
        END;
        $$"""
    )
    op.drop_constraint("fk_execution_graphs_redrive_same_session", "execution_graphs", type_="foreignkey")
    op.drop_constraint("uq_execution_graphs_redrive_source", "execution_graphs", type_="unique")
    op.drop_constraint("uq_execution_graphs_session_id_id", "execution_graphs", type_="unique")
    op.drop_constraint("ck_execution_graphs_lease_reclaim_count", "execution_graphs", type_="check")
    op.drop_column("execution_graphs", "redriven_from_execution_id")
    op.drop_column("execution_graphs", "dead_letter_reason")
    op.drop_column("execution_graphs", "dead_lettered_at")
    op.drop_column("execution_graphs", "lease_reclaim_count")
