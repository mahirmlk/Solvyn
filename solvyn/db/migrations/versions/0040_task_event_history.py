"""Add immutable task lifecycle events captured in the task transaction.

Revision ID: 0040_task_event_history
Revises: 0039_api_key_rls
Create Date: 2026-11-06 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040_task_event_history"
down_revision: str | None = "0039_api_key_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Relationship IDs are immutable snapshots, intentionally without FKs
        # so history survives deletion of tasks and tenants.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("previous_status", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_events_task_id", "task_events", ["task_id"])
    op.create_index("ix_task_events_execution_id", "task_events", ["execution_id"])
    op.create_index(
        "ix_task_events_tenant_task_time",
        "task_events",
        ["tenant_id", "task_id", "occurred_at", "id"],
    )

    op.execute("ALTER TABLE task_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON task_events
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"""
    )

    # Existing tasks receive a truthful point-in-time baseline, not fabricated
    # transition history. Set the tenant GUC for each batch because tasks and
    # task_events both enforce RLS during migration.
    op.execute(
        """DO $$
        DECLARE tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);
                INSERT INTO task_events (
                    tenant_id, task_id, execution_id, parent_task_id, event_type,
                    previous_status, status, source, occurred_at
                )
                SELECT tenant_id, id, execution_id, parent_task_id, 'baseline',
                       NULL, status, 'migration_baseline', clock_timestamp()
                FROM tasks
                WHERE tenant_id = tenant_row.id;
            END LOOP;
        END;
        $$"""
    )

    op.execute(
        """CREATE FUNCTION capture_task_lifecycle_event() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            event_source text;
            actor_setting text;
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status IS NOT DISTINCT FROM NEW.status THEN
                RETURN NEW;
            END IF;

            event_source := COALESCE(NULLIF(current_setting('app.task_event_source', true), ''), 'database');
            actor_setting := NULLIF(current_setting('app.task_event_actor_user_id', true), '');
            INSERT INTO public.task_events (
                tenant_id, task_id, execution_id, parent_task_id, event_type,
                previous_status, status, source, actor_user_id, occurred_at
            ) VALUES (
                NEW.tenant_id, NEW.id, NEW.execution_id, NEW.parent_task_id,
                CASE WHEN TG_OP = 'INSERT' THEN 'created' ELSE 'status_changed' END,
                CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE OLD.status END,
                NEW.status, event_source, actor_setting::uuid, clock_timestamp()
            );
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_lifecycle_event
        AFTER INSERT OR UPDATE OF status ON tasks
        FOR EACH ROW EXECUTE FUNCTION capture_task_lifecycle_event()"""
    )
    op.execute(
        """CREATE FUNCTION require_task_lifecycle_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF pg_trigger_depth() < 2 THEN
                RAISE EXCEPTION 'task_events may only be inserted by the task lifecycle trigger'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_events_require_lifecycle_trigger
        BEFORE INSERT ON task_events
        FOR EACH ROW EXECUTE FUNCTION require_task_lifecycle_trigger()"""
    )
    # Deployment defaults grant INSERT on new tables to the runtime role.
    # Revoke that grant from every non-owner grantee; the SECURITY DEFINER
    # task trigger remains the only runtime write path.
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
                WHERE table_row.oid = 'public.task_events'::regclass
                  AND acl.privilege_type = 'INSERT'
                  AND acl.grantee <> table_row.relowner
            LOOP
                EXECUTE format('REVOKE INSERT ON TABLE public.task_events FROM %s', grantee_row.role_name);
            END LOOP;
        END;
        $$"""
    )
    op.execute(
        """CREATE FUNCTION reject_task_event_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'task_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_events_reject_row_mutation
        BEFORE UPDATE OR DELETE ON task_events
        FOR EACH ROW EXECUTE FUNCTION reject_task_event_mutation()"""
    )
    op.execute(
        """CREATE TRIGGER task_events_reject_truncate
        BEFORE TRUNCATE ON task_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_task_event_mutation()"""
    )


def downgrade() -> None:
    # Never silently discard the durable audit trail. A rollback is possible
    # only before the table contains any events (for example, before the
    # first task exists); otherwise export/archive the history explicitly.
    op.execute("ALTER TABLE task_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE task_events DISABLE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM task_events) THEN
                RAISE EXCEPTION 'task_events contains durable history; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS task_events_reject_truncate ON task_events")
    op.execute("DROP TRIGGER IF EXISTS task_events_reject_row_mutation ON task_events")
    op.execute("DROP TRIGGER IF EXISTS task_events_require_lifecycle_trigger ON task_events")
    op.execute("DROP TRIGGER IF EXISTS task_lifecycle_event ON tasks")
    op.execute("DROP FUNCTION IF EXISTS reject_task_event_mutation()")
    op.execute("DROP FUNCTION IF EXISTS require_task_lifecycle_trigger()")
    op.execute("DROP FUNCTION IF EXISTS capture_task_lifecycle_event()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON task_events")
    op.drop_table("task_events")
