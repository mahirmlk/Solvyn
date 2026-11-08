"""Helpers for creating bounded proactive engineering signals."""

from __future__ import annotations

import uuid

from solvyn.db.models import ProactiveEvent, Task, TaskHandoff


def task_assignment_event(
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    handoff: TaskHandoff,
    task: Task,
) -> ProactiveEvent:
    """Build an idempotently keyed task assignment signal for human triage."""
    source_key = f"task-handoff:{handoff.id}:assigned"
    assigned_to = (task.assigned_agent or "teammate")[:100]
    title = task.title[:120]
    return ProactiveEvent(
        organization_id=organization_id,
        project_id=project_id,
        delivery_id=f"task-assignment:{handoff.id}"[:128],
        source_event_id=source_key[:320],
        event_type="task_assignment",
        action="assigned",
        repository_name="Task assignment",
        workflow_name=f"{assigned_to} assigned: {title}"[:255],
        conclusion="assigned",
        branch="",
        details_url="",
        status="pending",
    )
