"""Tests for sandbox workspace boundaries and Docker isolation policy."""

from __future__ import annotations

import io
import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

from solvyn.sandbox import DockerSandboxRunner, ManagedWorkspace, SandboxLimits, WorkspaceManager
from solvyn.sandbox.runtime import SandboxRuntimeError
from solvyn.sandbox.workspaces import WorkspaceError


def test_workspace_paths_and_size_are_confined(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces", max_bytes=8)
    workspace = manager.create()

    manager.write_file(workspace, "src/main.py", b"1234")
    assert manager.read_file(workspace, "src/main.py") == b"1234"
    assert manager.list_files(workspace) == ["src/main.py"]

    with pytest.raises(WorkspaceError):
        manager.write_file(workspace, "../outside.txt", b"no")
    with pytest.raises(WorkspaceError):
        manager.write_file(workspace, "C:/outside.txt", b"no")
    with pytest.raises(WorkspaceError):
        manager.write_file(workspace, "NUL", b"no")
    with pytest.raises(WorkspaceError):
        manager.write_file(workspace, "other.txt", b"12345")
    manager.write_file(workspace, "src/main.py", b"12345678")
    assert manager.read_file(workspace, "src/main.py") == b"12345678"
    with pytest.raises(WorkspaceError):
        manager.write_file(workspace, "src/main.py", b"123456789")
    with pytest.raises(WorkspaceError):
        manager.read_file(workspace, "src/main.py", max_bytes=2)

    manager.delete(workspace.id)
    with pytest.raises(WorkspaceError):
        manager.get(workspace.id)


def test_workspace_fingerprint_tracks_paths_and_content(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"before")
    original = manager.fingerprint(workspace)
    assert original == manager.fingerprint(workspace)
    manager.write_file(workspace, "src/main.py", b"after")
    changed_content = manager.fingerprint(workspace)
    assert changed_content != original
    manager.write_file(workspace, "src/renamed.py", b"after")
    assert manager.fingerprint(workspace) != changed_content


def test_workspace_rejects_symlink_traversal(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (workspace.path / "escape").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(WorkspaceError):
        manager.write_file(workspace, "escape/owned.txt", b"no")
    assert not (outside / "owned.txt").exists()


def test_workspace_rejects_forged_handles_and_special_files(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    forged = ManagedWorkspace(workspace.id, tmp_path)
    with pytest.raises(WorkspaceError, match="handle is invalid"):
        manager.validate_tree(forged)

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation is unavailable")
    os.mkfifo(workspace.path / "pipe")
    with pytest.raises(WorkspaceError, match="unsupported filesystem entry"):
        manager.validate_tree(workspace)


def test_sandbox_limits_reject_unsafe_values() -> None:
    with pytest.raises(ValueError):
        SandboxLimits(timeout_seconds=301)
    with pytest.raises(ValueError):
        SandboxLimits(memory_mb=64)
    with pytest.raises(ValueError):
        SandboxLimits(pids=10000)


@pytest.mark.asyncio
async def test_runner_applies_container_policy_and_returns_workspace_changes(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "src/main.py", b"before")
    fake_docker, calls_file = _fake_docker(tmp_path)
    runner = DockerSandboxRunner(
        manager,
        docker_command=[sys.executable, str(fake_docker)],
    )

    result = await runner.run(workspace, ["python", "-c", "print('safe')"])

    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    create = calls[0]
    assert result.exit_code == 0
    assert result.stdout == "runner output\n"
    assert manager.read_file(workspace, "src/main.py") == b"after"
    assert "--network" in create and "none" in create
    assert "--read-only" in create
    assert "--cap-drop" in create and "ALL" in create
    assert "--memory" in create and "512m" in create
    assert "--cpus" in create and "1.0" in create
    assert "--pids-limit" in create and "128" in create
    assert "--tmpfs" in create and "/workspace:rw,nosuid,nodev,size=67108864,nr_inodes=10000" in create
    assert calls[3] == ["exec", "--user", "0", "a" * 64, "chmod", "-R", "a+rwX", "/workspace"]
    assert calls[4] == ["exec", "a" * 64, "python", "-c", "print('safe')"]
    assert calls[-1][:2] == ["rm", "--force"]
    assert calls[-1][2].startswith("solvyn-sandbox-")


@pytest.mark.asyncio
async def test_runner_times_out_and_still_cleans_up(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    fake_docker, calls_file = _fake_docker(tmp_path, slow_command=True)
    runner = DockerSandboxRunner(
        manager,
        docker_command=[sys.executable, str(fake_docker)],
        limits=SandboxLimits(timeout_seconds=1),
    )

    result = await runner.run(workspace, ["slow"])

    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert result.timed_out is True
    assert result.exit_code is None
    assert any(call[0] == "kill" for call in calls)
    assert calls[-1][:2] == ["rm", "--force"]


@pytest.mark.asyncio
async def test_runner_surfaces_container_cleanup_failure(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    fake_docker, _ = _fake_docker(tmp_path, fail_remove=True)
    runner = DockerSandboxRunner(manager, docker_command=[sys.executable, str(fake_docker)])

    with pytest.raises(SandboxRuntimeError, match="failed to clean up"):
        await runner.run(workspace, ["true"])


@pytest.mark.asyncio
async def test_runner_attempts_cleanup_when_container_creation_times_out(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    fake_docker, calls_file = _fake_docker(tmp_path, slow_create=True)
    runner = DockerSandboxRunner(
        manager,
        docker_command=[sys.executable, str(fake_docker)],
        cli_timeout_seconds=1,
    )

    with pytest.raises(SandboxRuntimeError, match="failed to create"):
        await runner.run(workspace, ["true"])

    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert calls[-1][:2] == ["rm", "--force"]


def test_workspace_archive_rejects_path_traversal(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w") as archive:
        member = tarfile.TarInfo("../escaped.txt")
        member.size = 2
        archive.addfile(member, io.BytesIO(b"no"))
    archive_buffer.seek(0)
    runner = DockerSandboxRunner(manager)

    with pytest.raises(SandboxRuntimeError):
        runner._extract_workspace_archive(archive_buffer, workspace, len(archive_buffer.getvalue()))
    assert not (tmp_path / "escaped.txt").exists()


@pytest.mark.skipif(os.getenv("SOLVYN_DOCKER_INTEGRATION") != "1", reason="requires a running Docker Engine and built sandbox image")
@pytest.mark.asyncio
async def test_docker_integration_runs_a_workspace_change(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "input.txt", b"hello")
    runner = DockerSandboxRunner(manager)

    result = await runner.run(workspace, ["python", "-c", "from pathlib import Path; p=Path('input.txt'); p.write_text(p.read_text().upper())"])

    assert result.exit_code == 0
    assert manager.read_file(workspace, "input.txt") == b"HELLO"


def _fake_docker(
    tmp_path: Path,
    *,
    slow_command: bool = False,
    slow_create: bool = False,
    fail_remove: bool = False,
) -> tuple[Path, Path]:
    script_path = tmp_path / "fake_docker.py"
    calls_file = tmp_path / "docker_calls.jsonl"
    calls_literal = repr(str(calls_file))
    slow = "time.sleep(3)" if slow_command else "pass"
    slow_container_create = "time.sleep(3)" if slow_create else "pass"
    fail_rm = "sys.exit(1)" if fail_remove else "pass"
    script_path.write_text(
        "import io, json, pathlib, sys, tarfile, time\n"
        f"calls = pathlib.Path({calls_literal})\n"
        "args = sys.argv[1:]\n"
        "with calls.open('a', encoding='utf-8') as stream: stream.write(json.dumps(args) + '\\n')\n"
        "if args[0] == 'create':\n"
        f"    {slow_container_create}\n"
        "    print('a' * 64)\n"
        "elif args[0] == 'rm':\n"
        f"    {fail_rm}\n"
        "elif args[0] == 'exec':\n"
        "    if args[-1] == 'slow':\n"
        f"        {slow}\n"
        "    print('runner output')\n"
        "elif args[0] == 'cp' and args[-1] == '-':\n"
        "    data = io.BytesIO()\n"
        "    with tarfile.open(fileobj=data, mode='w') as archive:\n"
        "        payload = b'after'\n"
        "        item = tarfile.TarInfo('src/main.py')\n"
        "        item.size = len(payload)\n"
        "        archive.addfile(item, io.BytesIO(payload))\n"
        "    sys.stdout.buffer.write(data.getvalue())\n",
        encoding="utf-8",
    )
    return script_path, calls_file
