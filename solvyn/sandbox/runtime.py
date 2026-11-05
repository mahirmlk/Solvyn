"""Ephemeral Docker execution with a restrictive, fixed container policy.

The Docker CLI is used only by this trusted supervisor. No daemon socket,
credentials, caller environment, or network is mounted into the untrusted job.
"""

from __future__ import annotations

import asyncio
import os
import re
import tarfile
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from solvyn.sandbox.workspaces import ManagedWorkspace, WorkspaceError, WorkspaceManager, safe_relative_path

_CONTAINER_ID = re.compile(r"^[a-f0-9]{12,64}$")


class SandboxRuntimeError(RuntimeError):
    """The isolated runtime could not safely start or complete a job."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 120.0
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    workspace_bytes: int = 64 * 1024 * 1024
    tmp_bytes: int = 16 * 1024 * 1024
    output_bytes: int = 128 * 1024

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be between 1 and 300")
        if not 128 <= self.memory_mb <= 4096:
            raise ValueError("memory_mb must be between 128 and 4096")
        if not 0.1 <= self.cpus <= 4:
            raise ValueError("cpus must be between 0.1 and 4")
        if not 16 <= self.pids <= 256:
            raise ValueError("pids must be between 16 and 256")
        if not 1024 * 1024 <= self.workspace_bytes <= 128 * 1024 * 1024:
            raise ValueError("workspace_bytes must be between 1 and 128 MiB")
        if not 1024 * 1024 <= self.tmp_bytes <= 64 * 1024 * 1024:
            raise ValueError("tmp_bytes must be between 1 and 64 MiB")
        if not 1024 <= self.output_bytes <= 1024 * 1024:
            raise ValueError("output_bytes must be between 1 KiB and 1 MiB")


@dataclass(frozen=True)
class SandboxResult:
    run_id: uuid.UUID
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    output_truncated: bool
    duration_seconds: float


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    timed_out: bool = False


class DockerSandboxRunner:
    """Run argv inside a one-shot, networkless container and return safe edits."""

    def __init__(
        self,
        workspaces: WorkspaceManager,
        *,
        image: str = "solvyn-sandbox:local",
        docker_binary: str = "docker",
        docker_command: Sequence[str] | None = None,
        limits: SandboxLimits | None = None,
        cli_timeout_seconds: float = 20.0,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}", image):
            raise ValueError("Invalid sandbox image reference")
        if cli_timeout_seconds <= 0 or cli_timeout_seconds > 60:
            raise ValueError("cli_timeout_seconds must be between 0 and 60")
        self.workspaces = workspaces
        self.image = image
        self.docker_command = tuple((docker_binary,) if docker_command is None else docker_command)
        if not self.docker_command or any(not part or "\x00" in part for part in self.docker_command):
            raise ValueError("docker_command must contain non-empty executable arguments")
        self.limits = limits or SandboxLimits()
        self.cli_timeout_seconds = cli_timeout_seconds

    async def run(self, workspace: ManagedWorkspace, command: Sequence[str]) -> SandboxResult:
        argv = self._validate_command(command)
        size = self.workspaces.validate_tree(workspace)
        if size > self.limits.workspace_bytes:
            raise WorkspaceError("Workspace exceeds the execution size limit")

        run_id = uuid.uuid4()
        container_name = f"solvyn-sandbox-{run_id.hex}"
        started = time.monotonic()
        container_id: str | None = None
        cleanup_needed = False
        exec_result = _CommandResult(125, b"", b"Sandbox did not run")
        try:
            created = await self._cli(self._create_args(container_name), timeout=self.cli_timeout_seconds)
            cleanup_needed = created.returncode == 0 or created.timed_out
            container_id = created.stdout.decode("ascii", errors="ignore").strip()
            if created.returncode != 0 or not _CONTAINER_ID.fullmatch(container_id):
                raise SandboxRuntimeError("Docker failed to create the sandbox container")

            started_container = await self._cli(["start", container_id], timeout=self.cli_timeout_seconds)
            if started_container.returncode != 0:
                raise SandboxRuntimeError("Docker failed to start the sandbox container")

            copied_in = await self._cli(
                ["cp", f"{workspace.path}{os.sep}.", f"{container_id}:/workspace"],
                timeout=self.cli_timeout_seconds,
            )
            if copied_in.returncode != 0:
                raise SandboxRuntimeError("Docker failed to stage the workspace")

            permissions_set = await self._cli(
                ["exec", "--user", "0", container_id, "chmod", "-R", "a+rwX", "/workspace"],
                timeout=self.cli_timeout_seconds,
            )
            if permissions_set.returncode != 0:
                raise SandboxRuntimeError("Docker failed to prepare the workspace")

            exec_result = await self._cli(
                ["exec", container_id, *argv],
                timeout=self.limits.timeout_seconds,
                output_limit=self.limits.output_bytes,
            )
            if exec_result.timed_out:
                stopped = await self._cli(["kill", container_id], timeout=self.cli_timeout_seconds)
                if stopped.returncode != 0:
                    raise SandboxRuntimeError("Docker failed to stop the timed-out sandbox")

            await self._export_workspace(container_id, workspace)
        finally:
            if cleanup_needed:
                await self._remove_container(container_name)

        return SandboxResult(
            run_id=run_id,
            command=argv,
            exit_code=None if exec_result.timed_out else exec_result.returncode,
            timed_out=exec_result.timed_out,
            stdout=exec_result.stdout.decode("utf-8", errors="replace"),
            stderr=exec_result.stderr.decode("utf-8", errors="replace"),
            output_truncated=exec_result.truncated,
            duration_seconds=time.monotonic() - started,
        )

    def _create_args(self, name: str) -> list[str]:
        limits = self.limits
        tmp_size = f"{limits.tmp_bytes}"
        workspace_size = f"{limits.workspace_bytes}"
        memory = f"{limits.memory_mb}m"
        return [
            "create",
            "--name",
            name,
            "--label",
            "solvyn.managed=true",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "10001:10001",
            "--memory",
            memory,
            "--memory-swap",
            memory,
            "--cpus",
            str(limits.cpus),
            "--pids-limit",
            str(limits.pids),
            "--shm-size",
            "16m",
            "--ulimit",
            "nofile=128:128",
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={tmp_size},nr_inodes=2048",
            "--tmpfs",
            f"/workspace:rw,nosuid,nodev,size={workspace_size},nr_inodes=10000",
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--pull",
            "never",
            self.image,
            "sleep",
            "infinity",
        ]

    @staticmethod
    def _validate_command(command: Sequence[str]) -> tuple[str, ...]:
        if isinstance(command, (str, bytes)) or not command or len(command) > 128:
            raise ValueError("Command must be a non-empty argv sequence of at most 128 items")
        argv = tuple(command)
        if any(not isinstance(arg, str) or not arg or "\x00" in arg or len(arg) > 4096 for arg in argv):
            raise ValueError("Command arguments must be non-empty strings of at most 4096 characters")
        return argv

    async def _cli(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        output_limit: int = 16 * 1024,
    ) -> _CommandResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.docker_command,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
            )
        except (OSError, ValueError) as exc:
            raise SandboxRuntimeError("Docker CLI is unavailable") from exc
        assert proc.stdout is not None and proc.stderr is not None
        stdout_task = asyncio.create_task(self._read_bounded(proc.stdout, output_limit))
        stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, output_limit))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except TimeoutError:
            timed_out = True
            proc.kill()
            await proc.wait()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        stdout, out_cut = await stdout_task
        stderr, err_cut = await stderr_task
        return _CommandResult(
            returncode=proc.returncode if proc.returncode is not None else 125,
            stdout=stdout,
            stderr=stderr,
            truncated=out_cut or err_cut,
            timed_out=timed_out,
        )

    @staticmethod
    async def _read_bounded(stream: asyncio.StreamReader, limit: int) -> tuple[bytes, bool]:
        chunks = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            remaining = limit - len(chunks)
            if remaining > 0:
                chunks.extend(chunk[:remaining])
            if len(chunk) > max(remaining, 0):
                truncated = True
        return bytes(chunks), truncated

    async def _export_workspace(self, container_id: str, workspace: ManagedWorkspace) -> None:
        proc = await asyncio.create_subprocess_exec(
            *self.docker_command,
            "cp",
            f"{container_id}:/workspace/.",
            "-",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
        )
        assert proc.stdout is not None and proc.stderr is not None
        with tempfile.TemporaryFile() as archive:
            archive_stream = cast(BinaryIO, archive)
            stdout_task = asyncio.create_task(self._write_bounded(proc.stdout, archive_stream, self.limits.workspace_bytes + 2_000_000))
            stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, 16 * 1024))
            try:
                await asyncio.wait_for(proc.wait(), timeout=self.cli_timeout_seconds)
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                stdout_task.cancel()
                stderr_task.cancel()
                raise SandboxRuntimeError("Docker workspace export timed out") from exc
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise
            archive_size, exceeded = await stdout_task
            await stderr_task  # Drain Docker diagnostics without exposing them as job output.
            if proc.returncode != 0 or exceeded:
                raise SandboxRuntimeError("Docker failed to export the sandbox workspace")
            archive_stream.seek(0)
            self._extract_workspace_archive(archive_stream, workspace, archive_size)

    @staticmethod
    async def _write_bounded(stream: asyncio.StreamReader, target: BinaryIO, limit: int) -> tuple[int, bool]:
        size = 0
        exceeded = False
        while chunk := await stream.read(8192):
            size += len(chunk)
            if size > limit:
                exceeded = True
                continue
            target.write(chunk)
        return size, exceeded

    def _extract_workspace_archive(self, archive: BinaryIO, workspace: ManagedWorkspace, archive_size: int) -> None:
        if archive_size > self.limits.workspace_bytes + 2_000_000:
            raise SandboxRuntimeError("Sandbox workspace export exceeded its size limit")
        total = 0
        try:
            with tempfile.TemporaryDirectory(dir=workspace.path.parent, prefix=f".{workspace.id}.export-") as staging_name:
                staging = Path(staging_name)
                seen: set[str] = set()
                with tarfile.open(fileobj=archive, mode="r:") as tar:
                    for member in tar:
                        if member.isdir() and member.name in {".", "./"}:
                            continue
                        try:
                            posix_path = safe_relative_path(member.name)
                        except WorkspaceError as exc:
                            raise SandboxRuntimeError("Sandbox returned an unsafe workspace path") from exc
                        if posix_path.is_absolute():
                            raise SandboxRuntimeError("Sandbox returned an unsafe workspace path")
                        normalized = posix_path.as_posix()
                        if normalized in seen:
                            raise SandboxRuntimeError("Sandbox returned duplicate workspace paths")
                        seen.add(normalized)
                        if not (member.isdir() or member.isfile()):
                            raise SandboxRuntimeError("Sandbox returned an unsupported filesystem entry")
                        destination = staging.joinpath(*posix_path.parts)
                        if not destination.resolve(strict=False).is_relative_to(staging):
                            raise SandboxRuntimeError("Sandbox returned an unsafe workspace path")
                        if member.isdir():
                            destination.mkdir(parents=True, exist_ok=True)
                            continue
                        total += member.size
                        if member.size < 0 or total > self.limits.workspace_bytes:
                            raise SandboxRuntimeError("Sandbox workspace export exceeded its size limit")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        member_source = tar.extractfile(member)
                        if member_source is None:
                            raise SandboxRuntimeError("Sandbox returned an invalid workspace file")
                        with member_source, destination.open("wb") as output:
                            remaining = member.size
                            while remaining:
                                chunk = member_source.read(min(64 * 1024, remaining))
                                if not chunk:
                                    raise SandboxRuntimeError("Sandbox returned a truncated workspace file")
                                output.write(chunk)
                                remaining -= len(chunk)

                current_size = self.workspaces.validate_tree(workspace)
                updated_size = current_size
                for file_path in staging.rglob("*"):
                    if file_path.is_file():
                        relative = file_path.relative_to(staging).as_posix()
                        target = self.workspaces._resolve_path(workspace, relative)
                        if target.is_dir():
                            raise SandboxRuntimeError("Sandbox returned a conflicting workspace path")
                        if target.is_file():
                            updated_size -= target.stat().st_size
                        updated_size += file_path.stat().st_size
                        if updated_size > self.limits.workspace_bytes:
                            raise SandboxRuntimeError("Sandbox workspace export exceeded its size limit")
                for file_path in sorted(path for path in staging.rglob("*") if path.is_file()):
                    relative = file_path.relative_to(staging).as_posix()
                    self.workspaces.write_file(workspace, relative, file_path.read_bytes())
        except (tarfile.TarError, OSError) as exc:
            raise SandboxRuntimeError("Docker returned an invalid workspace archive") from exc

    async def _remove_container(self, container_name: str) -> None:
        # Cleanup targets only the generated name, never user input.
        if not re.fullmatch(r"solvyn-sandbox-[a-f0-9]{32}", container_name):
            raise SandboxRuntimeError("Refusing to remove an invalid sandbox container name")
        removed = await self._cli(["rm", "--force", container_name], timeout=self.cli_timeout_seconds)
        if removed.timed_out:
            raise SandboxRuntimeError("Docker failed to clean up the sandbox container")
        if removed.returncode != 0:
            message = removed.stderr.decode("utf-8", errors="replace").casefold()
            if "no such container" in message or "no such object" in message:
                return
            raise SandboxRuntimeError("Docker failed to clean up the sandbox container")
