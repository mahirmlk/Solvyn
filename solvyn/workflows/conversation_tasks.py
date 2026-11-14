"""Lifecycle synchronization for tasks opened from teammate conversations."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import ExecutionGraph, Task, TaskHandoff, TeammateConversation


def conversation_handoff_idempotency_key(tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"conversation-handoff:{tenant_id}:{conversation_id}")


def _graph_status(task_status: str) -> str:
    if task_status in {"completed", "failed", "cancelled"}:
        return task_status
    if task_status == "waiting_approval":
        return task_status
    if task_status == "review_required":
        return task_status
    if task_status == "pending":
        return "pending"
    return "running"


async def sync_conversation_task_state(db: AsyncSession, task: Task) -> None:
    """Mirror an execution child state to its conversation request and graph."""
    if task.parent_task_id is None or task.execution_id is None:
        return
    conversation = (
        await db.execute(
            select(TeammateConversation).where(
                TeammateConversation.tenant_id == task.tenant_id,
                TeammateConversation.session_id == task.session_id,
                TeammateConversation.root_task_id == task.parent_task_id,
            )
        )
    ).scalar_one_or_none()
    if conversation is None:
        return
    matching_handoff = (
        await db.execute(
            select(TaskHandoff.id).where(
                TaskHandoff.tenant_id == task.tenant_id,
                TaskHandoff.parent_task_id == conversation.root_task_id,
                TaskHandoff.child_task_id == task.id,
                TaskHandoff.idempotency_key == conversation_handoff_idempotency_key(task.tenant_id, conversation.id),
            )
        )
    ).scalar_one_or_none()
    if matching_handoff is None:
        return
    root = (
        await db.execute(
            select(Task)
            .where(
                Task.tenant_id == task.tenant_id,
                Task.id == conversation.root_task_id,
                Task.execution_id == task.execution_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    graph = (
        await db.execute(
            select(ExecutionGraph)
            .where(ExecutionGraph.id == task.execution_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if root is None or graph is None:
        return

    root.status = task.status
    root.error = task.error
    root.result = task.result
    root.result_summary = task.result_summary
    root.result_sha256 = task.result_sha256
    root.completed_at = task.completed_at if task.status in {"completed", "failed", "cancelled"} else None

    snapshot = json.loads(graph.graph_data)
    snapshot["status"] = _graph_status(task.status)
    for node in snapshot.get("nodes", []):
        node_id = node.get("id")
        if node_id == str(root.id):
            node["status"] = root.status
            node["error"] = root.error
            node["completed_at"] = root.completed_at.isoformat() if root.completed_at else None
        elif node_id == str(task.id):
            node["status"] = task.status
            node["error"] = task.error
            node["output"] = json.loads(task.result) if task.result else None
            node["completed_at"] = task.completed_at.isoformat() if task.completed_at else None
    graph.graph_data = json.dumps(snapshot)
    graph.status = _graph_status(task.status)
    db.add(root)
    db.add(graph)
