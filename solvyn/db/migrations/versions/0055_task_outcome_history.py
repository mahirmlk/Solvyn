"""Persist immutable task result summaries and digests.

Revision ID: 0055_task_outcome_history
Revises: 0054_task_scoped_token_usage
Create Date: 2026-11-08 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055_task_outcome_history"
down_revision: str | None = "0054_task_scoped_token_usage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("result_summary", sa.String(length=512), nullable=True))
    op.add_column("tasks", sa.Column("result_sha256", sa.String(length=64), nullable=True))
    op.add_column(
        "task_events",
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(
        """CREATE FUNCTION capture_task_outcome_event() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            event_source text;
            actor_setting text;
            event_outcome text;
            event_summary text;
        BEGIN
            IF NEW.status NOT IN ('completed', 'failed', 'cancelled') THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE'
                AND OLD.status IS NOT DISTINCT FROM NEW.status
                AND OLD.result_summary IS NOT DISTINCT FROM NEW.result_summary
                AND OLD.result_sha256 IS NOT DISTINCT FROM NEW.result_sha256
                AND OLD.error IS NOT DISTINCT FROM NEW.error THEN
                RETURN NEW;
            END IF;

            event_source := COALESCE(NULLIF(current_setting('app.task_event_source', true), ''), 'database');
            actor_setting := NULLIF(current_setting('app.task_event_actor_user_id', true), '');
            event_outcome := CASE NEW.status
                WHEN 'completed' THEN 'success'
                WHEN 'cancelled' THEN 'cancelled'
                ELSE 'failure'
            END;
            event_summary := COALESCE(
                NULLIF(NEW.result_summary, ''),
                CASE NEW.status
                    WHEN 'completed' THEN 'Task completed with no saved summary.'
                    WHEN 'cancelled' THEN 'Task was cancelled.'
                    ELSE 'Task failed during execution.'
                END
            );
            INSERT INTO public.task_events (
                tenant_id, task_id, execution_id, parent_task_id, event_type,
                previous_status, status, source, actor_user_id, payload, occurred_at
            ) VALUES (
                NEW.tenant_id, NEW.id, NEW.execution_id, NEW.parent_task_id, 'result_recorded',
                CASE WHEN TG_OP = 'UPDATE' THEN OLD.status ELSE NULL END,
                NEW.status, event_source, actor_setting::uuid,
                jsonb_build_object(
                    'outcome', event_outcome,
                    'summary', left(event_summary, 512),
                    'result_sha256', NEW.result_sha256,
                    'attempt_count', NEW.attempt_count
                ),
                clock_timestamp()
            );
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_outcome_event
        AFTER INSERT OR UPDATE OF status, result_summary, result_sha256, error ON tasks
        FOR EACH ROW EXECUTE FUNCTION capture_task_outcome_event()"""
    )


def downgrade() -> None:
    op.execute("ALTER TABLE task_events NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM task_events WHERE event_type = 'result_recorded') THEN
                RAISE EXCEPTION 'task outcome history exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("ALTER TABLE task_events FORCE ROW LEVEL SECURITY")
    op.execute("DROP TRIGGER IF EXISTS task_outcome_event ON tasks")
    op.execute("DROP FUNCTION IF EXISTS capture_task_outcome_event()")
    op.drop_column("task_events", "payload")
    op.drop_column("tasks", "result_sha256")
    op.drop_column("tasks", "result_summary")
