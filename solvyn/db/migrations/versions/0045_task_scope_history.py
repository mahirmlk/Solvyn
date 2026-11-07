"""Capture acceptance-criteria and dependency history.

Revision ID: 0045_task_scope_history
Revises: 0044_orchestration_history
Create Date: 2026-11-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0045_task_scope_history"
down_revision: str | None = "0044_orchestration_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orchestration_events",
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint("ck_orchestration_events_entity_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_entity_type",
        "orchestration_events",
        "entity_type IN ('approval', 'handoff', 'acceptance_criterion', 'dependency')",
    )
    op.drop_constraint("ck_orchestration_events_event_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_event_type",
        "orchestration_events",
        "event_type IN ('created', 'status_changed', 'updated', 'deleted')",
    )
    op.execute(
        """CREATE FUNCTION capture_task_scope_history() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER
        SET search_path = pg_catalog, public AS $$
        DECLARE
            event_tenant uuid;
            event_task uuid;
            event_execution uuid;
            event_entity uuid;
            event_entity_type text;
            event_type text;
            event_status text;
            prior_status text;
            event_payload jsonb;
            event_source text;
            actor_setting text;
        BEGIN
            IF TG_TABLE_NAME = 'task_acceptance_criteria' THEN
                IF TG_OP = 'DELETE' THEN
                    event_tenant := OLD.tenant_id;
                    event_task := OLD.task_id;
                    event_entity := OLD.id;
                    event_entity_type := 'acceptance_criterion';
                    event_type := 'deleted';
                    event_status := 'deleted';
                    prior_status := OLD.status;
                    event_payload := jsonb_build_object(
                        'text', OLD.text,
                        'decision_note', OLD.decision_note,
                        'reviewer_user_id', OLD.reviewer_user_id,
                        'reviewer_organization_id', OLD.reviewer_organization_id
                    );
                    actor_setting := NULLIF(current_setting('app.task_event_actor_user_id', true), '');
                ELSE
                    event_tenant := NEW.tenant_id;
                    event_task := NEW.task_id;
                    event_entity := NEW.id;
                    event_entity_type := 'acceptance_criterion';
                    event_status := NEW.status;
                    event_payload := jsonb_build_object(
                        'text', NEW.text,
                        'decision_note', NEW.decision_note,
                        'reviewer_user_id', NEW.reviewer_user_id,
                        'reviewer_organization_id', NEW.reviewer_organization_id,
                        'previous_text', CASE WHEN TG_OP = 'UPDATE' THEN OLD.text ELSE NULL END,
                        'previous_decision_note', CASE WHEN TG_OP = 'UPDATE' THEN OLD.decision_note ELSE NULL END,
                        'previous_reviewer_user_id', CASE WHEN TG_OP = 'UPDATE' THEN OLD.reviewer_user_id ELSE NULL END,
                        'previous_reviewer_organization_id', CASE WHEN TG_OP = 'UPDATE' THEN OLD.reviewer_organization_id ELSE NULL END
                    );
                    actor_setting := COALESCE(
                        NULLIF(current_setting('app.task_event_actor_user_id', true), ''),
                        NEW.reviewer_user_id::text
                    );
                    IF TG_OP = 'INSERT' THEN
                        event_type := 'created';
                    ELSE
                        prior_status := OLD.status;
                        IF OLD.status IS DISTINCT FROM NEW.status THEN
                            event_type := 'status_changed';
                        ELSIF OLD.text IS DISTINCT FROM NEW.text
                            OR OLD.decision_note IS DISTINCT FROM NEW.decision_note
                            OR OLD.reviewer_user_id IS DISTINCT FROM NEW.reviewer_user_id
                            OR OLD.reviewer_organization_id IS DISTINCT FROM NEW.reviewer_organization_id THEN
                            event_type := 'updated';
                        ELSE
                            RETURN NEW;
                        END IF;
                    END IF;
                END IF;
            ELSE
                IF TG_OP = 'DELETE' THEN
                    event_tenant := OLD.tenant_id;
                    event_task := OLD.task_id;
                    event_entity := OLD.id;
                    event_type := 'deleted';
                    event_status := 'deleted';
                    prior_status := 'active';
                    event_payload := jsonb_build_object('depends_on_task_id', OLD.depends_on_task_id);
                ELSE
                    IF TG_OP = 'UPDATE'
                        AND OLD.task_id IS NOT DISTINCT FROM NEW.task_id
                        AND OLD.depends_on_task_id IS NOT DISTINCT FROM NEW.depends_on_task_id THEN
                        RETURN NEW;
                    END IF;
                    event_tenant := NEW.tenant_id;
                    event_task := NEW.task_id;
                    event_entity := NEW.id;
                    event_type := CASE WHEN TG_OP = 'INSERT' THEN 'created' ELSE 'updated' END;
                    event_status := 'active';
                    prior_status := CASE WHEN TG_OP = 'UPDATE' THEN 'active' ELSE NULL END;
                    event_payload := jsonb_build_object(
                        'depends_on_task_id', NEW.depends_on_task_id,
                        'previous_task_id', CASE WHEN TG_OP = 'UPDATE' THEN OLD.task_id ELSE NULL END,
                        'previous_depends_on_task_id', CASE WHEN TG_OP = 'UPDATE' THEN OLD.depends_on_task_id ELSE NULL END
                    );
                END IF;
                event_entity_type := 'dependency';
                actor_setting := NULLIF(current_setting('app.task_event_actor_user_id', true), '');
            END IF;

            SELECT task_row.execution_id
              INTO event_execution
              FROM public.tasks task_row
             WHERE task_row.id = event_task
               AND task_row.tenant_id = event_tenant;

            event_source := COALESCE(NULLIF(current_setting('app.task_event_source', true), ''), 'database');
            INSERT INTO public.orchestration_events (
                tenant_id, task_id, execution_id, entity_type, entity_id,
                event_type, previous_status, status, source, actor_user_id, payload, occurred_at
            ) VALUES (
                event_tenant, event_task, event_execution, event_entity_type, event_entity,
                event_type, prior_status, event_status, event_source, actor_setting::uuid, event_payload, clock_timestamp()
            );
            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$"""
    )
    op.execute(
        """CREATE TRIGGER task_acceptance_criteria_history
        AFTER INSERT OR UPDATE OR DELETE ON task_acceptance_criteria
        FOR EACH ROW EXECUTE FUNCTION capture_task_scope_history()"""
    )
    op.execute(
        """CREATE TRIGGER task_dependency_history
        AFTER INSERT OR UPDATE OR DELETE ON task_dependencies
        FOR EACH ROW EXECUTE FUNCTION capture_task_scope_history()"""
    )


def downgrade() -> None:
    # The migration owner owns this table. Temporarily remove FORCE so this
    # preflight can see events from every tenant; a raised exception rolls
    # this DDL back and restores FORCE with the rest of the transaction.
    op.execute("ALTER TABLE orchestration_events NO FORCE ROW LEVEL SECURITY")
    op.execute(
        """DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM orchestration_events
                WHERE entity_type IN ('acceptance_criterion', 'dependency')
            ) THEN
                RAISE EXCEPTION 'task scope history exists; export/archive it before downgrading';
            END IF;
        END;
        $$"""
    )
    op.execute("DROP TRIGGER IF EXISTS task_dependency_history ON task_dependencies")
    op.execute("DROP TRIGGER IF EXISTS task_acceptance_criteria_history ON task_acceptance_criteria")
    op.execute("DROP FUNCTION IF EXISTS capture_task_scope_history()")
    op.drop_constraint("ck_orchestration_events_event_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_event_type",
        "orchestration_events",
        "event_type IN ('created', 'status_changed')",
    )
    op.drop_constraint("ck_orchestration_events_entity_type", "orchestration_events", type_="check")
    op.create_check_constraint(
        "ck_orchestration_events_entity_type",
        "orchestration_events",
        "entity_type IN ('approval', 'handoff')",
    )
    op.drop_column("orchestration_events", "payload")
    op.execute("ALTER TABLE orchestration_events FORCE ROW LEVEL SECURITY")
