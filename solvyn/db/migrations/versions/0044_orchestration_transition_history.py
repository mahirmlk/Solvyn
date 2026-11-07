"""Capture immutable approval and task handoff status transitions.

Revision ID: 0044_orchestration_transition_history
Revises: 0043_verification_attempts
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_orchestration_transition_history"
down_revision: str | None = "0043_verification_attempts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "orchestration_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Historical references intentionally have no foreign keys so events
        # survive deletion of their task, approval, handoff, or tenant.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("entity_type", sa.String(length=20), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("previous_status", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("entity_type IN ('approval', 'handoff')", name="ck_orchestration_events_entity_type"),
        sa.CheckConstraint("event_type IN ('created', 'status_changed')", name="ck_orchestration_events_event_type"),
    )
    op.create_index(
        "ix_orchestration_events_tenant_task_time",
        "orchestration_events",
        ["tenant_id", "task_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_orchestration_events_tenant_entity_time",
        "orchestration_events",
        ["tenant_id", "entity_type", "entity_id", "occurred_at", "id"],
    )
    op.execute("ALTER TABLE orchestration_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE orchestration_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON orchestration_events
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )

    op.execute(
        """CREATE FUNCTION capture_orchestration_transition() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            event_tenant uuid;
            event_task uuid;
            event_execution uuid;
            event_entity_type text;
            prior_status text;
            event_source text;
            actor_setting text;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
                RETURN NEW;
            END IF;

            IF TG_TABLE_NAME = 'task_handoffs' THEN
                event_tenant := NEW.tenant_id;
                event_task := NEW.child_task_id;
                event_entity_type := 'handoff';
                prior_status := CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END;
            ELSE
                SELECT organization_row.tenant_id
                  INTO event_tenant
                  FROM public.organizations organization_row
                 WHERE organization_row.id = NEW.organization_id;
                event_entity_type := 'approval';
                prior_status := CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END;
                IF NEW.task_handoff_id IS NOT NULL THEN
                    SELECT handoff_row.child_task_id
                      INTO event_task
                      FROM public.task_handoffs handoff_row
                     WHERE handoff_row.id = NEW.task_handoff_id
                       AND handoff_row.tenant_id = event_tenant;
                ELSIF NEW.verification_run_id IS NOT NULL THEN
                    SELECT run_row.task_id
                      INTO event_task
                      FROM public.verification_runs run_row
                     WHERE run_row.id = NEW.verification_run_id
                       AND run_row.tenant_id = event_tenant;
                END IF;
            END IF;

            IF event_tenant IS NULL THEN
                RAISE EXCEPTION 'Cannot determine tenant for orchestration history'
                    USING ERRCODE = '23514';
            END IF;
            IF event_task IS NOT NULL THEN
                SELECT task_row.execution_id
                  INTO event_execution
                  FROM public.tasks task_row
                 WHERE task_row.id = event_task
                   AND task_row.tenant_id = event_tenant;
            END IF;

            event_source := COALESCE(NULLIF(current_setting('app.task_event_source', true), ''), 'database');
            actor_setting := NULLIF(current_setting('app.task_event_actor_user_id', true), '');
            INSERT INTO public.orchestration_events (
                tenant_id, task_id, execution_id, entity_type, entity_id,
                event_type, previous_status, status, source, actor_user_id, occurred_at
            ) VALUES (
                event_tenant, event_task, event_execution, event_entity_type, NEW.id,
                CASE WHEN TG_OP = 'INSERT' THEN 'created' ELSE 'status_changed' END,
                prior_status, NEW.status, event_source, actor_setting::uuid, clock_timestamp()
            );
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_handoff_history
        AFTER INSERT OR UPDATE OF status ON task_handoffs
        FOR EACH ROW EXECUTE FUNCTION capture_orchestration_transition()"""
    )
    op.execute(
        """CREATE TRIGGER approval_history
        AFTER INSERT OR UPDATE OF status ON approvals
        FOR EACH ROW EXECUTE FUNCTION capture_orchestration_transition()"""
    )

    # Default deployment privileges grant runtime DML on new tables. Keep
    # writes available only through the SECURITY DEFINER state-change triggers.
    op.execute(
        """DO $$
        DECLARE grantee_row record;
        BEGIN
            FOR grantee_row IN
                SELECT DISTINCT CASE
                    WHEN acl.grantee = 0 THEN 'PUBLIC'
                    ELSE quote_ident(role_row.rolname)
                END AS role_name
                FROM pg_class table_row
                CROSS JOIN LATERAL aclexplode(table_row.relacl) acl
                LEFT JOIN pg_roles role_row ON role_row.oid = acl.grantee
                WHERE table_row.oid = 'public.orchestration_events'::regclass
                  AND acl.privilege_type IN ('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE')
                  AND acl.grantee <> table_row.relowner
            LOOP
                EXECUTE format(
                    'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public.orchestration_events FROM %s',
                    grantee_row.role_name
                );
            END LOOP;
        END;
        $$"""
    )
    op.execute(
        """CREATE FUNCTION reject_orchestration_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'orchestration_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER orchestration_events_reject_row_mutation
        BEFORE UPDATE OR DELETE ON orchestration_events
        FOR EACH ROW EXECUTE FUNCTION reject_orchestration_event_mutation()"""
    )
    op.execute(
        """CREATE TRIGGER orchestration_events_reject_truncate
        BEFORE TRUNCATE ON orchestration_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_orchestration_event_mutation()"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE orchestration_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE orchestration_events DISABLE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM orchestration_events) THEN
                RAISE EXCEPTION 'orchestration_events contains durable history; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS orchestration_events_reject_truncate ON orchestration_events")
    op.execute("DROP TRIGGER IF EXISTS orchestration_events_reject_row_mutation ON orchestration_events")
    op.execute("DROP TRIGGER IF EXISTS approval_history ON approvals")
    op.execute("DROP TRIGGER IF EXISTS task_handoff_history ON task_handoffs")
    op.execute("DROP FUNCTION IF EXISTS reject_orchestration_event_mutation()")
    op.execute("DROP FUNCTION IF EXISTS capture_orchestration_transition()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON orchestration_events")
    op.drop_table("orchestration_events")
