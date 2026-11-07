"""Capture workflow execution scheduling, claims, and terminal transitions.

Revision ID: 0046_workflow_execution_history
Revises: 0045_task_scope_history
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046_workflow_execution_history"
down_revision: str | None = "0045_task_scope_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_orchestration_events_tenant_execution_time",
        "orchestration_events",
        ["tenant_id", "execution_id", "occurred_at", "id"],
    )
    op.drop_constraint("ck_orchestration_events_entity_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_entity_type",
        "orchestration_events",
        "entity_type IN ('approval', 'handoff', 'acceptance_criterion', 'dependency', 'workflow_execution')",
    )
    op.drop_constraint("ck_orchestration_events_event_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_event_type",
        "orchestration_events",
        "event_type IN ('created', 'status_changed', 'updated', 'deleted', 'claimed')",
    )
    op.execute(
        """CREATE FUNCTION capture_workflow_execution_history() RETURNS trigger
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
                -- A reclaimed run can remain `resuming`; lease-owner changes
                -- record the claim without recording heartbeat renewals.
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
    op.execute(
        """CREATE TRIGGER workflow_execution_history
        AFTER INSERT OR UPDATE OF status, lease_owner ON execution_graphs
        FOR EACH ROW EXECUTE FUNCTION capture_workflow_execution_history()"""
    )


def downgrade() -> None:
    # Keep the append-only audit trail intact; downgrade is safe only before
    # this migration has captured any execution lifecycle events.
    op.execute("ALTER TABLE orchestration_events NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM orchestration_events
                WHERE entity_type = 'workflow_execution'
            ) THEN
                RAISE EXCEPTION 'workflow execution history exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS workflow_execution_history ON execution_graphs")
    op.execute("DROP FUNCTION IF EXISTS capture_workflow_execution_history()")
    op.drop_index("ix_orchestration_events_tenant_execution_time", table_name="orchestration_events")
    op.drop_constraint("ck_orchestration_events_event_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_event_type",
        "orchestration_events",
        "event_type IN ('created', 'status_changed', 'updated', 'deleted')",
    )
    op.drop_constraint("ck_orchestration_events_entity_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_entity_type",
        "orchestration_events",
        "entity_type IN ('approval', 'handoff', 'acceptance_criterion', 'dependency')",
    )
    op.execute("ALTER TABLE orchestration_events FORCE ROW LEVEL SECURITY")
