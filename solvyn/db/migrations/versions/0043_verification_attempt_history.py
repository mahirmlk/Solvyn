"""Capture immutable verification attempt lifecycle and results.

Revision ID: 0043_verification_attempts
Revises: 0042_verification_run_recovery
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0043_verification_attempts"
down_revision: str | None = "0042_verification_run_recovery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_attempt_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        # Historical references intentionally have no FKs so audit evidence
        # survives task, run, or tenant deletion.
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verification_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("verification_kind", sa.String(length=30), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("output_truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("stdout", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("stderr", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(10, 3), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("attempt_number >= 0", name="ck_verification_attempt_events_attempt_number"),
        sa.CheckConstraint(
            "event_type IN ('started', 'finished', 'snapshot')",
            name="ck_verification_attempt_events_type",
        ),
    )
    op.create_index(
        "ix_verification_attempt_events_tenant_task_time",
        "verification_attempt_events",
        ["tenant_id", "task_id", "occurred_at", "id"],
    )
    op.create_index(
        "ix_verification_attempt_events_run_attempt",
        "verification_attempt_events",
        ["verification_run_id", "attempt_number", "occurred_at"],
    )

    op.execute("ALTER TABLE verification_attempt_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verification_attempt_events FORCE ROW LEVEL SECURITY")
    op.execute(
        """CREATE POLICY tenant_isolation ON verification_attempt_events
        USING (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
        )
        WITH CHECK (
            tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid
            AND EXISTS (
                SELECT 1 FROM tasks task_row
                WHERE task_row.id = verification_attempt_events.task_id
                  AND task_row.tenant_id = verification_attempt_events.tenant_id
            )
            AND EXISTS (
                SELECT 1 FROM verification_runs run_row
                WHERE run_row.id = verification_attempt_events.verification_run_id
                  AND run_row.task_id = verification_attempt_events.task_id
                  AND run_row.tenant_id = verification_attempt_events.tenant_id
            )
        )"""
    )

    # Snapshot only in-flight/recoverable rows, where the next recovery could
    # replace visible fields. Retained output is capped at 32,768 characters per stream;
    # terminal results remain in verification_runs.
    op.execute(
        """DO $$
        DECLARE tenant_row record;
        BEGIN
            FOR tenant_row IN SELECT id FROM tenants ORDER BY id LOOP
                PERFORM set_config('app.tenant_id', tenant_row.id::text, true);
                INSERT INTO verification_attempt_events (
                    tenant_id, task_id, verification_run_id, verification_kind,
                    attempt_number, event_type, status, exit_code, timed_out,
                    output_truncated, stdout, stderr, error, duration_seconds, occurred_at
                )
                SELECT tenant_id, task_id, id, kind, attempt_count, 'snapshot', status,
                       exit_code, timed_out,
                       output_truncated OR length(stdout) > 32768 OR length(stderr) > 32768,
                       left(stdout, 32768), left(stderr, 32768), left(error, 4000),
                       duration_seconds, clock_timestamp()
                FROM verification_runs
                WHERE tenant_id = tenant_row.id
                  AND status IN ('running', 'interrupted');
            END LOOP;
        END;
        $$"""
    )

    op.execute(
        """CREATE FUNCTION capture_verification_attempt_event() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NEW.attempt_count > OLD.attempt_count AND NEW.status = 'running' THEN
                INSERT INTO public.verification_attempt_events (
                    tenant_id, task_id, verification_run_id, verification_kind,
                    attempt_number, event_type, status, occurred_at
                ) VALUES (
                    NEW.tenant_id, NEW.task_id, NEW.id, NEW.kind,
                    NEW.attempt_count, 'started', NEW.status, clock_timestamp()
                );
            ELSIF OLD.status = 'running' AND NEW.status <> 'running' THEN
                INSERT INTO public.verification_attempt_events (
                    tenant_id, task_id, verification_run_id, verification_kind,
                    attempt_number, event_type, status, exit_code, timed_out,
                    output_truncated, stdout, stderr, error, duration_seconds, occurred_at
                ) VALUES (
                    NEW.tenant_id, NEW.task_id, NEW.id, NEW.kind,
                    NEW.attempt_count, 'finished', NEW.status, NEW.exit_code,
                    NEW.timed_out,
                    NEW.output_truncated OR length(NEW.stdout) > 32768 OR length(NEW.stderr) > 32768,
                    left(NEW.stdout, 32768), left(NEW.stderr, 32768),
                    left(NEW.error, 4000), NEW.duration_seconds, clock_timestamp()
                );
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER verification_attempt_event_capture
        AFTER UPDATE OF attempt_count, status ON verification_runs
        FOR EACH ROW EXECUTE FUNCTION capture_verification_attempt_event()"""
    )
    op.execute(
        """CREATE FUNCTION require_verification_attempt_trigger() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF pg_trigger_depth() < 2 THEN
                RAISE EXCEPTION 'verification attempt events may only be inserted by the run capture trigger'
                    USING ERRCODE = '55000';
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER verification_attempt_events_require_capture
        BEFORE INSERT ON verification_attempt_events
        FOR EACH ROW EXECUTE FUNCTION require_verification_attempt_trigger()"""
    )
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
                WHERE table_row.oid = 'public.verification_attempt_events'::regclass
                  AND acl.privilege_type = 'INSERT'
                  AND acl.grantee <> table_row.relowner
            LOOP
                EXECUTE format('REVOKE INSERT ON TABLE public.verification_attempt_events FROM %s', grantee_row.role_name);
            END LOOP;
        END;
        $$"""
    )
    op.execute(
        """CREATE FUNCTION reject_verification_attempt_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'verification_attempt_events is append-only'
                USING ERRCODE = '55000';
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER verification_attempt_events_reject_mutation
        BEFORE UPDATE OR DELETE ON verification_attempt_events
        FOR EACH ROW EXECUTE FUNCTION reject_verification_attempt_mutation()"""
    )
    op.execute(
        """CREATE TRIGGER verification_attempt_events_reject_truncate
        BEFORE TRUNCATE ON verification_attempt_events
        FOR EACH STATEMENT EXECUTE FUNCTION reject_verification_attempt_mutation()"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE verification_attempt_events NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verification_attempt_events DISABLE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM verification_attempt_events) THEN
                RAISE EXCEPTION 'verification attempt history exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS verification_attempt_events_reject_truncate ON verification_attempt_events")
    op.execute("DROP TRIGGER IF EXISTS verification_attempt_events_reject_mutation ON verification_attempt_events")
    op.execute("DROP TRIGGER IF EXISTS verification_attempt_events_require_capture ON verification_attempt_events")
    op.execute("DROP TRIGGER IF EXISTS verification_attempt_event_capture ON verification_runs")
    op.execute("DROP FUNCTION IF EXISTS reject_verification_attempt_mutation()")
    op.execute("DROP FUNCTION IF EXISTS require_verification_attempt_trigger()")
    op.execute("DROP FUNCTION IF EXISTS capture_verification_attempt_event()")
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON verification_attempt_events")
    op.drop_table("verification_attempt_events")
