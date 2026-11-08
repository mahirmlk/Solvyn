"""Bounds and credential handling for full repository workspaces."""

from __future__ import annotations

import asyncio
import base64
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from solvyn.repositories.worktree import (
    RepositoryWorktreeError,
    _checkout_raw_blobs,
    _git_environment,
    _materialize,
    validate_repository_tree,
)
from solvyn.sandbox.workspaces import WorkspaceManager


class _WorkspaceSession:
    def __init__(self, handoff: Any) -> None:
        self.handoff = handoff
        self.calls = 0
        self.added: list[Any] = []
        self.rollbacks = 0

    async def execute(self, _statement: Any) -> Any:
        self.calls += 1
        value = None if self.calls == 1 else self.handoff
        return SimpleNamespace(scalar_one_or_none=lambda: value)

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        self.rollbacks += 1


class _WorkspaceGitHub:
    async def __aenter__(self) -> _WorkspaceGitHub:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def repository_metadata(self, _full_name: str) -> dict[str, Any]:
        return {"default_branch": "main", "private": False}

    async def branch_commit(self, _full_name: str, _branch: str) -> str:
        return "a" * 40

    async def repository_tree(self, _full_name: str, _revision: str) -> tuple[str, list[dict[str, Any]]]:
        return "c" * 40, [
            {"path": "README.md", "type": "blob", "mode": "100644", "size": 6, "sha": "b" * 40}
        ]


def test_repository_tree_accepts_bounded_regular_files() -> None:
    entries = [
        {"path": "src", "type": "tree", "mode": "040000"},
        {"path": "src/main.py", "type": "blob", "mode": "100644", "size": 16, "sha": "a" * 40},
        {"path": "scripts/run.sh", "type": "blob", "mode": "100755", "size": 32, "sha": "b" * 40},
    ]

    assert validate_repository_tree(entries) == 48


@pytest.mark.parametrize(
    "entry",
    [
        {"path": "../secret", "type": "blob", "mode": "100644", "size": 1},
        {"path": ".git/config", "type": "blob", "mode": "100644", "size": 1},
        {"path": "shortcut", "type": "blob", "mode": "120000", "size": 1},
        {"path": "vendor", "type": "commit", "mode": "160000", "size": 0},
        {"path": "src/main.py", "type": "blob", "mode": "100644"},
    ],
)
def test_repository_tree_rejects_unsafe_or_unsupported_entries(entry: dict[str, object]) -> None:
    with pytest.raises(RepositoryWorktreeError):
        validate_repository_tree([entry])


def test_repository_tree_rejects_case_insensitive_path_collisions() -> None:
    entries = [
        {"path": "Src", "type": "tree", "mode": "040000"},
        {"path": "src/main.py", "type": "blob", "mode": "100644", "size": 1},
    ]

    with pytest.raises(RepositoryWorktreeError, match="case-insensitive"):
        validate_repository_tree(entries)


def test_git_credential_is_transient_and_not_plaintext() -> None:
    token = "secret-token-for-test"
    environment = _git_environment(token)

    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert token not in environment["GIT_CONFIG_VALUE_0"]
    assert base64.b64decode(environment["GIT_CONFIG_VALUE_0"].split("basic ", 1)[1]).decode() == f"x-access-token:{token}"


def test_public_git_environment_does_not_inherit_git_auth_entries() -> None:
    environment = _git_environment(None)

    assert environment["GIT_CONFIG_COUNT"] == "0"


@pytest.mark.asyncio
async def test_worktree_transport_keeps_credentials_out_of_remote_and_checks_pinned_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import solvyn.repositories.worktree as worktree

    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    async def fake_run_git(
        _path: Path, arguments: list[str], environment: dict[str, str], *, capture: bool = False
    ) -> str:
        commands.append(arguments)
        environments.append(environment)
        if capture and arguments == ["rev-parse", "FETCH_HEAD"]:
            return "a" * 40
        if capture and arguments == ["rev-parse", f"{'a' * 40}^{{tree}}"]:
            return "c" * 40
        return ""

    async def fake_checkout_raw_blobs(_path: Path, _tree: list[dict[str, Any]], _environment: dict[str, str]) -> None:
        return None

    monkeypatch.setattr(worktree, "_run_git", fake_run_git)
    monkeypatch.setattr(worktree, "_checkout_raw_blobs", fake_checkout_raw_blobs)
    token = "private-token"
    tree = [{"path": "README.md", "type": "blob", "mode": "100644", "size": 6, "sha": "b" * 40}]
    await _materialize(tmp_path, "octo/repo", "main", "a" * 40, "c" * 40, token, tree)

    joined = " ".join(" ".join(command) for command in commands)
    assert "https://github.com/octo/repo.git" in joined
    assert token not in joined
    assert "checkout-index" not in joined
    assert "checkout" not in joined
    assert ["rev-parse", "FETCH_HEAD"] in commands
    assert ["rev-parse", f"{'a' * 40}^{{tree}}"] in commands
    assert ["update-ref", "HEAD", "a" * 40] in commands
    assert environments[0]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert token not in environments[0].get("GIT_CONFIG_VALUE_0", "")
    assert token not in environments[-1].get("GIT_CONFIG_VALUE_0", "")
    assert token not in _git_environment(token)["GIT_CONFIG_VALUE_0"]


def test_raw_git_blob_materialization_ignores_gitattributes(tmp_path: Path) -> None:
    def git(*arguments: str) -> None:
        subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
        )

    git("init", "--quiet")
    git("config", "user.name", "Solvyn Test")
    git("config", "user.email", "solvyn-test@example.invalid")
    git("config", "core.autocrlf", "false")
    (tmp_path / ".gitattributes").write_text("*.txt text eol=crlf\nident.txt ident\n", encoding="utf-8")
    (tmp_path / "sample.txt").write_bytes(b"line one\n")
    (tmp_path / "ident.txt").write_bytes(b"$Id$\n")
    git("add", ".")
    git("commit", "--quiet", "-m", "fixture")

    attributes = subprocess.run(
        ["git", "check-attr", "--all", "--", "sample.txt", "ident.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    ).stdout
    assert "sample.txt: text: set" in attributes
    assert "sample.txt: eol: crlf" in attributes
    assert "ident.txt: ident: set" in attributes

    (tmp_path / "sample.txt").unlink()
    (tmp_path / "ident.txt").unlink()
    paths = [".gitattributes", "ident.txt", "sample.txt"]
    entries = []
    for path in paths:
        sha = subprocess.run(
            ["git", "rev-parse", f"HEAD:{path}"], cwd=tmp_path, check=True, capture_output=True, text=True, shell=False
        ).stdout.strip()
        size = int(
            subprocess.run(
                ["git", "cat-file", "-s", sha], cwd=tmp_path, check=True, capture_output=True, text=True, shell=False
            ).stdout.strip()
        )
        entries.append({"path": path, "type": "blob", "mode": "100644", "sha": sha, "size": size})
    asyncio.run(_checkout_raw_blobs(tmp_path, entries, _git_environment(None)))
    assert (tmp_path / "sample.txt").read_bytes() == b"line one\n"
    assert (tmp_path / "ident.txt").read_bytes() == b"$Id$\n"
    git("diff", "--quiet", "HEAD")


@pytest.mark.asyncio
async def test_full_repository_workspace_persists_marker_and_cleans_failed_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from solvyn.api.v1 import verification as verification_api

    manager = WorkspaceManager(tmp_path / "workspaces")
    task = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        status="running",
        completed_at=None,
        acceptance_criteria_defined=False,
    )
    project_id = uuid.uuid4()
    repository = SimpleNamespace(id=uuid.uuid4(), full_name="octo/repo", provider="github")
    project = SimpleNamespace(id=project_id, organization_id=uuid.uuid4())
    handoff = SimpleNamespace(status="completed", project_id=project_id)
    db = _WorkspaceSession(handoff)
    monkeypatch.setattr(verification_api, "_owned_task", lambda *_args: _return(task))
    monkeypatch.setattr(verification_api, "_workspace_manager", lambda: manager)
    async def no_completion_gates(_db: Any, active_task: Any) -> str:
        return active_task.status

    monkeypatch.setattr(verification_api, "refresh_task_completion_state", no_completion_gates)
    monkeypatch.setattr(verification_api, "_load_authorized_repository", lambda *_args: _return((repository, project)))
    monkeypatch.setattr(
        verification_api, "get_authorized_repository_provider", lambda *_args: _return(_WorkspaceGitHub())
    )

    async def successful_checkout(path: Path, **_kwargs: Any) -> None:
        (path / "README.md").write_text("source", encoding="utf-8")

    monkeypatch.setattr(verification_api, "materialize_repository_worktree", successful_checkout)
    response = await verification_api.create_task_workspace(
        uuid.uuid4(),
        task.id,
        verification_api.TaskWorkspaceCreate(repository_id=repository.id, full_repository=True),
        db,  # type: ignore[arg-type]
    )

    assert response["workspace_mode"] == "full_repository"
    assert response["source_paths"] is None
    assert db.added[0].source_paths is None
    assert db.added[0].source_commit_sha == "a" * 40

    db = _WorkspaceSession(handoff)

    async def failed_checkout(path: Path, **_kwargs: Any) -> None:
        (path / ".git").mkdir()
        (path / ".git" / "partial-pack").write_bytes(b"partial")
        raise RepositoryWorktreeError("checkout failed")

    monkeypatch.setattr(verification_api, "materialize_repository_worktree", failed_checkout)
    with pytest.raises(HTTPException) as error:
        await verification_api.create_task_workspace(
            uuid.uuid4(),
            task.id,
            verification_api.TaskWorkspaceCreate(repository_id=repository.id, full_repository=True),
            db,  # type: ignore[arg-type]
        )

    assert error.value.status_code == 422
    assert db.rollbacks == 1
    assert len(list(manager.root.iterdir())) == 1


async def _return(value: Any) -> Any:
    return value
