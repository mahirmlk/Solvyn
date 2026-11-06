"""Load persisted teammate identity and project scope into the shared agent runtime."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.registry import agent_registry
from solvyn.agents.workspace_tools import TeammateWorkspaceToolRuntime
from solvyn.config import settings
from solvyn.db.models import (
    Project,
    ProjectKnowledge,
    Repository,
    TaskHandoff,
    TaskWorkspace,
    TeammateMemory,
    TeammatePermission,
    TeammateProfile,
    TeammateProjectAssignment,
    TeammateStatus,
)
from solvyn.repositories.credentials import CredentialEncryptionUnavailableError, GitHubCredentialNotFoundError
from solvyn.repositories.github import GitHubRepositoryError
from solvyn.repositories.providers import get_authorized_repository_provider, get_repository_provider
from solvyn.sandbox.workspaces import WorkspaceManager

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
_MAX_REPOSITORY_CONTEXT_CHARS = 12_000
_MAX_REPOSITORY_SUMMARY_CHARS = 6_000
_MAX_SELECTED_SOURCE_FILES = 4
_MAX_SELECTED_SOURCE_FILE_CHARS = 1_500


class TeammateRunConflictError(RuntimeError):
    """Raised when a teammate is already running or cannot be claimed."""


class TeammateRunDeniedError(PermissionError):
    """Raised when a teammate or project is outside the caller's run scope."""


class RepositoryContextUnavailableError(RuntimeError):
    """Raised when explicitly requested connected repository context cannot be loaded."""


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
        "Granted teammate permissions (the task workspace tool runtime enforces READ_ONLY, MODIFY_FILES, "
        "and fixed-profile EXECUTE_COMMANDS; other permission types do not expose tools in this runtime): "
        + (", ".join(sorted(permissions)) if permissions else "none")
    )
    return "\n".join(sections)[:_MAX_CONTEXT_CHARS]


async def _load_repository_context(
    repository: Repository,
    source_paths: list[str] | None = None,
    *,
    db: AsyncSession | None = None,
    organization_id: uuid.UUID | None = None,
) -> str:
    """Return bounded summaries and caller-selected files from a connected GitHub repository."""
    try:
        if db is not None and organization_id is not None:
            provider_context = await get_authorized_repository_provider(
                repository.provider, db, repository, organization_id
            )
        else:
            provider_context = get_repository_provider(repository.provider)
        async with provider_context as provider:
            inspection = await provider.inspect(repository.full_name)
            dependencies = provider.parse_dependencies(inspection.get("manifests", {}))
            frameworks = provider.detect_frameworks(dependencies)
            selected_sources = []
            for path in (source_paths or [])[:_MAX_SELECTED_SOURCE_FILES]:
                try:
                    contents = await provider.text_file(repository.full_name, path, repository.default_branch)
                except GitHubRepositoryError as exc:
                    if exc.status_code == 404:
                        raise ValueError(f"Selected repository file was not found: {path}") from exc
                    raise
                if contents:
                    selected_sources.append((path, contents[:_MAX_SELECTED_SOURCE_FILE_CHARS]))
    except (GitHubRepositoryError, GitHubCredentialNotFoundError, CredentialEncryptionUnavailableError) as exc:
        raise RepositoryContextUnavailableError("Connected repository context is temporarily unavailable") from exc

    repository_info = inspection.get("repository", {})
    manifests = inspection.get("manifests", {})
    entries = inspection.get("top_level_entries", [])
    languages = inspection.get("languages", {})
    lines = [
        f"Repository: {repository.full_name}",
        f"Default branch: {repository.default_branch}",
        f"Description: {str(repository_info.get('description') or '')[:800]}",
        "Languages: " + ", ".join(str(name) for name in list(languages)[:12]),
        "Detected frameworks: " + (", ".join(frameworks[:20]) or "none detected"),
        "Top-level paths: " + ", ".join(str(entry)[:120] for entry in entries[:60]),
    ]
    if dependencies:
        lines.append("Manifest dependencies (untrusted):")
        for manager, packages in list(dependencies.items())[:10]:
            lines.append(f"- {manager}: {', '.join(str(package)[:100] for package in packages[:30])}")
    readme = manifests.get("README.md")
    if isinstance(readme, str) and readme:
        lines.extend(["README excerpt (untrusted repository content):", readme[:2_000]])
    summary = "\n".join(lines)[:_MAX_REPOSITORY_SUMMARY_CHARS]
    source_sections = [
        f"Selected source file (untrusted repository content): {path}\n{contents}"
        for path, contents in selected_sources
    ]
    return (summary + ("\n\n" + "\n\n".join(source_sections) if source_sections else ""))[:_MAX_REPOSITORY_CONTEXT_CHARS]


async def run_teammate(
    db: AsyncSession,
    teammate: TeammateProfile,
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    input_data: dict[str, Any],
    session_id: str | None = None,
    repository_id: uuid.UUID | None = None,
    repository_paths: list[str] | None = None,
    task_id: uuid.UUID | None = None,
    handoff_lease_id: uuid.UUID | None = None,
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

    repository_context = None
    if repository_paths and repository_id is None:
        raise ValueError("repository_id is required when repository_paths are provided")
    if repository_id is not None:
        repository_result = await db.execute(
            select(Repository).where(
                Repository.id == repository_id,
                Repository.project_id == project_id,
                Repository.is_active.is_(True),
            )
        )
        repository = repository_result.scalar_one_or_none()
        if repository is None:
            raise TeammateRunDeniedError("Repository is not connected to the assigned project")
        if repository.provider != "github":
            raise RepositoryContextUnavailableError("Repository context is not supported for this provider")
        repository_context = await _load_repository_context(
            repository, repository_paths, db=db, organization_id=project.organization_id
        )

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

    if task_id is not None and handoff_lease_id is not None:
        handoff_result = await db.execute(
            select(TaskHandoff.id).where(
                TaskHandoff.tenant_id == tenant_id,
                TaskHandoff.child_task_id == task_id,
                TaskHandoff.teammate_id == teammate.id,
                TaskHandoff.project_id == project_id,
                TaskHandoff.status == "running",
                TaskHandoff.lease_owner == handoff_lease_id,
                TaskHandoff.lease_expires_at > func.now(),
            )
        )
        handoff_id = handoff_result.scalar_one_or_none()
        workspace_result = await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.tenant_id == tenant_id,
                TaskWorkspace.task_id == task_id,
            )
        )
        workspace_record = workspace_result.scalar_one_or_none()
        if handoff_id is not None and workspace_record is not None and (
            "READ_ONLY" in permissions or "MODIFY_FILES" in permissions or "EXECUTE_COMMANDS" in permissions
        ):
            agent_tool_runtime = TeammateWorkspaceToolRuntime(
                db,
                WorkspaceManager(Path(settings.sandbox_workspace_root)),
                tenant_id=tenant_id,
                task_id=task_id,
                workspace_id=workspace_record.id,
                teammate_id=teammate.id,
                handoff_lease_id=handoff_lease_id,
            )
        else:
            agent_tool_runtime = None
    else:
        agent_tool_runtime = None
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
    agent._teammate_repository_context = repository_context or ""
    agent._teammate_tool_runtime = agent_tool_runtime
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
