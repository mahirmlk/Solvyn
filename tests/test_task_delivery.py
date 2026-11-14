"""Bounded diff previews for repository-backed task workspaces."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from solvyn.repositories.github import GitHubRepositoryError
from solvyn.sandbox.workspaces import WorkspaceManager


def _verification_api() -> Any:
    from solvyn.api.v1 import verification

    return verification


class FakeGitHub:
    async def repository_metadata(self, _full_name: str) -> dict[str, Any]:
        return {"default_branch": "main"}

    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", [{"path": "src/main.py", "type": "blob", "mode": "100644"}]

    async def text_file(
        self,
        _full_name: str,
        path: str,
        _revision: str,
        *,
        max_bytes: int,
    ) -> str | None:
        assert max_bytes == 1_000_000
        return "print('before')\n" if path == "src/main.py" else None


class FullRepositoryGitHub(FakeGitHub):
    sources = {"src/main.py": "print('before')\n", "README.md": "old readme\n"}

    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        entries = []
        for path, content in self.sources.items():
            raw = content.encode()
            blob_sha = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
            entries.append({"path": path, "type": "blob", "mode": "100644", "size": len(raw), "sha": blob_sha})
        return "tree-sha", entries

    async def text_file(
        self,
        _full_name: str,
        path: str,
        _revision: str,
        *,
        max_bytes: int,
    ) -> str | None:
        return self.sources.get(path)


class FullRepositoryDeliveryGitHub(FullRepositoryGitHub):
    def __init__(self) -> None:
        self.writes: list[str] = []
        self.committed_files: list[dict[str, str]] = []
        self.deleted_paths: list[str] = []
        self.delivery_session: Any | None = None

    async def __aenter__(self) -> FullRepositoryDeliveryGitHub:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def create_branch_at_commit(self, _repo: str, branch: str, sha: str) -> dict[str, str]:
        assert self.delivery_session is not None
        assert len(self.delivery_session.records) == 1
        assert self.delivery_session.records[0].evidence_snapshot
        self.writes.append("branch")
        return {"name": branch, "sha": sha}

    async def commit_files(self, _repo: str, branch: str, **kwargs: Any) -> dict[str, str]:
        self.writes.append("commit")
        self.committed_files = kwargs["files"]
        self.deleted_paths = kwargs["deleted_paths"]
        return {"branch": branch, "sha": "b" * 40}

    async def create_pull_request(self, _repo: str, **_kwargs: str) -> dict[str, Any]:
        self.writes.append("pull-request")
        return {"number": 12, "url": "https://github.com/octo/repo/pull/12", "draft": True}


class ExecutableSourceGitHub(FakeGitHub):
    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", [{"path": "src/main.py", "type": "blob", "mode": "100755"}]


class CollidingSourceTreeGitHub(FakeGitHub):
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", self.entries


class MissingSourceTreeGitHub(FakeGitHub):
    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", []


class UnsafeDeletionSourceGitHub(FakeGitHub):
    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", [{"path": "src/main.py", "type": "blob", "mode": "120000"}]


class OversizedDeletionSourceGitHub(FakeGitHub):
    async def text_file(
        self,
        _full_name: str,
        _path: str,
        _revision: str,
        *,
        max_bytes: int,
    ) -> str | None:
        assert max_bytes == 1_000_000
        return "x" * 500_001


class FakeDeliveryGitHub(FakeGitHub):
    def __init__(self) -> None:
        self.writes: list[str] = []

    async def __aenter__(self) -> FakeDeliveryGitHub:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def create_branch_at_commit(self, _repo: str, branch: str, sha: str) -> dict[str, str]:
        self.writes.append("branch")
        return {"name": branch, "sha": sha}

    async def commit_files(self, _repo: str, branch: str, **kwargs: Any) -> dict[str, str]:
        assert kwargs["expected_head_sha"] == "a" * 40
        assert kwargs["files"] == [{"path": "src/main.py", "content": "print('after')\n"}]
        assert kwargs["deleted_paths"] == []
        self.writes.append("commit")
        return {"branch": branch, "sha": "b" * 40}

    async def create_pull_request(self, _repo: str, **kwargs: str) -> dict[str, Any]:
        assert kwargs["head"] == "solvyn/task-12"
        self.writes.append("pull-request")
        return {"number": 12, "url": "https://github.com/octo/repo/pull/12", "draft": True}


class AmbiguousPullRequestGitHub(FakeDeliveryGitHub):
    async def create_pull_request(self, _repo: str, **_kwargs: str) -> dict[str, Any]:
        self.writes.append("pull-request")
        raise GitHubRepositoryError(504, "GitHub request timed out")


class DeletionOnlyDeliveryGitHub(FakeDeliveryGitHub):
    async def commit_files(self, _repo: str, branch: str, **kwargs: Any) -> dict[str, str]:
        assert kwargs["files"] == []
        assert kwargs["deleted_paths"] == ["src/main.py"]
        self.writes.append("commit")
        return {"branch": branch, "sha": "b" * 40}


class FakeDeliverySession:
    def __init__(self, existing: Any | None = None) -> None:
        self.records: list[Any] = []
        self.commits = 0
        self.existing = existing

    async def execute(self, _statement: Any) -> Any:
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, record: Any) -> None:
        if record.id is None:
            record.id = uuid.uuid4()
        self.records.append(record)

    async def commit(self) -> None:
        self.commits += 1
        now = datetime.now(UTC)
        for record in self.records:
            record.created_at = record.created_at or now
            record.updated_at = now

    async def refresh(self, _record: Any) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _delivery_fixture(tmp_path: Any) -> tuple[Any, ...]:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    workspace_record = SimpleNamespace(id=uuid.uuid4(), source_commit_sha="a" * 40)
    repository = SimpleNamespace(id=uuid.uuid4(), full_name="octo/repo", default_branch="main")
    return (
        SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4()),
        workspace_record,
        repository,
        SimpleNamespace(),
        ["src/main.py"],
        manager,
        workspace,
        "secret-token",
    )


@pytest.mark.asyncio
async def test_delivery_preview_is_bounded_and_digest_binds_workspace_contents(tmp_path: Any) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    manager.write_file(workspace, "README.md", b"new file\n")
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    first = await verification_api._build_delivery_preview(
        repository, record, ["src/main.py"], manager, workspace, FakeGitHub()  # type: ignore[arg-type]
    )
    second = await verification_api._build_delivery_preview(
        repository, record, ["src/main.py"], manager, workspace, FakeGitHub()  # type: ignore[arg-type]
    )

    assert first["can_deliver"] is True
    assert first["diff_digest"] == second["diff_digest"]
    assert len(first["workspace_digest"]) == 64
    assert first["workspace_digest"] == second["workspace_digest"]
    assert [item["path"] for item in first["files"]] == ["README.md", "src/main.py"]
    assert "+print('after')" in first["diffs"][1]["diff"]


@pytest.mark.asyncio
async def test_full_repository_delivery_compares_pinned_blobs_and_bounds_changes(tmp_path: Any) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    manager.write_file(workspace, "docs/new.md", b"new file\n")
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    preview = await verification_api._build_delivery_preview(
        repository, record, None, manager, workspace, FullRepositoryGitHub()  # type: ignore[arg-type]
    )

    assert preview["can_deliver"] is True
    assert [item["path"] for item in preview["files"]] == ["docs/new.md", "src/main.py"]
    assert preview["deleted_paths"] == ["README.md"]
    assert len(preview["diffs"]) == 3


@pytest.mark.asyncio
async def test_delivery_preview_represents_missing_selected_source_as_a_deletion(tmp_path: Any) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    preview = await verification_api._build_delivery_preview(
        repository, record, ["src/main.py"], manager, workspace, FakeGitHub()  # type: ignore[arg-type]
    )

    assert preview["unsupported_missing_source_paths"] == []
    assert preview["deleted_paths"] == ["src/main.py"]
    assert "-print('before')" in preview["diffs"][0]["diff"]
    assert preview["can_deliver"] is True


@pytest.mark.asyncio
async def test_delivery_preview_keeps_unknown_or_nonregular_deletions_blocked(tmp_path: Any) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    missing = await verification_api._build_delivery_preview(
        repository, record, ["src/main.py"], manager, workspace, MissingSourceTreeGitHub()  # type: ignore[arg-type]
    )
    unsafe = await verification_api._build_delivery_preview(
        repository, record, ["src/main.py"], manager, workspace, UnsafeDeletionSourceGitHub()  # type: ignore[arg-type]
    )

    assert missing["unsupported_missing_source_paths"] == ["src/main.py"]
    assert unsafe["unsupported_missing_source_paths"] == ["src/main.py"]
    assert missing["can_deliver"] is False
    assert unsafe["can_deliver"] is False


@pytest.mark.asyncio
async def test_delivery_preview_applies_changed_content_byte_limit_to_deletions(tmp_path: Any) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    with pytest.raises(HTTPException) as error:
        await verification_api._build_delivery_preview(
            repository,
            record,
            ["src/main.py"],
            manager,
            workspace,
            OversizedDeletionSourceGitHub(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_delivery_preview_blocks_changed_executable_source_files(tmp_path: Any) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    with pytest.raises(HTTPException) as error:
        await verification_api._build_delivery_preview(
            repository,
            record,
            ["src/main.py"],
            manager,
            workspace,
            ExecutableSourceGitHub(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert "executable mode" in error.value.detail


@pytest.mark.parametrize(
    ("added_path", "source_entries"),
    [
        (
            "docs",
            [
                {"path": "docs", "type": "tree"},
                {"path": "docs/guide.md", "type": "blob"},
                {"path": "src/main.py", "type": "blob", "mode": "100644"},
            ],
        ),
        (
            "docs/new.md",
            [
                {"path": "docs", "type": "blob", "mode": "100644"},
                {"path": "src/main.py", "type": "blob", "mode": "100644"},
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_delivery_preview_rejects_source_file_directory_prefix_collisions(
    tmp_path: Any, added_path: str, source_entries: list[dict[str, Any]]
) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    manager.write_file(workspace, added_path, b"new workspace file\n")
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    with pytest.raises(HTTPException) as error:
        await verification_api._build_delivery_preview(
            repository,
            record,
            ["src/main.py"],
            manager,
            workspace,
            CollidingSourceTreeGitHub(source_entries),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert "conflicts" in error.value.detail


def test_delivery_evidence_snapshot_has_a_separate_storage_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    verification_api = _verification_api()
    monkeypatch.setattr(verification_api, "_MAX_DELIVERY_EVIDENCE_BYTES", 1)
    with pytest.raises(HTTPException) as error:
        verification_api._delivery_evidence_snapshot(
            {"files": [], "deleted_paths": [], "diffs": []}
        )
    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_delivery_rejects_stale_preview_before_creating_remote_branch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    github = FakeDeliveryGitHub()

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    payload = verification_api.TaskDeliveryRequest(
        expected_diff_digest="0" * 64,
        branch_name="solvyn/task-12",
        commit_message="Update source",
        pull_request_title="Update source",
    )

    with pytest.raises(HTTPException) as error:
        await verification_api.deliver_task_workspace(
            execution_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            payload=payload,
            db=object(),  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert github.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("task_status", ["pending", "review_required", "failed"])
async def test_delivery_requires_completion_gate_before_remote_writes(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    task_status: str,
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    github = FakeDeliveryGitHub()
    db = FakeDeliverySession()
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], context[4], context[5], context[6], github  # type: ignore[arg-type]
    )
    completion_refresh = AsyncMock(return_value=task_status)

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", completion_refresh)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)

    with pytest.raises(HTTPException) as error:
        await verification_api.deliver_task_workspace(
            execution_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            payload=verification_api.TaskDeliveryRequest(
                expected_diff_digest=preview["diff_digest"],
                branch_name="solvyn/task-12",
                commit_message="Update source",
                pull_request_title="Update source",
            ),
            db=db,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert github.writes == []
    # A review_required result (including a handoff with no required check)
    # must stop before any branch, commit, or pull request write.
    completion_refresh.assert_awaited_once_with(
        db,
        context[0],
        expected_workspace_digest=context[5].fingerprint(context[6]),
        commit=False,
    )
    assert db.commits == 1


@pytest.mark.asyncio
async def test_gate_mutation_is_rejected_while_delivery_needs_recovery() -> None:
    verification_api = _verification_api()
    task = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    active_delivery_id = uuid.uuid4()
    db = AsyncMock()
    db.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: task),
        SimpleNamespace(scalar_one_or_none=lambda: active_delivery_id),
    ]

    with pytest.raises(HTTPException) as error:
        await verification_api._lock_task_for_gate_mutation(db, task)  # type: ignore[arg-type]

    assert error.value.status_code == 409
    assert "delivery is in progress" in error.value.detail


@pytest.mark.asyncio
async def test_delivery_creates_pinned_branch_commit_and_draft_pr(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    github = FakeDeliveryGitHub()
    db = FakeDeliverySession()
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], context[4], context[5], context[6], FakeGitHub()  # type: ignore[arg-type]
    )

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    result = await verification_api.deliver_task_workspace(
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        payload=verification_api.TaskDeliveryRequest(
            expected_diff_digest=preview["diff_digest"],
            branch_name="solvyn/task-12",
            commit_message="Update source",
            pull_request_title="Update source",
        ),
        db=db,  # type: ignore[arg-type]
    )

    assert github.writes == ["branch", "commit", "pull-request"]
    assert result["commit_sha"] == "b" * 40
    assert result["pull_request"]["draft"] is True
    assert result["merged"] is False
    assert result["replayed"] is False
    assert len(db.records) == 1
    assert db.records[0].status == "pull_request_open"
    assert db.records[0].pull_request_number == 12
    saved_evidence = json.loads(db.records[0].evidence_snapshot)
    assert saved_evidence["version"] == 1
    assert saved_evidence["files"] == preview["files"]
    assert saved_evidence["deleted_paths"] == preview["deleted_paths"]
    assert saved_evidence["diffs"] == preview["diffs"]
    context[5].write_file(context[6], "src/main.py", b"print('later')\n")
    assert json.loads(db.records[0].evidence_snapshot) == saved_evidence


@pytest.mark.asyncio
async def test_full_repository_delivery_records_reviewable_file_and_deletion_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    manager.write_file(workspace, "docs/new.md", b"new file\n")
    task_id, tenant_id, workspace_id, repository_id = (uuid.uuid4() for _ in range(4))
    context = (
        SimpleNamespace(id=task_id, tenant_id=tenant_id),
        SimpleNamespace(id=workspace_id, source_commit_sha="a" * 40),
        SimpleNamespace(id=repository_id, full_name="octo/repo"),
        SimpleNamespace(id=uuid.uuid4()),
        None,
        manager,
        workspace,
        "github-token",
    )
    github = FullRepositoryDeliveryGitHub()
    db = FakeDeliverySession()
    github.delivery_session = db
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], None, manager, workspace, github  # type: ignore[arg-type]
    )

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    result = await verification_api.deliver_task_workspace(
        execution_id=uuid.uuid4(),
        task_id=task_id,
        payload=verification_api.TaskDeliveryRequest(
            expected_diff_digest=preview["diff_digest"],
            branch_name="solvyn/task-13",
            commit_message="Update full repository",
            pull_request_title="Update full repository",
        ),
        db=db,  # type: ignore[arg-type]
    )

    assert github.writes == ["branch", "commit", "pull-request"]
    assert github.committed_files == preview["files"]
    assert github.deleted_paths == preview["deleted_paths"] == ["README.md"]
    assert result["pull_request"]["draft"] is True
    evidence = json.loads(db.records[0].evidence_snapshot)
    assert evidence["files"] == preview["files"]
    assert evidence["deleted_paths"] == ["README.md"]
    assert evidence["diffs"] == preview["diffs"]


@pytest.mark.asyncio
async def test_deletion_only_delivery_creates_a_reviewable_draft_pr(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    context[5].delete_file(context[6], "src/main.py")
    github = DeletionOnlyDeliveryGitHub()
    db = FakeDeliverySession()
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], context[4], context[5], context[6], FakeGitHub()  # type: ignore[arg-type]
    )

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    result = await verification_api.deliver_task_workspace(
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        payload=verification_api.TaskDeliveryRequest(
            expected_diff_digest=preview["diff_digest"],
            branch_name="solvyn/task-12",
            commit_message="Remove unused source",
            pull_request_title="Remove unused source",
        ),
        db=db,  # type: ignore[arg-type]
    )

    assert github.writes == ["branch", "commit", "pull-request"]
    assert preview["deleted_paths"] == ["src/main.py"]
    assert result["pull_request"]["draft"] is True
    assert result["merged"] is False


@pytest.mark.asyncio
async def test_ambiguous_pull_request_failure_reports_unknown_outcome(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    github = AmbiguousPullRequestGitHub()
    db = FakeDeliverySession()
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], context[4], context[5], context[6], FakeGitHub()  # type: ignore[arg-type]
    )

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    with pytest.raises(HTTPException) as error:
        await verification_api.deliver_task_workspace(
            execution_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            payload=verification_api.TaskDeliveryRequest(
                expected_diff_digest=preview["diff_digest"],
                branch_name="solvyn/task-12",
                commit_message="Update source",
                pull_request_title="Update source",
            ),
            db=db,  # type: ignore[arg-type]
        )

    assert github.writes == ["branch", "commit", "pull-request"]
    assert error.value.status_code == 504
    assert error.value.detail["pull_request_created"] == "unknown"
    assert error.value.detail["commit_sha"] == "b" * 40
    assert len(db.records) == 1
    assert db.records[0].status == "unknown"
    assert db.records[0].commit_sha == "b" * 40
    assert json.loads(db.records[0].evidence_snapshot)["diffs"] == preview["diffs"]


@pytest.mark.asyncio
async def test_delivery_replays_persisted_pull_request_without_github_writes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    github = FakeDeliveryGitHub()
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], context[4], context[5], context[6], FakeGitHub()  # type: ignore[arg-type]
    )
    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        status="pull_request_open",
        pull_request_url="https://github.com/octo/repo/pull/12",
        pull_request_title="Original saved title",
        pull_request_number=12,
        commit_sha="b" * 40,
        branch_name="solvyn/task-12",
        diff_digest=preview["diff_digest"],
    )
    db = FakeDeliverySession(existing=delivery)

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    result = await verification_api.deliver_task_workspace(
        execution_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        payload=verification_api.TaskDeliveryRequest(
            expected_diff_digest=preview["diff_digest"],
            branch_name="solvyn/task-12",
            commit_message="Update source",
            pull_request_title="Update source",
        ),
        db=db,  # type: ignore[arg-type]
    )

    assert result["replayed"] is True
    assert result["pull_request"]["url"] == delivery.pull_request_url
    assert result["pull_request"]["title"] == delivery.pull_request_title
    assert result["pull_request"]["state"] == "recorded"
    assert result["pull_request"]["draft"] is None
    assert github.writes == []
    assert db.records == []


@pytest.mark.asyncio
async def test_delivery_blocks_retry_for_unresolved_persisted_attempt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    verification_api = _verification_api()
    context = _delivery_fixture(tmp_path)
    monkeypatch.setattr(verification_api, "_refresh_task_delivery_completion", AsyncMock(return_value="completed"))
    github = FakeDeliveryGitHub()
    preview = await verification_api._build_delivery_preview(
        context[2], context[1], context[4], context[5], context[6], FakeGitHub()  # type: ignore[arg-type]
    )
    delivery = SimpleNamespace(
        id=uuid.uuid4(),
        status="unknown",
        diff_digest=preview["diff_digest"],
        base_branch="main",
        branch_name="solvyn/task-12",
        branch_sha="a" * 40,
        commit_sha="b" * 40,
        pull_request_title=None,
        pull_request_number=None,
        pull_request_url=None,
        error="GitHub request timed out",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db = FakeDeliverySession(existing=delivery)

    async def load_context(*_args: Any) -> tuple[Any, ...]:
        return context

    monkeypatch.setattr(verification_api, "_task_delivery_context", load_context)
    monkeypatch.setattr(verification_api, "GitHubClient", lambda **_kwargs: github)
    with pytest.raises(HTTPException) as error:
        await verification_api.deliver_task_workspace(
            execution_id=uuid.uuid4(),
            task_id=uuid.uuid4(),
            payload=verification_api.TaskDeliveryRequest(
                expected_diff_digest=preview["diff_digest"],
                branch_name="solvyn/task-12",
                commit_message="Update source",
                pull_request_title="Update source",
            ),
            db=db,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 409
    assert error.value.detail["delivery"]["status"] == "unknown"
    assert github.writes == []


@pytest.mark.asyncio
async def test_delivery_evidence_is_task_scoped_and_legacy_rows_are_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    verification_api = _verification_api()
    execution_id, task_id, tenant_id, delivery_id, repository_id, project_id = (uuid.uuid4() for _ in range(6))
    task = SimpleNamespace(id=task_id, tenant_id=tenant_id)
    delivery = SimpleNamespace(
        id=delivery_id,
        tenant_id=tenant_id,
        task_id=task_id,
        repository_id=repository_id,
        evidence_snapshot=json.dumps({"version": 1, "files": [], "deleted_paths": [], "diffs": []}),
    )
    handoff = SimpleNamespace(status="completed", project_id=project_id)
    repository, project = SimpleNamespace(id=repository_id), SimpleNamespace(id=project_id)

    async def owned_task(*_args: Any) -> Any:
        return task

    class Session:
        def __init__(self, delivery_record: Any, results: list[Any]) -> None:
            self.delivery_record = delivery_record
            self.results = iter(results)

        async def execute(self, statement: Any) -> Any:
            params = statement.compile().params
            assert task_id in params.values() and tenant_id in params.values()
            if delivery_id in params.values():
                result = self.delivery_record
            else:
                result = next(self.results)
            return SimpleNamespace(scalar_one_or_none=lambda: result)

    async def authorized_repository(repository_ref: Any, _db: Any, *, roles: Any) -> tuple[Any, Any]:
        assert repository_ref == repository_id
        assert roles == verification_api.OWNER_ADMIN
        return repository, project

    monkeypatch.setattr(verification_api, "_owned_task", owned_task)
    monkeypatch.setattr(verification_api, "_load_authorized_repository", authorized_repository)
    response = await verification_api.get_task_delivery_evidence(
        execution_id, task_id, delivery_id, Session(delivery, [handoff])  # type: ignore[arg-type]
    )
    assert response["evidence"] == json.loads(delivery.evidence_snapshot)

    legacy = SimpleNamespace(id=delivery_id, tenant_id=tenant_id, task_id=task_id, evidence_snapshot=None)
    legacy.repository_id = repository_id
    legacy_response = await verification_api.get_task_delivery_evidence(
        execution_id, task_id, delivery_id, Session(legacy, [handoff])  # type: ignore[arg-type]
    )
    assert legacy_response["evidence"] is None

    async def denied_repository(*_args: Any, **_kwargs: Any) -> Any:
        raise HTTPException(status_code=403, detail="Owner or admin role required")

    monkeypatch.setattr(verification_api, "_load_authorized_repository", denied_repository)
    with pytest.raises(HTTPException) as error:
        await verification_api.get_task_delivery_evidence(
            execution_id, task_id, delivery_id, Session(delivery, [handoff])  # type: ignore[arg-type]
        )
    assert error.value.status_code == 403
