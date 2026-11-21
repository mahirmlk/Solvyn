"""Ephemeral Docker execution with a restrictive, fixed container policy.

The Docker CLI is used only by this trusted supervisor. No daemon socket,
credentials, caller environment, or network is mounted into the untrusted job.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tarfile
import tempfile
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast

from solvyn.sandbox.dependencies import (
    _PIP_VERSION,
    DependencyBlockedError,
    DependencyEvidence,
    _sha256_file,
    create_bundle_archive,
    discover_dependency_inputs,
)
from solvyn.sandbox.dependency_client import DependencyBundle, fetch_dependency_bundle
from solvyn.sandbox.workspaces import (
    ManagedWorkspace,
    WorkspaceError,
    WorkspaceManager,
    safe_relative_path,
)

_CONTAINER_ID = re.compile(r"^[a-f0-9]{12,64}$")


class SandboxRuntimeError(RuntimeError):
    """The isolated runtime could not safely start or complete a job."""


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: float = 120.0
    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    workspace_bytes: int = 128 * 1024 * 1024
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
    dependency_evidence: DependencyEvidence | None = None


@dataclass(frozen=True)
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    timed_out: bool = False


class _BoundedArchiveWriter:
    """Forward tar bytes to a file while enforcing a hard byte ceiling."""

    def __init__(self, target: BinaryIO, limit: int) -> None:
        self.target = target
        self.limit = limit
        self.size = 0

    def write(self, content: bytes) -> int:
        if self.size + len(content) > self.limit:
            raise SandboxRuntimeError("Workspace archive exceeded its size limit")
        written = self.target.write(content)
        if written != len(content):
            raise OSError("Unable to write a complete workspace archive")
        self.size += written
        return written

    def tell(self) -> int:
        return self.size

    def flush(self) -> None:
        self.target.flush()


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
        dependency_fetcher_url: str | None = None,
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
        self.dependency_fetcher_url = dependency_fetcher_url or os.getenv("DEPENDENCY_FETCHER_URL") or "http://dependency-fetcher:8002"

    async def check_ready(self) -> bool:
        """Check that the configured engine and prebuilt execution image are available."""
        engine = await self._cli(["info"], timeout=self.cli_timeout_seconds)
        if engine.returncode != 0 or engine.timed_out:
            return False
        image = await self._cli(["image", "inspect", self.image], timeout=self.cli_timeout_seconds)
        return image.returncode == 0 and not image.timed_out

    async def run(self, workspace: ManagedWorkspace, command: Sequence[str], *, install_dependencies: bool = False) -> SandboxResult:
        argv = self._validate_command(command)
        size = self.workspaces.validate_tree(workspace)
        if size > self.limits.workspace_bytes:
            raise WorkspaceError("Workspace exceeds the execution size limit")

        source_fingerprint = self.workspaces.fingerprint(workspace)
        dependency_bundle: DependencyBundle | None = None
        if install_dependencies:
            try:
                inputs = discover_dependency_inputs(workspace.path, required=False)
                if inputs is not None:
                    dependency_bundle = await fetch_dependency_bundle(
                        inputs,
                        base_url=self.dependency_fetcher_url,
                    )
            except (DependencyBlockedError, OSError, ValueError) as exc:
                raise SandboxRuntimeError(f"Dependency setup is blocked: {exc}") from exc

        run_id = uuid.uuid4()
        container_name = f"solvyn-sandbox-{run_id.hex}"
        started = time.monotonic()
        container_id: str | None = None
        cleanup_needed = False
        exec_result = _CommandResult(125, b"", b"Sandbox did not run")
        try:
            created = await self._cli(self._create_args(container_name, install_dependencies=install_dependencies), timeout=self.cli_timeout_seconds)
            cleanup_needed = created.returncode == 0 or created.timed_out
            container_id = created.stdout.decode("ascii", errors="ignore").strip()
            if created.returncode != 0 or not _CONTAINER_ID.fullmatch(container_id):
                raise SandboxRuntimeError("Docker failed to create the sandbox container")

            started_container = await self._cli(["start", container_id], timeout=self.cli_timeout_seconds)
            if started_container.returncode != 0:
                raise SandboxRuntimeError("Docker failed to start the sandbox container")

            await self._stage_workspace(container_id, workspace)
            if dependency_bundle is not None:
                await self._stage_dependency_bundle(container_id, dependency_bundle)
                await self._install_dependencies(container_id)
                if self.workspaces.fingerprint(workspace) != source_fingerprint:
                    raise SandboxRuntimeError("Dependency setup changed the source workspace")

            permissions_set = await self._cli(
                ["exec", "--user", "0", container_id, "chmod", "-R", "a+rwX", "/workspace"],
                timeout=self.cli_timeout_seconds,
            )
            if permissions_set.returncode != 0:
                raise SandboxRuntimeError("Docker failed to prepare the workspace")

            run_arguments = ["exec"]
            if dependency_bundle is not None:
                run_arguments.extend(["--env", "PYTHONPATH=/deps/site-packages:/workspace"])
            run_arguments.extend([container_id, *argv])
            exec_result = await self._cli(
                run_arguments,
                timeout=self.limits.timeout_seconds,
                output_limit=self.limits.output_bytes,
            )
            if exec_result.timed_out:
                stopped = await self._cli(["kill", container_id], timeout=self.cli_timeout_seconds)
                if stopped.returncode != 0:
                    raise SandboxRuntimeError("Docker failed to stop the timed-out sandbox")

            await self._export_workspace(container_id, workspace)
        finally:
            try:
                if cleanup_needed:
                    await self._remove_container(container_name)
            finally:
                if dependency_bundle is not None:
                    dependency_bundle.close()

        return SandboxResult(
            run_id=run_id,
            command=argv,
            exit_code=None if exec_result.timed_out else exec_result.returncode,
            timed_out=exec_result.timed_out,
            stdout=exec_result.stdout.decode("utf-8", errors="replace"),
            stderr=exec_result.stderr.decode("utf-8", errors="replace"),
            output_truncated=exec_result.truncated,
            duration_seconds=time.monotonic() - started,
            dependency_evidence=dependency_bundle.evidence if dependency_bundle is not None else None,
        )

    def _create_args(self, name: str, *, install_dependencies: bool = False) -> list[str]:
        limits = self.limits
        tmp_size = f"{limits.tmp_bytes}"
        workspace_size = f"{limits.workspace_bytes}"
        memory = f"{max(limits.memory_mb, 2048) if install_dependencies else limits.memory_mb}m"
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
            *(["--tmpfs", "/deps:rw,nosuid,nodev,size=1536m,uid=10001,gid=10001,nr_inodes=100000"] if install_dependencies else []),
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--env",
            "PYTHONPYCACHEPREFIX=/tmp/pycache",
            "--env",
            "PYTEST_ADDOPTS=-p no:cacheprovider",
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

    @staticmethod
    def _docker_error_detail(stderr: bytes, *, sensitive_paths: Sequence[str] = ()) -> str:
        """Return short, printable Docker diagnostics without local workspace paths."""
        detail = stderr.decode("utf-8", errors="replace")
        detail = "".join(character if character.isprintable() or character.isspace() else " " for character in detail)
        detail = " ".join(detail.split())
        for path in sensitive_paths:
            if path:
                detail = detail.replace(path, "<workspace>")
        return detail[:300]

    def _create_workspace_archive(self, workspace: ManagedWorkspace, archive: BinaryIO) -> int:
        """Build a bounded tar containing only validated regular workspace files."""
        bounded = _BoundedArchiveWriter(
            archive,
            self.limits.workspace_bytes + 2_000_000,
        )
        with tarfile.open(fileobj=cast(BinaryIO, bounded), mode="w") as tar:
            for path in sorted(workspace.path.rglob("*")):
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode):
                    raise SandboxRuntimeError("Workspace contains an unsupported filesystem entry")
                if stat.S_ISDIR(metadata.st_mode):
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise SandboxRuntimeError("Workspace contains an unsupported filesystem entry")
                relative = path.relative_to(workspace.path).as_posix()
                safe_relative_path(relative)
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
                descriptor = os.open(path, flags)
                try:
                    opened = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
                    ):
                        raise SandboxRuntimeError("Workspace changed while it was being staged")
                    member = tarfile.TarInfo(relative)
                    member.type = tarfile.REGTYPE
                    member.mode = stat.S_IMODE(opened.st_mode)
                    member.size = opened.st_size
                    member.mtime = opened.st_mtime
                    member.uid = member.gid = 0
                    member.uname = member.gname = ""
                    with os.fdopen(descriptor, "rb", closefd=False) as source:
                        tar.addfile(member, source)
                finally:
                    os.close(descriptor)
        size = bounded.tell()
        archive.seek(0)
        return size

    async def _stage_dependency_bundle(self, container_id: str, bundle: DependencyBundle) -> None:
        """Copy a locally verified archive into the job's separate /deps tmpfs."""
        if await self._target_runtime_fingerprint(container_id) != bundle.evidence.runtime_fingerprint:
            raise SandboxRuntimeError("Dependency wheels do not match the sandbox Python ABI or platform")
        with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as archive:
            requirements = bundle.requirements_path.read_bytes()
            size = create_bundle_archive(bundle.wheel_dir, requirements, {
                "schema": 1,
                "lock_sha256": bundle.evidence.lock_sha256,
                "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
                "package_count": bundle.evidence.package_count,
                "wheelhouse_bytes": bundle.evidence.wheelhouse_bytes,
                "wheels": [
                    {"filename": path.name, "sha256": _sha256_file(path), "size": path.stat().st_size}
                    for path in sorted(bundle.wheel_dir.iterdir(), key=lambda item: item.name)
                ],
            }, cast(BinaryIO, archive))
            if size > 400 * 1024 * 1024:
                raise SandboxRuntimeError("Verified dependency archive exceeds its transfer limit")
            archive.seek(0)
            proc = await asyncio.create_subprocess_exec(
                *self.docker_command,
                "exec", "--user", "0", "-i", container_id,
                "tar", "-x", "--no-same-owner", "--no-same-permissions", "-C", "/deps",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
            )
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            stdout_task = asyncio.create_task(self._read_bounded(proc.stdout, 16 * 1024))
            stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, 16 * 1024))
            copy_task = asyncio.create_task(self._copy_archive(cast(BinaryIO, archive), proc.stdin))
            try:
                await asyncio.wait_for(copy_task, timeout=self.cli_timeout_seconds)
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                await asyncio.wait_for(proc.wait(), timeout=self.cli_timeout_seconds)
            except (TimeoutError, asyncio.CancelledError) as exc:
                proc.kill()
                await proc.wait()
                await asyncio.gather(copy_task, stdout_task, stderr_task, return_exceptions=True)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise SandboxRuntimeError("Dependency bundle staging timed out") from exc
            _, _ = await stdout_task
            stderr, _ = await stderr_task
            if proc.returncode != 0:
                detail = self._docker_error_detail(stderr)
                raise SandboxRuntimeError("Docker failed to stage dependency wheels" + (f": {detail}" if detail else ""))

    async def _target_runtime_fingerprint(self, container_id: str) -> str:
        script = (
            "import hashlib,json,platform,sys,sysconfig; "
            "identity={'python':list(sys.version_info[:3]),'implementation':sys.implementation.name,"
            "'soabi':sysconfig.get_config_var('SOABI'),'platform':sysconfig.get_platform(),"
            "'machine':platform.machine(),'libc':list(platform.libc_ver())}; "
            "print(hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest())"
        )
        result = await self._cli(["exec", container_id, "python", "-c", script], timeout=self.cli_timeout_seconds)
        if result.returncode != 0 or result.timed_out:
            raise SandboxRuntimeError("Docker could not verify the sandbox Python runtime")
        fingerprint = result.stdout.decode("ascii", errors="ignore").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise SandboxRuntimeError("Sandbox Python runtime returned invalid ABI evidence")
        return fingerprint

    async def _install_dependencies(self, container_id: str) -> None:
        pip_version = await self._cli(
            ["exec", container_id, "python", "-m", "pip", "--version"],
            timeout=self.cli_timeout_seconds,
        )
        if pip_version.returncode != 0 or not pip_version.stdout.decode("ascii", errors="ignore").startswith(f"pip {_PIP_VERSION} "):
            raise SandboxRuntimeError("Sandbox pip version does not match the pinned dependency toolchain")
        created = await self._cli(
            ["exec", "--user", "10001", container_id, "mkdir", "-p", "/deps/site-packages", "/deps/tmp"],
            timeout=self.cli_timeout_seconds,
        )
        if created.returncode != 0 or created.timed_out:
            detail = self._docker_error_detail(created.stderr)
            suffix = f": {detail}" if detail else ""
            raise SandboxRuntimeError("Docker failed to prepare the dependency target" + suffix)
        installed = await self._cli(
            [
                "exec", "--user", "10001",
                "--env", "PIP_CONFIG_FILE=/dev/null",
                "--env", "PIP_NO_INDEX=1",
                "--env", "PIP_NO_INPUT=1",
                "--env", "TMPDIR=/deps/tmp",
                "--env", "PIP_DISABLE_PIP_VERSION_CHECK=1",
                container_id, "python", "-m", "pip", "install",
                "--disable-pip-version-check", "--no-input", "--no-index",
                "--require-hashes", "--only-binary=:all:", "--no-deps", "--no-cache-dir",
                "--find-links", "/deps/wheels", "--target", "/deps/site-packages",
                "-r", "/deps/requirements.txt",
            ],
            timeout=self.limits.timeout_seconds,
            output_limit=self.limits.output_bytes,
        )
        if installed.returncode != 0 or installed.timed_out:
            raise SandboxRuntimeError("Offline dependency installation failed")

    async def _stage_workspace(self, container_id: str, workspace: ManagedWorkspace) -> None:
        """Stream workspace bytes to tar in the container without using docker cp."""
        with tempfile.TemporaryFile() as archive:
            self._create_workspace_archive(workspace, cast(BinaryIO, archive))
            proc = await asyncio.create_subprocess_exec(
                *self.docker_command,
                "exec",
                "--user",
                "0",
                "-i",
                container_id,
                "tar",
                "-x",
                "--no-same-owner",
                "--no-same-permissions",
                "-C",
                "/workspace",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
            )
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            stdout_task = asyncio.create_task(self._read_bounded(proc.stdout, 16 * 1024))
            stderr_task = asyncio.create_task(self._read_bounded(proc.stderr, 16 * 1024))
            copy_task = asyncio.create_task(self._copy_archive(archive, proc.stdin))
            try:
                try:
                    await asyncio.wait_for(copy_task, timeout=self.cli_timeout_seconds)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                await asyncio.wait_for(proc.wait(), timeout=self.cli_timeout_seconds)
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                await asyncio.gather(copy_task, stdout_task, stderr_task, return_exceptions=True)
                raise SandboxRuntimeError("Docker workspace staging timed out") from exc
            except asyncio.CancelledError:
                proc.kill()
                await proc.wait()
                await asyncio.gather(copy_task, stdout_task, stderr_task, return_exceptions=True)
                raise
            _, _ = await stdout_task
            stderr, _ = await stderr_task
            if proc.returncode != 0:
                detail = self._docker_error_detail(stderr, sensitive_paths=(str(workspace.path),))
                suffix = f": {detail}" if detail else ""
                raise SandboxRuntimeError(f"Docker failed to stage the workspace{suffix}")

    @staticmethod
    async def _copy_archive(archive: BinaryIO, stdin: asyncio.StreamWriter) -> None:
        while chunk := archive.read(64 * 1024):
            stdin.write(chunk)
            await stdin.drain()

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
            "exec",
            "-i",
            container_id,
            "tar",
            "-c",
            "-C",
            "/workspace",
            ".",
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
            stderr, _ = await stderr_task
            if proc.returncode != 0 or exceeded:
                detail = self._docker_error_detail(stderr)
                suffix = f": {detail}" if detail else ""
                raise SandboxRuntimeError(f"Docker failed to export the sandbox workspace{suffix}")
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
