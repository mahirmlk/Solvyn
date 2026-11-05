"""Load persisted teammate identity and project scope into the shared agent runtime."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.registry import agent_registry
from solvyn.db.models import (
    Project,
    ProjectKnowledge,
    TeammateMemory,
    TeammatePermission,
    TeammateProfile,
    TeammateProjectAssignment,
    TeammateStatus,
)

ROLE_AGENT_TYPES = {
    "Tech Lead": "system-designer",
    "Software Engineer": "backend-engineer",
    "QA Engineer": "qa-engineer",
    "Security Engineer": "security-auditor",
}
_MAX_CONTEXT_CHARS = 24_000
_MAX_KNOWLEDGE_ROWS = 100
_MAX_MEMORY_ROWS = 50
_DEFAULT_MAX_TOKENS = 4_096
_HARD_MAX_TOKENS = 16_000
_DEFAULT_MAX_RUN_SECONDS = 300
_HARD_MAX_RUN_SECONDS = 900


class TeammateRunConflictError(RuntimeError):
    """Raised when a teammate is already running or cannot be claimed."""


class TeammateRunDeniedError(PermissionError):
    """Raised when a teammate or project is outside the caller's run scope."""


def validate_role_agent_type(role: str, agent_type: str) -> None:
    expected = ROLE_AGENT_TYPES.get(role)
    if expected is None:
        raise ValueError(f"Unsupported teammate role: {role}")
    if agent_type != expected:
        raise ValueError(f"Role '{role}' requires agent type '{expected}'")
    if agent_registry.get_metadata(agent_type) is None:
        raise ValueError(f"Unknown agent type: {agent_type}")


def build_teammate_system_context(
    teammate: TeammateProfile,
    project: Project,
    knowledge: list[ProjectKnowledge],
    permissions: list[str],
    memory: list[TeammateMemory] | None = None,
) -> str:
    """Format bounded, assigned-project context for the agent's model call."""
    sections = [
        f"Teammate role: {teammate.role}",
        f"Responsibilities: {(teammate.responsibilities or '').strip()[:4_000]}",
        f"Additional teammate instructions: {(teammate.system_instructions or '').strip()[:4_000]}",
        "Assigned project context:",
        f"Project: {project.name} ({project.slug})",
        f"Description: {(project.description or '').strip()[:4_000]}",
        f"Project instructions: {(project.instructions or '').strip()[:4_000]}",
    ]
    if knowledge:
        sections.append("Project knowledge:")
        sections.extend(
            f"- [{row.category}] {row.key}: {row.value[:2_000]}" for row in knowledge
        )
    if memory:
        sections.append("Persistent teammate memory for this project:")
        sections.extend(f"- {row.key}: {row.value[:2_000]}" for row in memory)
    sections.append(
        "Granted tool permissions (informational; tools remain unavailable unless the runtime enforces them): "
        + (", ".join(sorted(permissions)) if permissions else "none")
    )
    return "\n".join(sections)[:_MAX_CONTEXT_CHARS]


async def run_teammate(
    db: AsyncSession,
    teammate: TeammateProfile,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    input_data: dict[str, Any],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Run one registered agent as a persisted teammate on an assigned project."""
    if teammate.tenant_id != tenant_id:
        raise LookupError("Teammate not found")
    if not teammate.is_enabled:
        raise TeammateRunDeniedError("Teammate is disabled")
    if teammate.status not in {TeammateStatus.IDLE, TeammateStatus.ACTIVE}:
        raise TeammateRunConflictError("Teammate is already running")
    validate_role_agent_type(teammate.role, teammate.agent_type)

    project_result = await db.execute(
        select(Project)
        .join(TeammateProjectAssignment, TeammateProjectAssignment.project_id == Project.id)
        .where(
            TeammateProjectAssignment.teammate_id == teammate.id,
            Project.id == project_id,
            Project.tenant_id == tenant_id,
            Project.organization_id == teammate.organization_id,
            Project.is_active.is_(True),
        )
    )
    project = project_result.scalar_one_or_none()
    if project is None:
        raise TeammateRunDeniedError("Project is not assigned to this teammate")

    knowledge_result = await db.execute(
        select(ProjectKnowledge)
        .where(ProjectKnowledge.project_id == project_id)
        .order_by(ProjectKnowledge.category, ProjectKnowledge.key)
        .limit(_MAX_KNOWLEDGE_ROWS)
    )
    knowledge = list(knowledge_result.scalars().all())
    permission_result = await db.execute(
        select(TeammatePermission).where(
            TeammatePermission.teammate_id == teammate.id,
            TeammatePermission.granted.is_(True),
        )
    )
    permissions = [row.permission for row in permission_result.scalars().all()]
    memory_result = await db.execute(
        select(TeammateMemory)
        .where(
            TeammateMemory.tenant_id == tenant_id,
            TeammateMemory.teammate_id == teammate.id,
            TeammateMemory.project_id == project_id,
        )
        .order_by(TeammateMemory.updated_at.desc(), TeammateMemory.key)
        .limit(_MAX_MEMORY_ROWS)
    )
    memory = list(memory_result.scalars().all())

    context = build_teammate_system_context(teammate, project, knowledge, permissions, memory)
    agent = agent_registry.create(
        teammate.agent_type,
        agent_id=str(teammate.id),
        session_id=session_id or str(uuid.uuid4()),
        tenant_id=str(tenant_id),
        config={"teammate_id": str(teammate.id), "project_id": str(project.id)},
    )
    agent._llm_provider = teammate.model_provider
    agent._llm_model = teammate.model_name
    agent._teammate_system_context = context
    agent._max_tokens_per_run = min(teammate.max_tokens_per_run or _DEFAULT_MAX_TOKENS, _HARD_MAX_TOKENS)

    claimed = await db.execute(
        update(TeammateProfile)
        .where(
            TeammateProfile.id == teammate.id,
            TeammateProfile.tenant_id == tenant_id,
            TeammateProfile.is_enabled.is_(True),
            TeammateProfile.status.in_([TeammateStatus.IDLE, TeammateStatus.ACTIVE]),
        )
        .values(status=TeammateStatus.BUSY)
        .returning(TeammateProfile.id)
    )
    if claimed.scalar_one_or_none() != teammate.id:
        await db.rollback()
        raise TeammateRunConflictError("Teammate is disabled or already running")
    teammate.status = TeammateStatus.BUSY
    await db.commit()
    try:
        timeout_seconds = min(teammate.max_run_seconds or _DEFAULT_MAX_RUN_SECONDS, _HARD_MAX_RUN_SECONDS)
        async with asyncio.timeout(timeout_seconds):
            return await agent.run(input_data)
    finally:
        # Preserve a concurrent pause/disable transition. A disabling request
        # changes status away from BUSY, so this update cannot overwrite it.
        await db.execute(
            update(TeammateProfile)
            .where(TeammateProfile.id == teammate.id, TeammateProfile.status == TeammateStatus.BUSY)
            .values(
                status=case(
                    (TeammateProfile.is_enabled.is_(True), TeammateStatus.IDLE),
                    else_=TeammateStatus.DISABLED,
                )
            )
        )
        await db.commit()
