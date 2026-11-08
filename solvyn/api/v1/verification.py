"""Tenant-scoped task workspaces and durable sandbox verification results."""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, MEMBER_OR_ABOVE, OWNER_ADMIN, require_org_membership
from solvyn.api.v1.repositories import _load_authorized_repository
from solvyn.config import settings
from solvyn.db.models import (
    Approval,
    OrchestrationEvent,
    Organization,
    OrganizationMembership,
    Project,
    Repository,
    Task,
    TaskAcceptanceCriterion,
    TaskDelivery,
    TaskEvent,
    TaskHandoff,
    TaskWorkspace,
    TeammateToolInvocation,
    TokenUsage,
    User,
    VerificationAttemptEvent,
    VerificationQueueControl,
    VerificationRun,
)
from solvyn.db.session import bind_tenant_scope, get_session
from solvyn.multi_tenant.middleware import SHARED_SECRET_PRINCIPAL_ID, tenant_context
from solvyn.repositories.credentials import (
    CredentialEncryptionUnavailableError,
    GitHubCredentialNotFoundError,
    load_organization_github_token,
)
from solvyn.repositories.github import GitHubClient, GitHubRepositoryError, _validate_solvyn_branch
from solvyn.repositories.providers import get_authorized_repository_provider
from solvyn.repositories.worktree import RepositoryWorktreeError, materialize_repository_worktree, validate_repository_tree
from solvyn.sandbox.factory import create_sandbox_runner
from solvyn.sandbox.remote import RemoteSandboxResult, RemoteSandboxRunner
from solvyn.sandbox.runtime import DockerSandboxRunner, SandboxResult, SandboxRuntimeError
from solvyn.sandbox.workspaces import WorkspaceError, WorkspaceManager, safe_relative_path
from solvyn.workflows.executor import workflow_executor

router = APIRouter(prefix="/workflows/executions/{execution_id}/tasks/{task_id}", tags=["verification"])
_MAX_INITIAL_FILES_BYTES = 16 * 1024 * 1024
_MAX_FULL_WORKSPACE_BYTES = 128 * 1024 * 1024
_MAX_REPOSITORY_PATHS = 50
_MAX_REPOSITORY_FILE_BYTES = 1_000_000
_MAX_STORED_OUTPUT_CHARS = 256 * 1024
_MAX_DELIVERY_FILES = 50
_MAX_DELIVERY_BYTES = 500_000
_MAX_DELIVERY_DIFF_BYTES = 1_000_000
_ACTIVITY_SOURCE_LIMIT = 100
_ACTIVITY_TOTAL_LIMIT = 200
_ACTIVITY_TASK_TREE_LIMIT = 500
_ACTIVITY_TASK_TREE_DEPTH_LIMIT = 32
_MAX_LIVE_PULL_REQUESTS = 5
_VERIFICATION_LEASE_DURATION = timedelta(minutes=10)
_VERIFICATION_MAX_ATTEMPTS = 3
VerificationKind = Literal["test", "build", "lint", "typecheck", "security"]


class TaskWorkspaceCreate(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)
    repository_id: uuid.UUID | None = None
    repository_paths: list[str] | None = Field(default=None, max_length=_MAX_REPOSITORY_PATHS)
    full_repository: bool = False


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
) -> tuple[Task, TaskWorkspace, Repository, Any, list[str] | None, Any, Any, str]:
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
        source_paths = json.loads(workspace_record.source_paths) if workspace_record.source_paths is not None else None
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid") from exc
    if source_paths is not None and (
        not isinstance(source_paths, list) or not source_paths or any(not isinstance(path, str) for path in source_paths)
    ):
        raise HTTPException(status_code=409, detail="Task workspace has no valid selected source paths")
    normalized_paths: list[str] = []
    for path in source_paths or []:
        try:
            normalized = safe_relative_path(path).as_posix()
        except WorkspaceError as exc:
            raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid") from exc
        if normalized != path or any(part.casefold() == ".git" for part in normalized.split("/")):
            raise HTTPException(status_code=409, detail="Task workspace source provenance is invalid")
        normalized_paths.append(normalized)
    if source_paths is not None and len({path.casefold() for path in normalized_paths}) != len(normalized_paths):
        raise HTTPException(status_code=409, detail="Task workspace source paths are not unique")

    manager = _workspace_manager()
    try:
        workspace = manager.get(workspace_record.id)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail="Managed task workspace is unavailable") from exc
    return task, workspace_record, repository, project, normalized_paths if source_paths is not None else None, manager, workspace, token


async def _build_delivery_preview(
    repository: Repository,
    workspace_record: TaskWorkspace,
    source_paths: list[str] | None,
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
    current_limit = 20_000 if source_paths is None else 1000
    current_paths = manager.list_files(workspace, limit=current_limit + 1, exclude_git=True)
    if len(current_paths) > current_limit:
        raise HTTPException(status_code=413, detail=f"Delivery preview is limited to workspaces with at most {current_limit:,} files")
    for path in current_paths:
        try:
            normalized = safe_relative_path(path).as_posix()
        except WorkspaceError as exc:
            raise HTTPException(status_code=409, detail="Workspace contains an invalid delivery path") from exc
        if normalized != path or any(part.casefold() == ".git" for part in normalized.split("/")):
            raise HTTPException(status_code=409, detail="Workspace contains an invalid delivery path")

    missing_source_paths = sorted(set(source_paths or []) - set(current_paths))
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
    if source_paths is None:
        try:
            validate_repository_tree(source_tree)
        except RepositoryWorktreeError as exc:
            raise HTTPException(status_code=409, detail="Pinned repository tree is no longer supported for delivery") from exc
        return await _build_full_repository_delivery_preview(
            repository, workspace_record, current_paths, manager, workspace, github, base_branch, source_tree_entries
        )
    files: list[dict[str, str]] = []
    deleted_paths: list[str] = []
    diffs: list[dict[str, str]] = []
    total_content_bytes = 0
    total_diff_bytes = 0
    unsupported_missing_source_paths: list[str] = []
    for path in missing_source_paths:
        source_entry = source_tree_blob_entries.get(path)
        if source_entry is None or source_entry.get("mode") != "100644":
            unsupported_missing_source_paths.append(path)
            continue
        baseline = await github.text_file(
            repository.full_name,
            path,
            source_commit_sha,
            max_bytes=1_000_000,
        )
        if baseline is None:
            unsupported_missing_source_paths.append(path)
            continue
        total_content_bytes += len(baseline.encode("utf-8"))
        if total_content_bytes > _MAX_DELIVERY_BYTES:
            raise HTTPException(status_code=413, detail="Delivery exceeds the 50-file or 500 KB limit")
        if len(files) + len(deleted_paths) >= _MAX_DELIVERY_FILES:
            raise HTTPException(status_code=413, detail="Delivery exceeds the 50-file or 500 KB limit")
        deleted_paths.append(path)
        diff = "".join(
            difflib.unified_diff(
                baseline.splitlines(keepends=True),
                [],
                fromfile=f"a/{path}",
                tofile="/dev/null",
            )
        )
        total_diff_bytes += len(diff.encode("utf-8"))
        if total_diff_bytes > _MAX_DELIVERY_DIFF_BYTES:
            raise HTTPException(status_code=413, detail="Rendered delivery diff exceeds the 1 MB limit")
        diffs.append({"path": path, "diff": diff})

    for path in current_paths:
        try:
            content = manager.read_file(workspace, path, max_bytes=1_000_000).decode("utf-8")
        except (WorkspaceError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=422, detail=f"Delivery supports UTF-8 text files up to 1 MB: {path}") from exc
        if path in (source_paths or []):
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
        if path in (source_paths or []) and source_tree_blob_entries.get(path, {}).get("mode") == "100755":
            raise HTTPException(
                status_code=409,
                detail=f"Delivery cannot preserve the executable mode of a changed source file: {path}",
            )
        file_bytes = len(content.encode("utf-8"))
        total_content_bytes += file_bytes
        if len(files) + len(deleted_paths) >= _MAX_DELIVERY_FILES or total_content_bytes > _MAX_DELIVERY_BYTES:
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
        "deleted_paths": deleted_paths,
        "unsupported_missing_source_paths": unsupported_missing_source_paths,
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
        "deleted_paths": deleted_paths,
        "diffs": diffs,
        "unsupported_missing_source_paths": unsupported_missing_source_paths,
        "diff_digest": digest,
        "can_deliver": bool(files or deleted_paths) and not unsupported_missing_source_paths,
    }


def _git_blob_sha(path: Any) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1(usedforsecurity=False)
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as content:
        for chunk in iter(lambda: content.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _build_full_repository_delivery_preview(
    repository: Repository,
    workspace_record: TaskWorkspace,
    current_paths: list[str],
    manager: WorkspaceManager,
    workspace: Any,
    github: GitHubClient,
    base_branch: str,
    source_tree_entries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Compare a full checkout to its pinned tree without trusting local Git metadata."""
    source_blobs = {
        path: entry for path, entry in source_tree_entries.items() if entry.get("type") == "blob"
    }
    source_casefold = {path.casefold(): path for path in source_tree_entries}
    current_set = set(current_paths)
    changed: list[tuple[str, dict[str, Any] | None]] = []
    for path, entry in sorted(source_blobs.items()):
        if path not in current_set:
            changed.append((path, None))
            if len(changed) > _MAX_DELIVERY_FILES:
                raise HTTPException(status_code=413, detail="Delivery exceeds the 50-file or 500 KB limit")
    for path in current_paths:
        source_entry = source_blobs.get(path)
        if source_entry is None:
            changed.append((path, None))
            continue
        expected_sha = source_entry.get("sha")
        try:
            actual_sha = _git_blob_sha(workspace.path.joinpath(*safe_relative_path(path).parts))
        except (OSError, WorkspaceError) as exc:
            raise HTTPException(status_code=409, detail=f"Workspace file cannot be compared safely: {path}") from exc
        if not isinstance(expected_sha, str) or actual_sha.lower() != expected_sha.lower():
            changed.append((path, source_entry))
        if len(changed) > _MAX_DELIVERY_FILES:
            raise HTTPException(status_code=413, detail="Delivery exceeds the 50-file or 500 KB limit")

    files: list[dict[str, str]] = []
    deleted_paths: list[str] = []
    diffs: list[dict[str, str]] = []
    unsupported: list[str] = []
    total_content_bytes = 0
    total_diff_bytes = 0
    for path, source_entry in changed:
        if source_entry is None and path in source_blobs:
            source_entry = source_blobs[path]
            if source_entry.get("mode") != "100644":
                unsupported.append(path)
                continue
            baseline = await github.text_file(repository.full_name, path, workspace_record.source_commit_sha or "", max_bytes=1_000_000)
            if baseline is None:
                unsupported.append(path)
                continue
            total_content_bytes += len(baseline.encode("utf-8"))
            deleted_paths.append(path)
            diff = "".join(
                difflib.unified_diff(
                    baseline.splitlines(keepends=True), [], fromfile=f"a/{path}", tofile="/dev/null"
                )
            )
        else:
            try:
                raw = manager.read_file(workspace, path, max_bytes=1_000_000)
                content = raw.decode("utf-8")
            except (WorkspaceError, UnicodeDecodeError) as exc:
                raise HTTPException(status_code=422, detail=f"Delivery supports UTF-8 text files up to 1 MB: {path}") from exc
            if source_entry is None:
                folded = path.casefold()
                collision = any(
                    folded == existing
                    or existing.startswith(f"{folded}/")
                    or (source_tree_entries[original].get("type") == "blob" and folded.startswith(f"{existing}/"))
                    for existing, original in source_casefold.items()
                )
                if collision:
                    raise HTTPException(status_code=409, detail=f"Workspace path conflicts with a source path: {path}")
                baseline = ""
            else:
                if source_entry.get("mode") == "100755":
                    raise HTTPException(status_code=409, detail=f"Delivery cannot preserve the executable mode of a changed source file: {path}")
                baseline = await github.text_file(
                    repository.full_name, path, workspace_record.source_commit_sha or "", max_bytes=1_000_000
                )
                if baseline is None:
                    raise HTTPException(status_code=409, detail=f"Changed source file is binary or exceeds 1 MB: {path}")
            if content == baseline:
                continue
            total_content_bytes += len(content.encode("utf-8"))
            files.append({"path": path, "content": content})
            diff = "".join(
                difflib.unified_diff(
                    baseline.splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile=f"a/{path}" if baseline else "/dev/null",
                    tofile=f"b/{path}",
                )
            )
        if total_content_bytes > _MAX_DELIVERY_BYTES:
            raise HTTPException(status_code=413, detail="Delivery exceeds the 50-file or 500 KB limit")
        total_diff_bytes += len(diff.encode("utf-8"))
        if total_diff_bytes > _MAX_DELIVERY_DIFF_BYTES:
            raise HTTPException(status_code=413, detail="Rendered delivery diff exceeds the 1 MB limit")
        diffs.append({"path": path, "diff": diff})

    payload = {
        "source_commit_sha": workspace_record.source_commit_sha,
        "base_branch": base_branch,
        "files": files,
        "deleted_paths": deleted_paths,
        "unsupported_missing_source_paths": unsupported,
    }
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "repository_id": str(repository.id),
        "source_commit_sha": workspace_record.source_commit_sha,
        "base_branch": base_branch,
        "files": files,
        "deleted_paths": deleted_paths,
        "diffs": diffs,
        "unsupported_missing_source_paths": unsupported,
        "diff_digest": digest,
        "can_deliver": bool(files or deleted_paths) and not unsupported,
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
    await bind_tenant_scope(db, tenant_id)
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
    await bind_tenant_scope(db, task.tenant_id)
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
        "attempt_count": run.attempt_count,
        "attempt_started_at": run.attempt_started_at.isoformat() if run.attempt_started_at else None,
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
    evidence = _verification_history(list(runs))
    if not runs:
        return evidence
    attempt_events = (
        await db.execute(
            select(VerificationAttemptEvent)
            .where(
                VerificationAttemptEvent.tenant_id == tenant_id,
                VerificationAttemptEvent.task_id == task_id,
            )
            .order_by(
                VerificationAttemptEvent.verification_run_id,
                VerificationAttemptEvent.attempt_number,
                VerificationAttemptEvent.occurred_at,
                VerificationAttemptEvent.id,
            )
            .limit(1001)
        )
    ).scalars().all()
    by_run: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for event in attempt_events[:1000]:
        by_run.setdefault(event.verification_run_id, []).append(
            _verification_attempt_event_dict(event, include_output=False)
        )
    attempt_history_truncated = len(attempt_events) > 1000
    for item in evidence:
        run_id = uuid.UUID(item["id"])
        item["attempt_history"] = by_run.get(run_id, [])
        item["attempt_history_truncated"] = attempt_history_truncated
    return evidence


def _verification_attempt_event_dict(
    event: VerificationAttemptEvent,
    *,
    include_output: bool = True,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(event.id),
        "attempt_number": event.attempt_number,
        "event_type": event.event_type,
        "status": event.status,
        "exit_code": event.exit_code,
        "timed_out": event.timed_out,
        "output_truncated": event.output_truncated,
        "error": event.error if include_output else (event.error[:1000] if event.error else None),
        "duration_seconds": float(event.duration_seconds) if event.duration_seconds is not None else None,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
    }
    if include_output:
        result["stdout"] = event.stdout
        result["stderr"] = event.stderr
    return result


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


async def _finish_run(
    db: AsyncSession,
    run: VerificationRun,
    lease_owner: uuid.UUID,
    values: dict[str, Any],
) -> dict[str, Any]:
    await bind_tenant_scope(db, run.tenant_id)
    completed = await db.execute(
        update(VerificationRun)
        .where(
            VerificationRun.id == run.id,
            VerificationRun.tenant_id == run.tenant_id,
            VerificationRun.status == "running",
            VerificationRun.lease_owner == lease_owner,
        )
        .values(lease_owner=None, lease_expires_at=None, **values)
        .returning(VerificationRun.id)
    )
    if completed.scalar_one_or_none() is None:
        await bind_tenant_scope(db, run.tenant_id)
        await db.refresh(run)
        evidence = await _verification_history_for_task(db, run.tenant_id, run.task_id)
        return next(item for item in evidence if item["id"] == str(run.id))
    await db.commit()
    await bind_tenant_scope(db, run.tenant_id)
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

    if payload.repository_id is None and (payload.repository_paths is not None or payload.full_repository):
        raise HTTPException(status_code=422, detail="Repository paths and full_repository require a repository_id")
    if payload.repository_id is not None and payload.files:
        raise HTTPException(status_code=422, detail="Provide either initial files or a repository source, not both")
    if payload.full_repository and payload.repository_paths is not None:
        raise HTTPException(status_code=422, detail="Select repository paths or request full_repository, not both")
    if payload.repository_id is not None and not payload.full_repository and payload.repository_paths is None:
        raise HTTPException(status_code=422, detail="Select repository paths or request full_repository")

    initial_files = dict(payload.files)
    source_commit_sha: str | None = None
    source_paths: list[str] | None = None
    repository: Repository | None = None
    repository_branch: str | None = None
    repository_tree_sha: str | None = None
    git_token: str | None = None
    if payload.repository_id is not None:
        if not payload.full_repository and not payload.repository_paths:
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
        selected_repository_paths: list[str] = []
        for path in payload.repository_paths or []:
            try:
                normalized = safe_relative_path(path).as_posix()
            except WorkspaceError as exc:
                raise HTTPException(status_code=422, detail="Repository path is invalid") from exc
            if (
                normalized != path
                or len(normalized) > 512
                or any(part.casefold() == ".git" for part in normalized.split("/"))
                or normalized.casefold() in {selected.casefold() for selected in selected_repository_paths}
            ):
                raise HTTPException(status_code=422, detail="Repository paths must be normalized and unique")
            selected_repository_paths.append(normalized)
        source_paths = None if payload.full_repository else selected_repository_paths

        try:
            try:
                github_provider = await get_authorized_repository_provider(
                    repository.provider, db, repository, project.organization_id
                )
            except GitHubCredentialNotFoundError as exc:
                raise HTTPException(status_code=409, detail="Reconnect the GitHub organization integration") from exc
            except CredentialEncryptionUnavailableError as exc:
                raise HTTPException(
                    status_code=503,
                    detail="GitHub credential cannot be decrypted with current configuration",
                ) from exc
            async with github_provider as github:
                metadata = await github.repository_metadata(repository.full_name)
                repository_branch = str(metadata["default_branch"])
                source_commit_sha = await github.branch_commit(repository.full_name, repository_branch)
                repository_tree_sha, tree = await github.repository_tree(repository.full_name, source_commit_sha)
                tree_entries = {entry["path"]: entry for entry in tree}
                if payload.full_repository:
                    try:
                        validate_repository_tree(tree)
                    except RepositoryWorktreeError as exc:
                        status_code = 413 if "limit" in str(exc) or "too many" in str(exc) else 422
                        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
                    if bool(metadata.get("private")) and not getattr(github, "git_auth_token", lambda: None)():
                        raise HTTPException(status_code=409, detail="Reconnect the GitHub integration to clone this private repository")
                    git_token = getattr(github, "git_auth_token", lambda: None)()
                selected_entries = []
                for path in source_paths or []:
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
        if payload.full_repository:
            if repository is None or not source_commit_sha or not repository_branch or not repository_tree_sha:
                raise HTTPException(status_code=502, detail="GitHub returned incomplete pinned repository metadata")
            await materialize_repository_worktree(
                workspace.path,
                full_name=repository.full_name,
                branch=repository_branch,
                expected_sha=source_commit_sha,
                expected_tree_sha=repository_tree_sha,
                token=git_token,
                tree=tree,
            )
            workspace_size = manager.validate_tree(workspace)
            if workspace_size > _MAX_FULL_WORKSPACE_BYTES:
                raise RepositoryWorktreeError("Checked out workspace exceeds the 128 MiB execution workspace limit")
        else:
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
        status_code = 413 if "size limit" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except RepositoryWorktreeError as exc:
        manager.delete(workspace.id)
        await db.rollback()
        status_code = 413 if "limit" in str(exc) or "too many" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
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
        "workspace_mode": "full_repository" if payload.full_repository else "selected_files" if repository else "manual",
    }


@router.post("/verification-runs", status_code=202, summary="Request approval for a sandbox verification check")
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
    handoff = (
        await db.execute(
            select(TaskHandoff).where(TaskHandoff.tenant_id == task.tenant_id, TaskHandoff.child_task_id == task.id)
        )
    ).scalar_one_or_none()
    if handoff is None:
        raise HTTPException(status_code=409, detail="Verification approval requires a project-assigned teammate task")
    project = await db.get(Project, handoff.project_id)
    if project is None or project.tenant_id != task.tenant_id:
        raise HTTPException(status_code=404, detail="Task project not found")
    if tenant_context.user_id is not None:
        await require_org_membership(db, project.organization_id, roles=MEMBER_OR_ABOVE)
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
        status="pending_approval",
        runner_version="sandbox-service/1" if settings.sandbox_runtime_url else "docker-sandbox/1",
        source_digest=source_digest,
    )
    run_id = uuid.uuid4()
    run.id = run_id
    approval_payload = json.dumps(
        {
            "execution_id": str(execution_id),
            "task_id": str(task.id),
            "verification_run_id": str(run_id),
            "workspace_id": str(workspace_record.id),
            "kind": payload.kind,
            "command": command,
            "source_digest": source_digest,
        }
    )
    approval = Approval(
        organization_id=project.organization_id,
        project_id=project.id,
        teammate_id=handoff.teammate_id,
        verification_run_id=run_id,
        kind="verification_run",
        summary=f"Run {payload.kind} verification for {task.title}",
        payload=approval_payload,
        requested_by=str(tenant_context.user_id or "User"),
    )
    db.add(run)
    await db.flush()
    db.add(approval)
    await db.commit()
    await bind_tenant_scope(db, task.tenant_id)
    await db.refresh(run)
    await db.refresh(approval)
    return {
        "verification_run": _verification_dict(run),
        "approval_id": str(approval.id),
        "approval_status": approval.status,
    }


async def _execute_approved_verification(
    db: AsyncSession,
    task: Task,
    run: VerificationRun,
    approval: Approval,
    *,
    recover_interrupted: bool = False,
) -> dict[str, Any]:
    allowed_run_statuses = ("approved", "interrupted") if recover_interrupted else ("approved",)
    allowed_approval_statuses = ("approved", "consumed") if recover_interrupted else ("approved",)
    if run.status not in allowed_run_statuses or approval.status not in allowed_approval_statuses:
        raise HTTPException(status_code=409, detail="Verification run does not have an unused approval")

    # Serialize claims with the administrator's pause transaction. The second
    # check after discovery makes a committed pause authoritative for every new
    # claim; already-running sandbox work is left untouched.
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(approval.organization_id), 0)))
    )
    control = (
        await db.execute(
            select(
                VerificationQueueControl.paused,
                VerificationQueueControl.max_concurrent_runs,
                VerificationQueueControl.max_runs_per_hour,
            ).where(
                VerificationQueueControl.organization_id == approval.organization_id,
                VerificationQueueControl.tenant_id == task.tenant_id,
            )
        )
    ).one_or_none()
    queue_paused = control[0] if control is not None else False
    if queue_paused:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Verification queue is paused for this organization")

    max_concurrent_runs = control[1] if control is not None else 2
    active_runs = await db.scalar(
        select(func.count())
        .select_from(VerificationRun)
        .join(Approval, Approval.verification_run_id == VerificationRun.id)
        .where(
            VerificationRun.tenant_id == task.tenant_id,
            VerificationRun.status == "running",
            Approval.organization_id == approval.organization_id,
        )
    )
    if int(active_runs or 0) >= max_concurrent_runs:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Organization verification concurrency limit reached")

    max_runs_per_hour = control[2] if control is not None else 30
    recent_attempts = await db.scalar(
        select(func.count())
        .select_from(VerificationAttemptEvent)
        .join(Approval, Approval.verification_run_id == VerificationAttemptEvent.verification_run_id)
        .where(
            VerificationAttemptEvent.tenant_id == task.tenant_id,
            VerificationAttemptEvent.event_type == "started",
            VerificationAttemptEvent.occurred_at >= func.now() - timedelta(hours=1),
            VerificationAttemptEvent.occurred_at <= func.now(),
            Approval.organization_id == approval.organization_id,
        )
    )
    if int(recent_attempts or 0) >= max_runs_per_hour:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Organization verification hourly rate limit reached")

    lease_owner = uuid.uuid4()
    run_claim = await db.execute(
        update(VerificationRun)
        .where(
            VerificationRun.id == run.id,
            VerificationRun.tenant_id == task.tenant_id,
            VerificationRun.status.in_(allowed_run_statuses),
            or_(
                VerificationRun.status != "interrupted",
                VerificationRun.lease_expires_at.is_(None),
                VerificationRun.lease_expires_at <= func.now(),
            ),
            VerificationRun.attempt_count < _VERIFICATION_MAX_ATTEMPTS,
        )
        .values(
            status="running",
            attempt_count=VerificationRun.attempt_count + 1,
            attempt_started_at=func.now(),
            lease_owner=lease_owner,
            lease_expires_at=func.now() + _VERIFICATION_LEASE_DURATION,
            completed_at=None,
            error=None,
            exit_code=None,
            timed_out=False,
            output_truncated=False,
            stdout="",
            stderr="",
            duration_seconds=None,
        )
        .returning(VerificationRun.id)
    )
    if run_claim.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Verification run is already claimed or exhausted its retry limit")

    approval_claim = await db.execute(
        update(Approval)
        .where(Approval.id == approval.id, Approval.status.in_(allowed_approval_statuses))
        .values(status="consumed", updated_at=datetime.now(UTC))
        .returning(Approval.id)
    )
    if approval_claim.scalar_one_or_none() is None:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Verification approval has already been used")
    await db.commit()
    await bind_tenant_scope(db, task.tenant_id)
    await db.refresh(run)

    # Claim first so workspace tools observe the running state and cannot race
    # a digest check while an interrupted, already-approved run is recovering.
    try:
        workspace = _workspace_manager().get(run.workspace_id)
        current_digest = _workspace_manager().fingerprint(workspace)
    except WorkspaceError:
        current_digest = None
    if current_digest != run.source_digest:
        stale_run = await db.execute(
            update(VerificationRun)
            .where(
                VerificationRun.id == run.id,
                VerificationRun.tenant_id == task.tenant_id,
                VerificationRun.status == "running",
                VerificationRun.lease_owner == lease_owner,
            )
            .values(
                status="stale",
                lease_owner=None,
                lease_expires_at=None,
                error="Workspace changed after this verification request was approved",
                completed_at=datetime.now(UTC),
            )
            .returning(VerificationRun.id)
        )
        stale_approval = await db.execute(
            update(Approval)
            .where(Approval.id == approval.id, Approval.status == "consumed")
            .values(status="stale", updated_at=datetime.now(UTC))
            .returning(Approval.id)
        )
        if stale_run.scalar_one_or_none() is None or stale_approval.scalar_one_or_none() is None:
            await db.rollback()
            raise HTTPException(status_code=409, detail="Verification approval has already been used")
        await db.commit()
        raise HTTPException(status_code=409, detail="Workspace changed after approval; request a new verification approval") from None

    command = json.loads(run.command)
    try:
        result: SandboxResult | RemoteSandboxResult = await _sandbox_runner().run(workspace, command)
    except asyncio.CancelledError:
        await _finish_run(
            db,
            run,
            lease_owner,
            {
                "status": "dead_letter" if run.attempt_count >= _VERIFICATION_MAX_ATTEMPTS else "interrupted",
                "error": (
                    "Verification exceeded the maximum number of recovery attempts"
                    if run.attempt_count >= _VERIFICATION_MAX_ATTEMPTS
                    else "Sandbox verification was interrupted before completion"
                ),
                "completed_at": datetime.now(UTC),
            },
        )
        raise
    except (SandboxRuntimeError, WorkspaceError, ValueError) as exc:
        return await _finish_run(
            db,
            run,
            lease_owner,
            {"status": "failed", "error": str(exc)[:_MAX_STORED_OUTPUT_CHARS], "completed_at": datetime.now(UTC)},
        )
    except Exception as exc:
        return await _finish_run(
            db,
            run,
            lease_owner,
            {
                "status": "failed",
                "error": f"Sandbox execution failed unexpectedly ({type(exc).__name__})",
                "completed_at": datetime.now(UTC),
            },
        )
    return await _finish_run(
        db,
        run,
        lease_owner,
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


@router.post("/verification-runs/{verification_run_id}/execute", summary="Execute an approved sandbox verification")
async def execute_approved_task_verification(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    verification_run_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    run = await db.get(VerificationRun, verification_run_id)
    if run is None or run.tenant_id != task.tenant_id or run.task_id != task.id or run.execution_id != execution_id:
        raise HTTPException(status_code=404, detail="Verification request not found")
    approval = (
        await db.execute(select(Approval).where(Approval.verification_run_id == run.id))
    ).scalar_one_or_none()
    if approval is None:
        raise HTTPException(status_code=409, detail="Verification request has no approval record")
    await require_org_membership(db, approval.organization_id, roles=OWNER_ADMIN)
    return await _execute_approved_verification(db, task, run, approval)


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


@router.get("/verification-runs/{verification_run_id}/attempts", summary="List immutable verification attempt evidence")
async def list_verification_attempt_events(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    verification_run_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    run = (
        await db.execute(
            select(VerificationRun).where(
                VerificationRun.id == verification_run_id,
                VerificationRun.tenant_id == task.tenant_id,
                VerificationRun.task_id == task.id,
                VerificationRun.execution_id == execution_id,
            )
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="Verification request not found")
    rows = (
        await db.execute(
            select(VerificationAttemptEvent)
            .where(
                VerificationAttemptEvent.tenant_id == task.tenant_id,
                VerificationAttemptEvent.task_id == task.id,
                VerificationAttemptEvent.verification_run_id == run.id,
            )
            .order_by(VerificationAttemptEvent.attempt_number, VerificationAttemptEvent.occurred_at, VerificationAttemptEvent.id)
            .limit(21)
        )
    ).scalars().all()
    return {
        "verification_run_id": str(run.id),
        "attempts": [_verification_attempt_event_dict(event) for event in rows[:20]],
        "truncated": len(rows) > 20,
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


def _activity_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _activity_sort_timestamp(value: str | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _activity_task_tree_ids(db: AsyncSession, task: Task) -> tuple[list[uuid.UUID], bool]:
    ids = [task.id]
    seen = {task.id}
    frontier = [task.id]
    for depth in range(_ACTIVITY_TASK_TREE_DEPTH_LIMIT):
        remaining = _ACTIVITY_TASK_TREE_LIMIT - len(ids)
        result = await db.execute(
            select(Task.id)
            .where(
                Task.tenant_id == task.tenant_id,
                Task.execution_id == task.execution_id,
                Task.parent_task_id.in_(frontier),
                Task.id.not_in(seen),
            )
            .order_by(Task.id)
            .limit(remaining + 1)
        )
        children = list(result.scalars().all())
        truncated = len(children) > remaining
        if truncated:
            children = children[:remaining]
        ids.extend(children)
        seen.update(children)
        if truncated:
            return ids, True
        frontier = children
        if not frontier:
            return ids, False

    # Probe one additional level so an exactly-32-level tree is not reported
    # truncated unless another descendant actually exists.
    probe = await db.execute(
        select(Task.id)
        .where(
            Task.tenant_id == task.tenant_id,
            Task.execution_id == task.execution_id,
            Task.parent_task_id.in_(frontier),
            Task.id.not_in(seen),
        )
        .limit(1)
    )
    return ids, bool(probe.scalars().first())


async def _refresh_task_pull_request_statuses(
    db: AsyncSession,
    task: Task,
    deliveries: list[TaskDelivery],
) -> dict[uuid.UUID, dict[str, Any]]:
    related_deliveries = [
        item
        for item in deliveries
        if isinstance(item.pull_request_number, int) and item.pull_request_number > 0
    ]
    statuses: dict[uuid.UUID, dict[str, Any]] = {}
    if not related_deliveries:
        return statuses

    unavailable = {"availability": "unavailable", "reason": "repository_access_unavailable"}

    workspace = (
        await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.tenant_id == task.tenant_id,
                TaskWorkspace.task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    handoff = (
        await db.execute(
            select(TaskHandoff).where(
                TaskHandoff.tenant_id == task.tenant_id,
                TaskHandoff.child_task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    if workspace is None or workspace.repository_id is None or handoff is None:
        return {item.id: unavailable for item in related_deliveries}

    deliveries_by_number: dict[int, list[TaskDelivery]] = {}
    for item in related_deliveries:
        pull_request_number = item.pull_request_number
        if not isinstance(pull_request_number, int):
            continue
        if item.workspace_id != workspace.id or item.repository_id != workspace.repository_id:
            statuses[item.id] = {"availability": "unavailable", "reason": "repository_mismatch"}
            continue
        deliveries_by_number.setdefault(pull_request_number, []).append(item)
    pull_request_numbers = list(deliveries_by_number)
    if not pull_request_numbers:
        return statuses

    project = await db.get(Project, handoff.project_id)
    repository = await db.get(Repository, workspace.repository_id)
    organization = await db.get(Organization, project.organization_id) if project is not None else None
    if (
        project is None
        or project.tenant_id != task.tenant_id
        or not project.is_active
        or organization is None
        or organization.tenant_id != task.tenant_id
        or not organization.is_active
        or repository is None
        or repository.provider != "github"
        or repository.project_id != project.id
        or not repository.is_active
    ):
        statuses.update({item.id: unavailable for item in related_deliveries if item.id not in statuses})
        return statuses

    if tenant_context.user_id is not None:
        try:
            await require_org_membership(db, project.organization_id, roles=ANY_ROLE)
        except HTTPException:
            statuses.update({item.id: unavailable for item in related_deliveries if item.id not in statuses})
            return statuses

    refresh_numbers = pull_request_numbers[:_MAX_LIVE_PULL_REQUESTS]
    statuses_by_number = {
        number: {"availability": "not_refreshed", "reason": "refresh_limit"}
        for number in pull_request_numbers[_MAX_LIVE_PULL_REQUESTS:]
    }
    try:
        provider = await get_authorized_repository_provider(
            repository.provider, db, repository, project.organization_id
        )
        async with provider as github:
            async def refresh(number: int) -> tuple[int, dict[str, Any]]:
                try:
                    current = await github.pull_request_status(repository.full_name, number)
                except GitHubRepositoryError as exc:
                    reason = "not_found" if exc.status_code == 404 else "github_unavailable"
                    return number, {"availability": "unavailable", "reason": reason}
                return number, {
                    "availability": "current",
                    "number": number,
                    "title": str(current.get("title") or "")[:512],
                    "state": str(current.get("state") or "unknown")[:32],
                    "draft": bool(current.get("draft", False)),
                    "merged": bool(current.get("merged", False)),
                    "review_status": current.get("review_status"),
                }

            refreshed = await asyncio.gather(*(refresh(number) for number in refresh_numbers))
            statuses_by_number.update(refreshed)
    except (CredentialEncryptionUnavailableError, GitHubCredentialNotFoundError, GitHubRepositoryError):
        statuses_by_number.update({number: unavailable for number in refresh_numbers})
    except Exception:
        # GitHub status is supplementary; remote failure must not hide durable history.
        statuses_by_number.update({number: unavailable for number in refresh_numbers})
    for number, items in deliveries_by_number.items():
        for item in items:
            statuses[item.id] = statuses_by_number[number]
    return statuses


@router.get("/activity", summary="List a unified task activity feed")
async def list_task_activity(
    execution_id: uuid.UUID,
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    task = await _owned_task(execution_id, task_id, db)
    task_ids, tree_truncated = await _activity_task_tree_ids(db, task)

    invocations = (
        await db.execute(
            select(TeammateToolInvocation)
            .where(TeammateToolInvocation.tenant_id == task.tenant_id, TeammateToolInvocation.task_id.in_(task_ids))
            .order_by(TeammateToolInvocation.started_at.desc(), TeammateToolInvocation.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    runs = (
        await db.execute(
            select(VerificationRun)
            .where(VerificationRun.tenant_id == task.tenant_id, VerificationRun.task_id.in_(task_ids))
            .order_by(VerificationRun.started_at.desc(), VerificationRun.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    attempt_events = (
        await db.execute(
            select(VerificationAttemptEvent)
            .where(
                VerificationAttemptEvent.tenant_id == task.tenant_id,
                VerificationAttemptEvent.task_id.in_(task_ids),
            )
            .order_by(VerificationAttemptEvent.occurred_at.desc(), VerificationAttemptEvent.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    deliveries = (
        await db.execute(
            select(TaskDelivery)
            .where(TaskDelivery.tenant_id == task.tenant_id, TaskDelivery.task_id.in_(task_ids))
            .order_by(TaskDelivery.created_at.desc(), TaskDelivery.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    handoffs = (
        await db.execute(
            select(TaskHandoff)
            .where(
                TaskHandoff.tenant_id == task.tenant_id,
                or_(TaskHandoff.parent_task_id.in_(task_ids), TaskHandoff.child_task_id.in_(task_ids)),
            )
            .order_by(TaskHandoff.created_at.desc(), TaskHandoff.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    task_events = (
        await db.execute(
            select(TaskEvent)
            .where(TaskEvent.tenant_id == task.tenant_id, TaskEvent.task_id.in_(task_ids))
            .order_by(TaskEvent.occurred_at.desc(), TaskEvent.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    orchestration_events = (
        await db.execute(
            select(OrchestrationEvent)
            .where(
                OrchestrationEvent.tenant_id == task.tenant_id,
                or_(
                    OrchestrationEvent.task_id.in_(task_ids),
                    and_(
                        OrchestrationEvent.execution_id == execution_id,
                        OrchestrationEvent.entity_type == "workflow_execution",
                    ),
                ),
            )
            .order_by(OrchestrationEvent.occurred_at.desc(), OrchestrationEvent.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()
    usage_records = (
        await db.execute(
            select(TokenUsage)
            .where(TokenUsage.tenant_id == task.tenant_id, TokenUsage.task_id.in_(task_ids))
            .order_by(TokenUsage.created_at.desc(), TokenUsage.id.desc())
            .limit(_ACTIVITY_SOURCE_LIMIT + 1)
        )
    ).scalars().all()

    handoff_ids = [item.id for item in handoffs]
    run_ids = [item.id for item in runs]
    approval_filters = []
    if handoff_ids:
        approval_filters.append(Approval.task_handoff_id.in_(handoff_ids))
    if run_ids:
        approval_filters.append(Approval.verification_run_id.in_(run_ids))
    approvals: list[Approval] = []
    if approval_filters:
        approvals = list(
            (
                await db.execute(
                    select(Approval)
                    .join(Organization, Organization.id == Approval.organization_id)
                    .where(Organization.tenant_id == task.tenant_id, or_(*approval_filters))
                    .order_by(Approval.created_at.desc(), Approval.id.desc())
                    .limit(_ACTIVITY_SOURCE_LIMIT + 1)
                )
            )
            .scalars()
            .all()
        )

    source_truncated = any(
        len(records) > _ACTIVITY_SOURCE_LIMIT
        for records in (
            invocations,
            runs,
            attempt_events,
            deliveries,
            handoffs,
            approvals,
            task_events,
            orchestration_events,
            usage_records,
        )
    )
    source_truncated = source_truncated or tree_truncated
    invocations = invocations[:_ACTIVITY_SOURCE_LIMIT]
    runs = runs[:_ACTIVITY_SOURCE_LIMIT]
    attempt_events = attempt_events[:_ACTIVITY_SOURCE_LIMIT]
    deliveries = deliveries[:_ACTIVITY_SOURCE_LIMIT]
    handoffs = handoffs[:_ACTIVITY_SOURCE_LIMIT]
    approvals = approvals[:_ACTIVITY_SOURCE_LIMIT]
    task_events = task_events[:_ACTIVITY_SOURCE_LIMIT]
    orchestration_events = orchestration_events[:_ACTIVITY_SOURCE_LIMIT]
    usage_records = usage_records[:_ACTIVITY_SOURCE_LIMIT]

    root_deliveries = [delivery for delivery in deliveries if delivery.task_id == task.id]
    live_pr_statuses = await _refresh_task_pull_request_statuses(db, task, root_deliveries)

    def task_prefix(related_task_id: uuid.UUID) -> str:
        if related_task_id == task.id:
            return ""
        return f"Child task {str(related_task_id)[:8]}: "

    events: list[dict[str, Any]] = []
    for task_event in task_events:
        prefix = task_prefix(task_event.task_id)
        if task_event.event_type == "baseline":
            summary = f"{prefix}History baseline: {task_event.status.replace('_', ' ')}"
        elif task_event.event_type == "created":
            summary = f"{prefix}Created with status {task_event.status.replace('_', ' ')}"
        else:
            previous = task_event.previous_status or "unknown"
            summary = f"{prefix}Status changed from {previous.replace('_', ' ')} to {task_event.status.replace('_', ' ')}"
        events.append(
            {
                "id": f"task_lifecycle:{task_event.id}",
                "type": "task_lifecycle",
                "status": task_event.status,
                "timestamp": _activity_timestamp(task_event.occurred_at),
                "summary": summary,
                "details": {
                    "task_id": str(task_event.task_id),
                    "parent_task_id": str(task_event.parent_task_id) if task_event.parent_task_id else None,
                    "source": task_event.source,
                    "actor_user_id": str(task_event.actor_user_id) if task_event.actor_user_id else None,
                },
            }
        )
    for transition in orchestration_events:
        previous = transition.previous_status or "none"
        entity_name = {
            "approval": "Approval",
            "handoff": "Task handoff",
            "acceptance_criterion": "Acceptance criterion",
            "dependency": "Task dependency",
            "workflow_execution": "Workflow execution",
        }.get(transition.entity_type, "Task record")
        transition_task_id = transition.task_id
        prefix = task_prefix(transition_task_id) if transition_task_id is not None else ""
        if transition.event_type == "created":
            summary = f"{prefix}{entity_name} created as {transition.status.replace('_', ' ')}"
        elif transition.event_type == "deleted":
            summary = f"{prefix}{entity_name} removed"
        elif transition.event_type == "updated":
            summary = f"{prefix}{entity_name} details updated"
        elif transition.event_type == "claimed":
            summary = f"{prefix}{entity_name} worker claimed the run again at {transition.status.replace('_', ' ')}"
        else:
            summary = (
                f"{prefix}{entity_name} changed from "
                f"{previous.replace('_', ' ')} to {transition.status.replace('_', ' ')}"
            )
        events.append(
            {
                "id": f"orchestration:{transition.id}",
                "type": "orchestration_transition",
                "status": transition.status,
                "timestamp": _activity_timestamp(transition.occurred_at),
                "summary": summary,
                "details": {
                    "task_id": str(transition_task_id) if transition_task_id else None,
                    "execution_id": str(transition.execution_id) if transition.execution_id else None,
                    "entity_type": transition.entity_type,
                    "entity_id": str(transition.entity_id),
                    "event_type": transition.event_type,
                    "previous_status": transition.previous_status,
                    "source": transition.source,
                    "actor_user_id": str(transition.actor_user_id) if transition.actor_user_id else None,
                    "payload": transition.payload,
                },
            }
        )
    for invocation in invocations:
        events.append(
            {
                "id": f"tool:{invocation.id}",
                "type": "tool",
                "status": invocation.status,
                "timestamp": _activity_timestamp(invocation.completed_at or invocation.started_at),
                "summary": f"{task_prefix(invocation.task_id)}Teammate used {invocation.tool_name}",
                "details": {
                    "task_id": str(invocation.task_id),
                    "path": invocation.path,
                    "error_code": invocation.error_code,
                },
            }
        )
    for usage in usage_records:
        if usage.task_id is None:
            continue
        cost = float(usage.cost or 0)
        events.append(
            {
                "id": f"llm_usage:{usage.id}",
                "type": "llm_usage",
                "status": "recorded",
                "timestamp": _activity_timestamp(usage.created_at),
                "summary": (
                    f"{task_prefix(usage.task_id)}{usage.agent_type} used {usage.total_tokens} tokens "
                    f"with {usage.model} (cost ${cost:.6f})"
                ),
                "details": {
                    "task_id": str(usage.task_id),
                    "agent_type": usage.agent_type,
                    "provider": usage.provider,
                    "model": usage.model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "cost": cost,
                },
            }
        )
    for run in runs:
        events.append(
            {
                "id": f"verification:{run.id}",
                "type": "verification",
                "status": run.status,
                "timestamp": _activity_timestamp(run.completed_at or run.started_at),
                "summary": f"{task_prefix(run.task_id)}{run.kind.title()} verification {run.status.replace('_', ' ')}",
                "details": {"task_id": str(run.task_id), "exit_code": run.exit_code, "timed_out": run.timed_out},
            }
        )
    for attempt in attempt_events:
        prefix = task_prefix(attempt.task_id)
        if attempt.event_type == "snapshot":
            summary = f"{prefix}Verification result snapshot: {attempt.status.replace('_', ' ')}"
        elif attempt.event_type == "started":
            summary = f"{prefix}Verification attempt {attempt.attempt_number} started"
        else:
            summary = (
                f"{prefix}Verification attempt {attempt.attempt_number} "
                f"{attempt.status.replace('_', ' ')}"
            )
        events.append(
            {
                "id": f"verification_attempt:{attempt.id}",
                "type": "verification_attempt",
                "status": attempt.status,
                "timestamp": _activity_timestamp(attempt.occurred_at),
                "summary": summary,
                "details": {
                    "task_id": str(attempt.task_id),
                    "verification_run_id": str(attempt.verification_run_id),
                    "verification_kind": attempt.verification_kind,
                    "attempt_number": attempt.attempt_number,
                    "event_type": attempt.event_type,
                    "exit_code": attempt.exit_code,
                    "timed_out": attempt.timed_out,
                    "output_truncated": attempt.output_truncated,
                    "error": attempt.error[:1000] if attempt.error else None,
                },
            }
        )
    for delivery in deliveries:
        number = delivery.pull_request_number
        delivery_summary = delivery.pull_request_title or f"GitHub delivery {delivery.status.replace('_', ' ')}"
        events.append(
            {
                "id": f"delivery:{delivery.id}",
                "type": "delivery",
                "status": delivery.status,
                "timestamp": _activity_timestamp(delivery.updated_at or delivery.created_at),
                "summary": f"{task_prefix(delivery.task_id)}{delivery_summary}",
                "details": {
                    "task_id": str(delivery.task_id),
                    "branch": delivery.branch_name,
                    "pull_request_number": number,
                    "pull_request_status": (
                        live_pr_statuses.get(delivery.id)
                        if delivery.task_id == task.id and number
                        else (
                            {"availability": "unavailable", "reason": "descendant_status_not_refreshed"}
                            if number
                            else None
                        )
                    ),
                },
            }
        )
    for handoff in handoffs:
        direction = "delegated" if handoff.parent_task_id in task_ids else "received"
        related_task_id = handoff.parent_task_id if direction == "delegated" else handoff.child_task_id
        events.append(
            {
                "id": f"handoff:{handoff.id}",
                "type": "handoff",
                "status": handoff.status,
                "timestamp": _activity_timestamp(handoff.completed_at or handoff.created_at),
                "summary": (
                    f"{task_prefix(related_task_id)}Current handoff status: "
                    f"{handoff.status.replace('_', ' ')} (task {direction} by {handoff.delegated_to})"
                ),
                "details": {
                    "task_id": str(related_task_id),
                    "parent_task_id": str(handoff.parent_task_id),
                    "child_task_id": str(handoff.child_task_id),
                    "reason": handoff.reason[:1000],
                },
            }
        )
    approval_task_ids = {run.id: run.task_id for run in runs}
    approval_task_ids.update(
        {
            handoff.id: handoff.parent_task_id if handoff.parent_task_id in task_ids else handoff.child_task_id
            for handoff in handoffs
        }
    )
    for approval in approvals:
        approval_task_id = None
        if approval.verification_run_id is not None:
            approval_task_id = approval_task_ids.get(approval.verification_run_id)
        if approval_task_id is None and approval.task_handoff_id is not None:
            approval_task_id = approval_task_ids.get(approval.task_handoff_id)
        prefix = task_prefix(approval_task_id) if approval_task_id is not None else ""
        events.append(
            {
                "id": f"approval:{approval.id}",
                "type": "approval",
                "status": approval.status,
                "timestamp": _activity_timestamp(approval.decided_at or approval.updated_at or approval.created_at),
                "summary": (
                    f"{prefix}Current {approval.kind.replace('_', ' ')} approval status: "
                    f"{approval.status.replace('_', ' ')}"
                ),
                "details": {
                    "task_id": str(approval_task_id) if approval_task_id is not None else None,
                    "summary": approval.summary[:1000],
                    "decision_note": (approval.decision_note or "")[:1000],
                },
            }
        )

    events.sort(key=lambda event: _activity_sort_timestamp(event["timestamp"]), reverse=True)
    truncated = source_truncated or len(events) > _ACTIVITY_TOTAL_LIMIT
    return {
        "task_id": str(task.id),
        "activity": events[:_ACTIVITY_TOTAL_LIMIT],
        "count": min(len(events), _ACTIVITY_TOTAL_LIMIT),
        "truncated": truncated,
        "github_status_freshness": "live_snapshot_when_available",
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
    await db.execute(
        text("SELECT set_config('app.task_event_actor_user_id', :actor, true)"),
        {"actor": str(reviewer_user_id)},
    )
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
                    detail="Delivery requires at least one supported change and no unsupported missing source files",
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
                    deleted_paths=preview["deleted_paths"],
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
