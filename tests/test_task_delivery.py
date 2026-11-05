"""Bounded diff previews for repository-backed task workspaces."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from solvyn.repositories.github import GitHubRepositoryError
from solvyn.sandbox.workspaces import WorkspaceManager


class _LazyVerificationApi:
    def __getattr__(self, name: str) -> Any:
        from solvyn.api.v1 import verification

        return getattr(verification, name)


verification_api = _LazyVerificationApi()


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


class ExecutableSourceGitHub(FakeGitHub):
    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", [{"path": "src/main.py", "type": "blob", "mode": "100755"}]


class CollidingSourceTreeGitHub(FakeGitHub):
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "tree-sha", self.entries


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


def _delivery_fixture(tmp_path: Any) -> tuple[Any, ...]:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"print('after')\n")
    workspace_record = SimpleNamespace(id=workspace.id, source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo", default_branch="main")
    return (
        SimpleNamespace(id="task-id"),
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
    assert [item["path"] for item in first["files"]] == ["README.md", "src/main.py"]
    assert "+print('after')" in first["diffs"][1]["diff"]


@pytest.mark.asyncio
async def test_delivery_preview_blocks_missing_source_files_instead_of_implying_deletion(tmp_path: Any) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    record = SimpleNamespace(source_commit_sha="a" * 40)
    repository = SimpleNamespace(id="repo-id", full_name="octo/repo")

    preview = await verification_api._build_delivery_preview(
        repository, record, ["src/main.py"], manager, workspace, FakeGitHub()  # type: ignore[arg-type]
    )

    assert preview["unsupported_missing_source_paths"] == ["src/main.py"]
    assert preview["can_deliver"] is False


@pytest.mark.asyncio
async def test_delivery_preview_blocks_changed_executable_source_files(tmp_path: Any) -> None:
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


@pytest.mark.asyncio
async def test_delivery_rejects_stale_preview_before_creating_remote_branch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _delivery_fixture(tmp_path)
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
async def test_delivery_creates_pinned_branch_commit_and_draft_pr(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _delivery_fixture(tmp_path)
    github = FakeDeliveryGitHub()
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
        db=object(),  # type: ignore[arg-type]
    )

    assert github.writes == ["branch", "commit", "pull-request"]
    assert result["commit_sha"] == "b" * 40
    assert result["pull_request"]["draft"] is True
    assert result["merged"] is False


@pytest.mark.asyncio
async def test_ambiguous_pull_request_failure_reports_unknown_outcome(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _delivery_fixture(tmp_path)
    github = AmbiguousPullRequestGitHub()
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
            db=object(),  # type: ignore[arg-type]
        )

    assert github.writes == ["branch", "commit", "pull-request"]
    assert error.value.status_code == 504
    assert error.value.detail["pull_request_created"] == "unknown"
    assert error.value.detail["commit_sha"] == "b" * 40
