"""Tenant-scoped task workspaces and durable sandbox verification results."""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import OWNER_ADMIN
from solvyn.api.v1.repositories import _load_authorized_repository
from solvyn.config import settings
from solvyn.db.models import (
    Organization,
    OrganizationMembership,
    Repository,
    Task,
    TaskAcceptanceCriterion,
    TaskDelivery,
    TaskHandoff,
    TaskWorkspace,
    TeammateToolInvocation,
    User,
    VerificationRun,
)
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import SHARED_SECRET_PRINCIPAL_ID, tenant_context
from solvyn.repositories.credentials import (
    CredentialEncryptionUnavailableError,
    GitHubCredentialNotFoundError,
    load_organization_github_token,
)
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError, _validate_solvyn_branch
from solvyn.repositories.providers import get_repository_provider
from solvyn.sandbox.factory import create_sandbox_runner
from solvyn.sandbox.remote import RemoteSandboxResult, RemoteSandboxRunner
from solvyn.sandbox.runtime import DockerSandboxRunner, SandboxResult, SandboxRuntimeError
from solvyn.sandbox.workspaces import WorkspaceError, WorkspaceManager, safe_relative_path
from solvyn.workflows.executor import workflow_executor

router = APIRouter(prefix="/workflows/executions/{execution_id}/tasks/{task_id}", tags=["verification"])
_MAX_INITIAL_FILES_BYTES = 16 * 1024 * 1024
_MAX_REPOSITORY_PATHS = 50
_MAX_REPOSITORY_FILE_BYTES = 1_000_000
_MAX_STORED_OUTPUT_CHARS = 256 * 1024
_MAX_DELIVERY_FILES = 50
_MAX_DELIVERY_BYTES = 500_000
_MAX_DELIVERY_DIFF_BYTES = 1_000_000
VerificationKind = Literal["test", "build", "lint", "typecheck", "security"]


class TaskWorkspaceCreate(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)
    repository_id: uuid.UUID | None = None
    repository_paths: list[str] | None = Field(default=None, max_length=_MAX_REPOSITORY_PATHS)


class VerificationRequest(BaseModel):
    kind: VerificationKind
    command: list[str] | None = Field(default=None, min_length=1, max_length=128)


class AcceptanceCriteriaCreate(BaseModel):
    criteria: list[str] = Field(min_length=1, max_length=30)


class AcceptanceDecision(BaseModel):
    decision: Literal["accepted", "rejected"]
    note: str | None = Field(default=None, max_length=2000)


class TaskDeliveryRequest(BaseModel):
    expected_diff_digest: str = Field(..., min_length=64, max_length=64)
    branch_name: str = Field(..., min_length=8, max_length=240)
    commit_message: str = Field(..., min_length=1, max_length=2000)
    pull_request_title: str = Field(..., min_length=1, max_length=256)
    pull_request_body: str = Field(default="", max_length=20_000)


_DEFAULT_VERIFICATION_COMMANDS: dict[VerificationKind, list[str]] = {
    "test": ["pytest", "-q"],
    "build": ["python", "-m", "compileall", "-q", "."],
    "lint": ["ruff", "check", "."],
    "typecheck": ["mypy", "."],
    "security": ["ruff", "check", "--select", "S", "."],
}


def _workspace_manager() -> WorkspaceManager:
    return WorkspaceManager(Path(settings.sandbox_workspace_root))


def _sandbox_runner() -> DockerSandboxRunner | RemoteSandboxRunner:
    return create_sandbox_runner(_workspace_manager())


def _tenant_ref() -> str:
    if tenant_context.tenant_id is not None:
        return str(tenant_context.tenant_id)
    if tenant_context.authenticated_tenant_id == SHARED_SECRET_PRINCIPAL_ID and tenant_context.user_id is None:
        return settings.default_tenant_id
    raise HTTPException(status_code=403, detail="A tenant membership is required for verification access")


async def _task_delivery_context(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[Task, TaskWorkspace, Repository, Any, list[str], Any, Any, str]:
    task = await _owned_task(execution_id, task_id, db)
    workspace_record = (
        await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.tenant_id == task.tenant_id,
                TaskWorkspace.task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    if workspace_record is None or workspace_record.repository_id is None or not workspace_record.source_commit_sha:
        raise HTTPException(status_code=409, detail="GitHub delivery requires a repository-backed task workspace")
    handoff = (
        await db.execute(
            select(TaskHandoff).where(
                TaskHandoff.tenant_id == task.tenant_id,
                TaskHandoff.child_task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    if handoff is None or handoff.status not in {"pending", "running", "failed", "completed"}:
        raise HTTPException(status_code=403, detail="Task has no approved project handoff for GitHub delivery")

    try:
        repository, project = await _load_authorized_repository(
            workspace_record.repository_id, db, roles=OWNER_ADMIN
        )
        if project.id != handoff.project_id:
            raise HTTPException(status_code=403, detail="Task repository is outside its assigned project")
        token = await load_organization_github_token(db, project.organization_id)
    except GitHubCredentialNotFoundError as exc:
        raise HTTPException(status_code=409, detail="Connect a GitHub organization token before delivery") from exc
    except CredentialEncryptionUnavailableError as exc:
        raise HTTPException(
            status_code=503,
            detail="GitHub credential cannot be decrypted with current configuration",
        ) from exc

    try:
        source_paths = json.loads(workspace_record.source_paths or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid") from exc
    if (
        not isinstance(source_paths, list)
        or not source_paths
        or any(not isinstance(path, str) for path in source_paths)
    ):
        raise HTTPException(status_code=409, detail="Task workspace has no valid selected source paths")
    normalized_paths: list[str] = []
    for path in source_paths:
        try:
            normalized = safe_relative_path(path).as_posix()
        except WorkspaceError as exc:
            raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid") from exc
        if normalized != path or any(part.casefold() == ".git" for part in normalized.split("/")):
            raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid")
        normalized_paths.append(normalized)
    if len({path.casefold() for path in normalized_paths}) != len(normalized_paths):
        raise HTTPException(status_code=409, detail="Task workspace source paths are not unique")

    manager = _workspace_manager()
    try:
        workspace = manager.get(workspace_record.id)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail="Managed task workspace is unavailable") from exc
    return task, workspace_record, repository, project, normalized_paths, manager, workspace, token


async def _build_delivery_preview(
    repository: Repository,
    workspace_record: TaskWorkspace,
    source_paths: list[str],
    manager: WorkspaceManager,
    workspace: Any,
    github: GitHubClient,
) -> dict[str, Any]:
    source_commit_sha = workspace_record.source_commit_sha
    if not isinstance(source_commit_sha, str) or not source_commit_sha:
        raise HTTPException(status_code=409, detail="Task workspace has no pinned source commit")
    try:
        metadata = await github.repository_metadata(repository.full_name)
        base_branch = metadata.get("default_branch")
        if not isinstance(base_branch, str) or not base_branch:
            raise HTTPException(status_code=502, detail="GitHub returned an invalid pull request base branch")
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    current_paths = manager.list_files(workspace, limit=1001, exclude_git=True)
    if len(current_paths) > 1000:
        raise HTTPException(status_code=413, detail="Delivery preview is limited to workspaces with at most 1,000 files")
    for path in current_paths:
        try:
            normalized = safe_relative_path(path).as_posix()
        except WorkspaceError as exc:
            raise HTTPException(status_code=409, detail="Workspace contains an invalid delivery path") from exc
        if normalized != path or any(part.casefold() == ".git" for part in normalized.split("/")):
            raise HTTPException(status_code=409, detail="Workspace contains an invalid delivery path")

    missing_source_paths = sorted(set(source_paths) - set(current_paths))
    try:
        _, source_tree = await github.repository_tree(repository.full_name, source_commit_sha)
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    source_tree_entries: dict[str, dict[str, Any]] = {}
    for entry in source_tree:
        if not isinstance(entry, dict):
            continue
        entry_path = entry.get("path")
        if isinstance(entry_path, str) and isinstance(entry.get("type"), str):
            source_tree_entries[entry_path] = entry
    source_tree_blob_entries = {
        path: entry for path, entry in source_tree_entries.items() if entry.get("type") == "blob"
    }
    source_tree_paths = set(source_tree_entries)
    files: list[dict[str, str]] = []
    diffs: list[dict[str, str]] = []
    total_content_bytes = 0
    total_diff_bytes = 0
    for path in current_paths:
        try:
            content = manager.read_file(workspace, path, max_bytes=1_000_000).decode("utf-8")
        except (WorkspaceError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Delivery supports UTF-8 text files up to 1 MB: {path}") from exc
        if path in source_paths:
            baseline = await github.text_file(
                repository.full_name,
                path,
                source_commit_sha,
                max_bytes=1_000_000,
            )
            if baseline is None:
                raise HTTPException(status_code=409, detail=f"Source file is no longer readable as text: {path}")
        else:
            path_casefold = path.casefold()
            has_tree_collision = any(
                existing_path == path_casefold
                or existing_path.startswith(f"{path_casefold}/")
                or (
                    source_tree_entries[existing_original_path].get("type") == "blob"
                    and path_casefold.startswith(f"{existing_path}/")
                )
                for existing_original_path in source_tree_paths
                if (existing_path := existing_original_path.casefold())
            )
            if has_tree_collision:
                raise HTTPException(
                    status_code=409,
                    detail=f"Workspace path conflicts with an unselected source file or directory: {path}",
                )
            baseline = ""
        if content == baseline:
            continue
        if path in source_paths and source_tree_blob_entries.get(path, {}).get("mode") == "100755":
            raise HTTPException(
                status_code=409,
                detail=f"Delivery cannot preserve the executable mode of a changed source file: {path}",
            )
        file_bytes = len(content.encode("utf-8"))
        total_content_bytes += file_bytes
        if len(files) >= _MAX_DELIVERY_FILES or total_content_bytes > _MAX_DELIVERY_BYTES:
            raise HTTPException(status_code=413, detail="Delivery exceeds the 50-file or 500 KB limit")
        files.append({"path": path, "content": content})
        diff = "".join(
            difflib.unified_diff(
                baseline.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{path}" if baseline else "/dev/null",
                tofile=f"b/{path}",
            )
        )
        total_diff_bytes += len(diff.encode("utf-8"))
        if total_diff_bytes > _MAX_DELIVERY_DIFF_BYTES:
            raise HTTPException(status_code=413, detail="Rendered delivery diff exceeds the 1 MB limit")
        diffs.append({"path": path, "diff": diff})

    digest_payload = {
        "source_commit_sha": source_commit_sha,
        "base_branch": base_branch,
        "files": files,
        "unsupported_missing_source_paths": missing_source_paths,
    }
    try:
        canonical = json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=422, detail="Delivery files must contain valid UTF-8 text") from exc
    return {
        "repository_id": str(repository.id),
        "source_commit_sha": source_commit_sha,
        "base_branch": base_branch,
        "files": files,
        "diffs": diffs,
        "unsupported_missing_source_paths": missing_source_paths,
        "diff_digest": digest,
        "can_deliver": bool(files) and not missing_source_paths,
    }


async def _owned_task(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession,
) -> Task:
    try:
        tenant_id = await workflow_executor._resolve_tenant_uuid(_tenant_ref())
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Workflow tenant is not configured") from exc
    task = (
        await db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.execution_id == execution_id,
                Task.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Workflow task not found")
    return task


def _verification_dict(run: VerificationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "task_id": str(run.task_id),
        "execution_id": str(run.execution_id) if run.execution_id else None,
        "workspace_id": str(run.workspace_id),
        "kind": run.kind,
        "command": json.loads(run.command),
        "status": run.status,
        "exit_code": run.exit_code,
        "timed_out": run.timed_out,
        "output_truncated": run.output_truncated,
        "stdout": run.stdout,
        "stderr": run.stderr,
        "error": run.error,
        "duration_seconds": float(run.duration_seconds) if run.duration_seconds is not None else None,
        "runner_version": run.runner_version,
        "source_digest": run.source_digest,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def _task_delivery_dict(delivery: TaskDelivery) -> dict[str, Any]:
    return {
        "id": str(delivery.id),
        "status": delivery.status,
        "diff_digest": delivery.diff_digest,
        "base_branch": delivery.base_branch,
        "branch": delivery.branch_name,
        "branch_sha": delivery.branch_sha,
        "commit_sha": delivery.commit_sha,
        "pull_request_title": delivery.pull_request_title,
        "pull_request_number": delivery.pull_request_number,
        "pull_request_url": delivery.pull_request_url,
        "error": delivery.error,
        "created_at": delivery.created_at.isoformat() if delivery.created_at else None,
        "updated_at": delivery.updated_at.isoformat() if delivery.updated_at else None,
    }


def _tool_invocation_dict(invocation: TeammateToolInvocation) -> dict[str, Any]:
    return {
        "id": str(invocation.id),
        "task_id": str(invocation.task_id),
        "workspace_id": str(invocation.workspace_id),
        "teammate_id": str(invocation.teammate_id),
        "tool_name": invocation.tool_name,
        "path": invocation.path,
        "status": invocation.status,
        "input_bytes": invocation.input_bytes,
        "output_bytes": invocation.output_bytes,
        "error_code": invocation.error_code,
        "started_at": invocation.started_at.isoformat() if invocation.started_at else None,
        "completed_at": invocation.completed_at.isoformat() if invocation.completed_at else None,
    }


def _verification_history(runs: list[VerificationRun]) -> list[dict[str, Any]]:
    terminal_statuses = {"passed", "failed", "timed_out"}
    evidence: list[dict[str, Any]] = []
    for run in runs:
        item = _verification_dict(run)
        item.update({"regression_status": None, "compared_to_run_id": None, "source_changed": None})
        if run.status not in terminal_statuses:
            evidence.append(item)
            continue

        same_check = [
            candidate
            for candidate in runs
            if candidate.workspace_id == run.workspace_id and candidate.kind == run.kind and candidate.command == run.command
        ]
        overlapping = [
            candidate
            for candidate in same_check
            if candidate.id != run.id
            and (candidate.completed_at is None or run.started_at <= candidate.completed_at)
            and (run.completed_at is None or candidate.started_at <= run.completed_at)
        ]
        earlier = [candidate for candidate in same_check if candidate.started_at < run.started_at]
        previous = max(earlier, key=lambda candidate: (candidate.started_at, candidate.id), default=None)
        comparison = max(overlapping, key=lambda candidate: (candidate.started_at, candidate.id), default=previous)
        if comparison is not None:
            item["compared_to_run_id"] = str(comparison.id)
            if comparison.source_digest is not None and run.source_digest is not None:
                item["source_changed"] = comparison.source_digest != run.source_digest

        if overlapping or (previous is not None and previous.status not in terminal_statuses):
            item["regression_status"] = "inconclusive"
        elif previous is None:
            item["regression_status"] = "baseline"
        elif previous.status == "passed" and run.status in {"failed", "timed_out"}:
            item["regression_status"] = "regressed"
        elif previous.status in {"failed", "timed_out"} and run.status == "passed":
            item["regression_status"] = "recovered"
        else:
            item["regression_status"] = "unchanged"
        evidence.append(item)
    return evidence


async def _verification_history_for_task(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    task_id: uuid.UUID,
) -> list[dict[str, Any]]:
    runs = (
        await db.execute(
            select(VerificationRun)
            .where(VerificationRun.tenant_id == tenant_id, VerificationRun.task_id == task_id)
            .order_by(VerificationRun.started_at, VerificationRun.id)
        )
    ).scalars().all()
    return _verification_history(list(runs))


def _criterion_dict(criterion: TaskAcceptanceCriterion) -> dict[str, Any]:
    return {
        "id": str(criterion.id),
        "text": criterion.text,
        "status": criterion.status,
        "reviewer_user_id": str(criterion.reviewer_user_id) if criterion.reviewer_user_id else None,
        "reviewer_organization_id": str(criterion.reviewer_organization_id) if criterion.reviewer_organization_id else None,
        "decision_note": criterion.decision_note,
        "created_at": criterion.created_at.isoformat() if criterion.created_at else None,
        "decided_at": criterion.decided_at.isoformat() if criterion.decided_at else None,
    }


async def _require_acceptance_reviewer(db: AsyncSession, task: Task) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = tenant_context.user_id
    if not tenant_context.is_authenticated or user_id is None:
        raise HTTPException(status_code=401, detail="A signed-in organization owner or admin must review acceptance criteria")
    membership = (
        await db.execute(
            select(OrganizationMembership.organization_id)
            .join(Organization, Organization.id == OrganizationMembership.organization_id)
            .join(User, User.id == OrganizationMembership.user_id)
            .where(
                OrganizationMembership.user_id == user_id,
                OrganizationMembership.role.in_(OWNER_ADMIN),
                Organization.tenant_id == task.tenant_id,
                Organization.is_active.is_(True),
                User.is_active.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=403, detail="An active organization owner or admin membership is required")
    return user_id, membership


async def _finish_run(db: AsyncSession, run: VerificationRun, values: dict[str, Any]) -> dict[str, Any]:
    completed = await db.execute(
        update(VerificationRun)
        .where(VerificationRun.id == run.id, VerificationRun.tenant_id == run.tenant_id, VerificationRun.status == "running")
        .values(**values)
        .returning(VerificationRun.id)
    )
    if completed.scalar_one_or_none() is None:
        await db.refresh(run)
        evidence = await _verification_history_for_task(db, run.tenant_id, run.task_id)
        return next(item for item in evidence if item["id"] == str(run.id))
    await db.commit()
    await db.refresh(run)
    evidence = await _verification_history_for_task(db, run.tenant_id, run.task_id)
    return next(item for item in evidence if item["id"] == str(run.id))


@router.post("/workspace", status_code=201, summary="Create a managed task workspace")
async def create_task_workspace(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskWorkspaceCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    existing = (
        await db.execute(select(TaskWorkspace).where(TaskWorkspace.tenant_id == task.tenant_id, TaskWorkspace.task_id == task.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="Task already has a managed workspace")

    if (payload.repository_id is None) != (payload.repository_paths is None):
        raise HTTPException(status_code=422, detail="repository_id and repository_paths must be supplied together")
    if payload.repository_id is not None and payload.files:
        raise HTTPException(status_code=422, detail="Provide either initial files or a repository source, not both")

    initial_files = dict(payload.files)
    source_commit_sha: str | None = None
    source_paths: list[str] | None = None
    repository: Repository | None = None
    if payload.repository_id is not None:
        if not payload.repository_paths:
            raise HTTPException(status_code=422, detail="Select at least one repository path")
        handoff = (
            await db.execute(
                select(TaskHandoff).where(
                    TaskHandoff.tenant_id == task.tenant_id,
                    TaskHandoff.child_task_id == task.id,
                )
            )
        ).scalar_one_or_none()
        if handoff is None:
            raise HTTPException(status_code=403, detail="Repository workspaces require an assigned project handoff")
        if handoff.status not in {"pending", "running", "failed", "completed"}:
            raise HTTPException(status_code=409, detail="Task handoff is not approved for workspace access")
        repository, project = await _load_authorized_repository(payload.repository_id, db)
        if project.id != handoff.project_id:
            raise HTTPException(status_code=403, detail="Repository is outside the task's assigned project")
        if repository.provider != "github":
            raise HTTPException(status_code=422, detail="Only GitHub repository workspaces are supported")
        source_paths = []
        for path in payload.repository_paths:
            try:
                normalized = safe_relative_path(path).as_posix()
            except WorkspaceError as exc:
                raise HTTPException(status_code=422, detail="Repository path is invalid") from exc
            if (
                normalized != path
                or len(normalized) > 512
                or any(part.casefold() == ".git" for part in normalized.split("/"))
                or normalized.casefold() in {selected.casefold() for selected in source_paths}
            ):
                raise HTTPException(status_code=422, detail="Repository paths must be normalized and unique")
            source_paths.append(normalized)

        try:
            async with get_repository_provider("github") as github:
                metadata = await github.repository_metadata(repository.full_name)
                if metadata["private"]:
                    raise HTTPException(status_code=422, detail="Only public GitHub repository workspaces are supported")
                source_commit_sha = await github.branch_commit(repository.full_name, str(metadata["default_branch"]))
                _, tree = await github.repository_tree(repository.full_name, source_commit_sha)
                tree_entries = {entry["path"]: entry for entry in tree}
                selected_entries = []
                for path in source_paths:
                    entry = tree_entries.get(path)
                    if entry is None or entry.get("type") != "blob" or entry.get("mode") not in {"100644", "100755"}:
                        raise HTTPException(status_code=422, detail=f"Repository path is not a regular file: {path}")
                    size = entry.get("size")
                    if isinstance(size, int) and size > _MAX_REPOSITORY_FILE_BYTES:
                        raise HTTPException(status_code=413, detail=f"Repository file exceeds 1 MB: {path}")
                    selected_entries.append((path, entry))
                known_size = sum(
                    entry["size"] for _, entry in selected_entries if isinstance(entry.get("size"), int)
                )
                if known_size > _MAX_INITIAL_FILES_BYTES:
                    raise HTTPException(status_code=413, detail="Selected repository files exceed 16 MiB")
                for path, _entry in selected_entries:
                    content = await github.text_file(
                        repository.full_name,
                        path,
                        source_commit_sha,
                        max_bytes=_MAX_REPOSITORY_FILE_BYTES,
                    )
                    if content is None:
                        raise HTTPException(status_code=422, detail=f"Repository file is missing, binary, or over 1 MB: {path}")
                    initial_files[path] = content
        except GitHubRepositoryError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    try:
        total_bytes = sum(len(contents.encode("utf-8")) for contents in initial_files.values())
    except UnicodeEncodeError as exc:
        raise HTTPException(status_code=422, detail="Workspace files must contain valid UTF-8 text") from exc
    if total_bytes > _MAX_INITIAL_FILES_BYTES:
        raise HTTPException(status_code=413, detail="Initial workspace files exceed 16 MiB")

    manager = _workspace_manager()
    workspace = manager.create()
    try:
        for relative_path, contents in initial_files.items():
            manager.write_file(workspace, relative_path, contents.encode("utf-8"))
        record = TaskWorkspace(
            id=workspace.id,
            tenant_id=task.tenant_id,
            task_id=task.id,
            repository_id=repository.id if repository else None,
            source_commit_sha=source_commit_sha,
            source_paths=json.dumps(source_paths) if source_paths else None,
        )
        db.add(record)
        await db.commit()
    except WorkspaceError as exc:
        manager.delete(workspace.id)
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        manager.delete(workspace.id)
        await db.rollback()
        raise
    return {
        "workspace_id": str(workspace.id),
        "task_id": str(task.id),
        "status": "ready",
        "repository_id": str(repository.id) if repository else None,
        "source_commit_sha": source_commit_sha,
        "source_paths": source_paths,
    }


@router.post("/verification-runs", summary="Run and persist a sandbox verification check")
async def run_task_verification(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: VerificationRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    workspace_record = (
        await db.execute(select(TaskWorkspace).where(TaskWorkspace.tenant_id == task.tenant_id, TaskWorkspace.task_id == task.id))
    ).scalar_one_or_none()
    if workspace_record is None:
        raise HTTPException(status_code=409, detail="Create a task workspace before running verification")
    command = payload.command or _DEFAULT_VERIFICATION_COMMANDS[payload.kind]
    if any(not part or "\x00" in part for part in command):
        raise HTTPException(status_code=422, detail="Command arguments must be non-empty strings without NUL bytes")
    if any(len(part.encode("utf-8")) > 4096 for part in command) or sum(len(part.encode("utf-8")) for part in command) > 16_384:
        raise HTTPException(status_code=422, detail="Command arguments exceed the 16 KiB limit")

    try:
        workspace = _workspace_manager().get(workspace_record.id)
        source_digest = _workspace_manager().fingerprint(workspace)
    except WorkspaceError as exc:
        raise HTTPException(status_code=409, detail="Task workspace is missing or invalid") from exc

    run = VerificationRun(
        tenant_id=task.tenant_id,
        task_id=task.id,
        execution_id=execution_id,
        workspace_id=workspace_record.id,
        kind=payload.kind,
        command=json.dumps(command),
        status="running",
        runner_version="sandbox-service/1" if settings.sandbox_runtime_url else "docker-sandbox/1",
        source_digest=source_digest,
    )
    db.add(run)
    await db.commit()
    try:
        result: SandboxResult | RemoteSandboxResult = await _sandbox_runner().run(workspace, command)
    except (SandboxRuntimeError, WorkspaceError, ValueError) as exc:
        return await _finish_run(
            db,
            run,
            {
                "status": "failed",
                "error": str(exc)[:_MAX_STORED_OUTPUT_CHARS],
                "completed_at": datetime.now(UTC),
            },
        )

    return await _finish_run(
        db,
        run,
        {
            "status": "timed_out" if result.timed_out else ("passed" if result.exit_code == 0 else "failed"),
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "output_truncated": result.output_truncated,
            "stdout": result.stdout[:_MAX_STORED_OUTPUT_CHARS],
            "stderr": result.stderr[:_MAX_STORED_OUTPUT_CHARS],
            "duration_seconds": result.duration_seconds,
            "completed_at": datetime.now(UTC),
        },
    )


@router.get("/verification-runs", summary="List task verification evidence")
async def list_task_verification_runs(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    return {
        "task_id": str(task.id),
        "verification_runs": await _verification_history_for_task(db, task.tenant_id, task.id),
    }


@router.get("/tool-invocations", summary="List task-scoped teammate tool audit records")
async def list_task_tool_invocations(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    invocations = (
        await db.execute(
            select(TeammateToolInvocation)
            .where(
                TeammateToolInvocation.tenant_id == task.tenant_id,
                TeammateToolInvocation.task_id == task.id,
            )
            .order_by(TeammateToolInvocation.started_at.desc(), TeammateToolInvocation.id.desc())
            .limit(500)
        )
    ).scalars().all()
    return {
        "task_id": str(task.id),
        "tool_invocations": [_tool_invocation_dict(item) for item in invocations],
    }


@router.post("/acceptance-criteria", status_code=201, summary="Add human-reviewable acceptance criteria")
async def create_task_acceptance_criteria(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: AcceptanceCriteriaCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    criteria_text = [criterion.strip() for criterion in payload.criteria]
    if any(not criterion or len(criterion) > 2000 for criterion in criteria_text):
        raise HTTPException(status_code=422, detail="Acceptance criteria must contain 1 to 2000 non-whitespace characters")
    if len({criterion.casefold() for criterion in criteria_text}) != len(criteria_text):
        raise HTTPException(status_code=422, detail="Acceptance criteria must be unique")
    claimed = await db.execute(
        update(Task)
        .where(Task.id == task.id, Task.tenant_id == task.tenant_id, Task.acceptance_criteria_defined.is_(False))
        .values(acceptance_criteria_defined=True)
        .returning(Task.id)
    )
    if claimed.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Acceptance criteria are already defined for this task")

    records = [TaskAcceptanceCriterion(tenant_id=task.tenant_id, task_id=task.id, text=criterion) for criterion in criteria_text]
    db.add_all(records)
    await db.commit()
    return {"task_id": str(task.id), "acceptance_criteria": [_criterion_dict(record) for record in records]}


@router.get("/acceptance-criteria", summary="Get task acceptance criteria and review decisions")
async def list_task_acceptance_criteria(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    criteria = (
        await db.execute(
            select(TaskAcceptanceCriterion)
            .where(TaskAcceptanceCriterion.tenant_id == task.tenant_id, TaskAcceptanceCriterion.task_id == task.id)
            .order_by(TaskAcceptanceCriterion.created_at, TaskAcceptanceCriterion.id)
        )
    ).scalars().all()
    return {"task_id": str(task.id), "acceptance_criteria": [_criterion_dict(record) for record in criteria]}


@router.post("/acceptance-criteria/{criterion_id}/decision", summary="Record an owner/admin acceptance decision")
async def decide_task_acceptance_criterion(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    criterion_id: uuid.UUID,
    payload: AcceptanceDecision,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    reviewer_user_id, reviewer_organization_id = await _require_acceptance_reviewer(db, task)
    decided = await db.execute(
        update(TaskAcceptanceCriterion)
        .where(
            TaskAcceptanceCriterion.id == criterion_id,
            TaskAcceptanceCriterion.tenant_id == task.tenant_id,
            TaskAcceptanceCriterion.task_id == task.id,
            TaskAcceptanceCriterion.status == "pending",
        )
        .values(
            status=payload.decision,
            reviewer_user_id=reviewer_user_id,
            reviewer_organization_id=reviewer_organization_id,
            decision_note=payload.note,
            decided_at=datetime.now(UTC),
        )
        .returning(TaskAcceptanceCriterion.id)
    )
    if decided.scalar_one_or_none() is None:
        existing_status = (
            await db.execute(
                select(TaskAcceptanceCriterion.status).where(
                    TaskAcceptanceCriterion.id == criterion_id,
                    TaskAcceptanceCriterion.tenant_id == task.tenant_id,
                    TaskAcceptanceCriterion.task_id == task.id,
                )
            )
        ).scalar_one_or_none()
        await db.rollback()
        if existing_status is None:
            raise HTTPException(status_code=404, detail="Acceptance criterion not found")
        raise HTTPException(status_code=409, detail=f"Acceptance criterion is already {existing_status}")

    await db.commit()
    record = (
        await db.execute(
            select(TaskAcceptanceCriterion).where(
                TaskAcceptanceCriterion.id == criterion_id,
                TaskAcceptanceCriterion.tenant_id == task.tenant_id,
                TaskAcceptanceCriterion.task_id == task.id,
            )
        )
    ).scalar_one()
    return _criterion_dict(record)


@router.get("/delivery-preview", summary="Preview bounded task workspace changes for GitHub")
async def preview_task_delivery(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    (
        _task,
        workspace_record,
        repository,
        _project,
        source_paths,
        manager,
        workspace,
        token,
    ) = await _task_delivery_context(execution_id, task_id, db)
    try:
        async with GitHubClient(token=token) as github:
            preview = await _build_delivery_preview(
                repository, workspace_record, source_paths, manager, workspace, github
            )
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    deliveries = (
        await db.execute(
            select(TaskDelivery)
            .where(TaskDelivery.tenant_id == _task.tenant_id, TaskDelivery.task_id == _task.id)
            .order_by(TaskDelivery.created_at.desc(), TaskDelivery.id.desc())
            .limit(20)
        )
    ).scalars().all()
    preview["delivery_history"] = [_task_delivery_dict(item) for item in deliveries]
    return preview


@router.post("/deliver", status_code=201, summary="Create a draft PR from the approved workspace diff")
async def deliver_task_workspace(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    payload: TaskDeliveryRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if not payload.commit_message.strip() or not payload.pull_request_title.strip():
        raise HTTPException(status_code=422, detail="Commit message and pull request title must not be blank")
    try:
        _validate_solvyn_branch(payload.branch_name)
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    (
        _task,
        workspace_record,
        repository,
        _project,
        source_paths,
        manager,
        workspace,
        token,
    ) = await _task_delivery_context(execution_id, task_id, db)
    pinned_source_sha = workspace_record.source_commit_sha
    if not isinstance(pinned_source_sha, str) or not pinned_source_sha:
        raise HTTPException(status_code=409, detail="Task workspace has no pinned source commit")
    try:
        async with GitHubClient(token=token) as github:
            preview = await _build_delivery_preview(
                repository, workspace_record, source_paths, manager, workspace, github
            )
            if preview["diff_digest"] != payload.expected_diff_digest:
                raise HTTPException(status_code=409, detail="Task workspace changed after the delivery preview")
            if not preview["can_deliver"]:
                raise HTTPException(
                    status_code=409,
                    detail="Delivery requires at least one change and all selected source files; deletions are unsupported",
                )
            existing = (
                await db.execute(
                    select(TaskDelivery)
                    .where(
                        TaskDelivery.tenant_id == _task.tenant_id,
                        TaskDelivery.task_id == _task.id,
                        TaskDelivery.diff_digest == preview["diff_digest"],
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.status == "pull_request_open" and existing.pull_request_url and existing.commit_sha:
                    return {
                        "delivery_id": str(existing.id),
                        "branch": existing.branch_name,
                        "commit_sha": existing.commit_sha,
                        "pull_request": {
                            "number": existing.pull_request_number,
                            "title": existing.pull_request_title or "Pull request",
                            "url": existing.pull_request_url,
                            "state": "recorded",
                            "draft": None,
                        },
                        "merged": False,
                        "approved_diff_digest": existing.diff_digest,
                        "replayed": True,
                    }
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": f"This diff already has a delivery attempt in {existing.status} state.",
                        "delivery": _task_delivery_dict(existing),
                        "recovery": "Inspect the recorded branch and GitHub pull requests before starting another delivery.",
                    },
                )

            delivery = TaskDelivery(
                tenant_id=_task.tenant_id,
                task_id=_task.id,
                workspace_id=workspace_record.id,
                repository_id=repository.id,
                source_commit_sha=pinned_source_sha,
                base_branch=preview["base_branch"],
                diff_digest=preview["diff_digest"],
                branch_name=payload.branch_name,
                status="starting",
            )
            db.add(delivery)
            try:
                await db.commit()
                await db.refresh(delivery)
            except IntegrityError as exc:
                await db.rollback()
                concurrent = (
                    await db.execute(
                        select(TaskDelivery).where(
                            TaskDelivery.tenant_id == _task.tenant_id,
                            TaskDelivery.task_id == _task.id,
                            TaskDelivery.diff_digest == preview["diff_digest"],
                        )
                    )
                ).scalar_one_or_none()
                if concurrent is not None and concurrent.status == "pull_request_open" and concurrent.pull_request_url:
                    return {
                        "delivery_id": str(concurrent.id),
                        "branch": concurrent.branch_name,
                        "commit_sha": concurrent.commit_sha,
                        "pull_request": {
                            "number": concurrent.pull_request_number,
                            "title": concurrent.pull_request_title or "Pull request",
                            "url": concurrent.pull_request_url,
                            "state": "recorded",
                            "draft": None,
                        },
                        "merged": False,
                        "approved_diff_digest": concurrent.diff_digest,
                        "replayed": True,
                    }
                raise HTTPException(
                    status_code=409,
                    detail="A delivery attempt for this exact diff is already in progress or needs recovery.",
                ) from exc

            try:
                branch_created = await github.create_branch_at_commit(
                    repository.full_name,
                    payload.branch_name,
                    pinned_source_sha,
                )
            except GitHubRepositoryError as exc:
                delivery.status = "unknown"
                delivery.error = exc.message[:2000]
                await db.commit()
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "message": exc.message,
                        "delivery_id": str(delivery.id),
                        "branch": payload.branch_name,
                        "branch_created": "unknown",
                        "recovery": "Check GitHub for this branch before retrying the same task diff.",
                    },
                ) from exc

            delivery.status = "branch_created"
            delivery.branch_sha = branch_created["sha"]
            await db.commit()
            try:
                commit_created = await github.commit_files(
                    repository.full_name,
                    branch_created["name"],
                    expected_head_sha=branch_created["sha"],
                    message=payload.commit_message,
                    files=preview["files"],
                )
            except GitHubRepositoryError as exc:
                delivery.status = "unknown"
                delivery.error = exc.message[:2000]
                await db.commit()
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "message": exc.message,
                        "delivery_id": str(delivery.id),
                        "branch": branch_created["name"],
                        "branch_sha": branch_created["sha"],
                        "commit_sha": "unknown",
                        "recovery": "Inspect the branch head and GitHub pull requests before retrying this task diff.",
                    },
                ) from exc

            delivery.status = "committed"
            delivery.commit_sha = commit_created["sha"]
            await db.commit()
            try:
                pull_request = await github.create_pull_request(
                    repository.full_name,
                    title=payload.pull_request_title,
                    body=payload.pull_request_body,
                    head=branch_created["name"],
                    base=preview["base_branch"],
                )
            except GitHubRepositoryError as exc:
                delivery.status = "unknown"
                delivery.error = exc.message[:2000]
                await db.commit()
                raise HTTPException(
                    status_code=exc.status_code,
                    detail={
                        "message": exc.message,
                        "delivery_id": str(delivery.id),
                        "branch": branch_created["name"],
                        "branch_sha": branch_created["sha"],
                        "commit_sha": commit_created["sha"],
                        "pull_request_created": "unknown",
                        "recovery": "Check GitHub for an open pull request from this branch before retrying.",
                    },
                ) from exc

            delivery.status = "pull_request_open"
            delivery.pull_request_title = str(pull_request.get("title") or payload.pull_request_title)[:256]
            delivery.pull_request_number = pull_request.get("number")
            delivery.pull_request_url = pull_request.get("url") or None
            delivery.error = None
            await db.commit()
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "delivery_id": str(delivery.id),
        "branch": branch_created["name"],
        "commit_sha": commit_created["sha"],
        "pull_request": pull_request,
        "merged": False,
        "approved_diff_digest": preview["diff_digest"],
        "replayed": False,
    }
