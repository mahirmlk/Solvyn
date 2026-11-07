"""Bounded Git worktree materialization for trusted API workers."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
from pathlib import Path
from typing import Any, cast

from solvyn.repositories.github import GitHubRepositoryError, _validate_git_branch, validate_full_name
from solvyn.sandbox.workspaces import WorkspaceError, safe_relative_path

MAX_REPOSITORY_FILES = 20_000
MAX_REPOSITORY_BYTES = 48 * 1024 * 1024
GIT_FETCH_TIMEOUT_SECONDS = 90


class RepositoryWorktreeError(ValueError):
    """Repository tree or Git checkout exceeds worktree policy."""


def validate_repository_tree(entries: list[dict[str, object]]) -> int:
    """Validate a GitHub tree before fetching objects and return source bytes."""
    if len(entries) > MAX_REPOSITORY_FILES:
        raise RepositoryWorktreeError("Repository contains too many entries for a managed workspace")
    total = 0
    seen: dict[str, str] = {}
    for entry in entries:
        path = entry.get("path")
        kind = entry.get("type")
        mode = entry.get("mode")
        if not isinstance(path, str) or not isinstance(kind, str):
            raise RepositoryWorktreeError("Repository tree contains an invalid entry")
        try:
            normalized = safe_relative_path(path).as_posix()
        except WorkspaceError as exc:
            raise RepositoryWorktreeError("Repository tree contains an unsafe path") from exc
        if normalized != path or any(part.casefold() == ".git" for part in normalized.split("/")):
            raise RepositoryWorktreeError("Repository tree contains an unsafe path")
        parts = normalized.split("/")
        for index in range(1, len(parts) + 1):
            prefix = "/".join(parts[:index])
            previous = seen.get(prefix.casefold())
            if previous is not None and previous != prefix:
                raise RepositoryWorktreeError("Repository tree contains case-insensitive path collisions")
            seen[prefix.casefold()] = prefix
        if kind == "tree" and mode == "040000":
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise RepositoryWorktreeError("Repository contains symlinks, submodules, or unsupported file modes")
        size = entry.get("size")
        if not isinstance(size, int) or size < 0:
            raise RepositoryWorktreeError("Repository tree is missing a regular file size")
        sha = entry.get("sha")
        if not isinstance(sha, str) or len(sha) not in {40, 64} or any(char not in "0123456789abcdefABCDEF" for char in sha):
            raise RepositoryWorktreeError("Repository tree contains an invalid blob revision")
        total += size
        if total > MAX_REPOSITORY_BYTES:
            raise RepositoryWorktreeError("Repository exceeds the 48 MiB source limit")
    return total


def _git_environment(token: str | None) -> dict[str, str]:
    allowed_environment = {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
    environment = {key: value for key, value in os.environ.items() if key in allowed_environment}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GCM_INTERACTIVE": "Never",
        }
    )
    if token:
        if len(token) > 8192 or any(ord(char) < 32 for char in token):
            raise RepositoryWorktreeError("GitHub credential is invalid")
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            }
        )
    else:
        environment["GIT_CONFIG_COUNT"] = "0"
    return environment


async def _run_git(path: Path, arguments: list[str], environment: dict[str, str], *, capture: bool = False) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "protocol.file.allow=never",
            *arguments,
            cwd=path,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=GIT_FETCH_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        if "process" in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        raise
    except (OSError, TimeoutError) as exc:
        if "process" in locals() and process.returncode is None:
            process.kill()
            await process.wait()
        raise RepositoryWorktreeError("Repository checkout could not complete within its time limit") from exc
    if process.returncode:
        raise RepositoryWorktreeError("Repository checkout failed; verify integration access and retry")
    if capture:
        if len(stdout) > 256:
            raise RepositoryWorktreeError("Repository checkout returned invalid revision metadata")
        return stdout.decode("ascii", errors="strict").strip()
    return ""


async def _materialize(
    path: Path,
    full_name: str,
    branch: str,
    expected_sha: str,
    expected_tree_sha: str,
    token: str | None,
    tree: list[dict[str, Any]],
) -> None:
    try:
        owner, repo = validate_full_name(full_name)
    except ValueError as exc:
        raise RepositoryWorktreeError("Repository identity is invalid") from exc
    try:
        _validate_git_branch(branch)
    except GitHubRepositoryError as exc:
        raise RepositoryWorktreeError("Repository branch is invalid") from exc
    if any(
        len(sha) not in {40, 64} or any(char not in "0123456789abcdefABCDEF" for char in sha)
        for sha in (expected_sha, expected_tree_sha)
    ):
        raise RepositoryWorktreeError("Pinned repository revision is invalid")
    environment = _git_environment(None)
    auth_environment = _git_environment(token) if token else environment
    remote = f"https://github.com/{owner}/{repo}.git"
    async with asyncio.timeout(GIT_FETCH_TIMEOUT_SECONDS + 10):
        await _run_git(path, ["init", "--quiet"], environment)
        await _run_git(path, ["remote", "add", "origin", remote], environment)
        await _run_git(
            path,
            ["-c", "fetch.recurseSubmodules=false", "fetch", "--depth=1", "--no-tags", "origin", f"refs/heads/{branch}"],
            auth_environment,
        )
        fetched_sha = await _run_git(path, ["rev-parse", "FETCH_HEAD"], environment, capture=True)
        if fetched_sha.lower() != expected_sha.lower():
            raise RepositoryWorktreeError("Repository default branch changed while the workspace was being created")
        fetched_tree_sha = await _run_git(
            path, ["rev-parse", f"{expected_sha}^{{tree}}"], environment, capture=True
        )
        if fetched_tree_sha.lower() != expected_tree_sha.lower():
            raise RepositoryWorktreeError("Fetched repository tree does not match the pinned GitHub tree")
        await _run_git(path, ["read-tree", expected_sha], environment)
        await _run_git(path, ["update-ref", "HEAD", expected_sha], environment)
        await _checkout_raw_blobs(path, tree, environment)


async def _checkout_raw_blobs(path: Path, entries: list[dict[str, Any]], environment: dict[str, str]) -> None:
    """Write Git objects byte-for-byte, bypassing `.gitattributes` filters."""
    blobs = sorted((entry for entry in entries if entry.get("type") == "blob"), key=lambda entry: str(entry["path"]))
    input_data = b"".join(str(entry["sha"]).encode("ascii") + b"\n" for entry in blobs)
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            "protocol.file.allow=never",
            "cat-file",
            "--batch",
            cwd=path,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        raise RepositoryWorktreeError("Git could not safely materialize repository content") from exc
    stdin = cast(asyncio.StreamWriter, process.stdin)
    stdout = cast(asyncio.StreamReader, process.stdout)

    async def send_object_ids() -> None:
        stdin.write(input_data)
        await stdin.drain()
        stdin.close()
        await stdin.wait_closed()

    async def write_blobs() -> None:
        for entry in blobs:
            header = await stdout.readline()
            if len(header) > 256 or not header.endswith(b"\n"):
                raise RepositoryWorktreeError("Git returned invalid object metadata")
            fields = header[:-1].split(b" ")
            if len(fields) != 3 or fields[1] != b"blob":
                raise RepositoryWorktreeError("Git returned a missing or unsupported repository object")
            try:
                object_sha = fields[0].decode("ascii")
                size = int(fields[2])
            except (UnicodeDecodeError, ValueError) as exc:
                raise RepositoryWorktreeError("Git returned invalid object metadata") from exc
            if object_sha.lower() != str(entry["sha"]).lower() or size != entry["size"] or size < 0:
                raise RepositoryWorktreeError("Git object metadata does not match the pinned repository tree")
            relative_path = safe_relative_path(str(entry["path"]))
            if any(part.casefold() == ".git" for part in relative_path.parts):
                raise RepositoryWorktreeError("Repository tree contains an unsafe path")
            target = path.joinpath(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = size
            with target.open("wb") as output:
                while remaining:
                    chunk = await stdout.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise RepositoryWorktreeError("Git returned truncated repository content")
                    output.write(chunk)
                    remaining -= len(chunk)
            if await stdout.readexactly(1) != b"\n":
                raise RepositoryWorktreeError("Git returned an invalid object boundary")
            if entry.get("mode") == "100755":
                target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        if await stdout.read(1):
            raise RepositoryWorktreeError("Git returned unexpected repository content")

    try:
        async with asyncio.timeout(GIT_FETCH_TIMEOUT_SECONDS):
            transfer_tasks = [asyncio.create_task(send_object_ids()), asyncio.create_task(write_blobs())]
            try:
                await asyncio.gather(*transfer_tasks)
            except BaseException:
                for task in transfer_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*transfer_tasks, return_exceptions=True)
                raise
            returncode = await process.wait()
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    except (OSError, asyncio.IncompleteReadError) as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise RepositoryWorktreeError("Git could not safely materialize repository content") from exc
    except Exception:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if returncode:
        raise RepositoryWorktreeError("Git could not safely materialize repository content")


async def materialize_repository_worktree(
    path: Path,
    *,
    full_name: str,
    branch: str,
    expected_sha: str,
    expected_tree_sha: str,
    token: str | None,
    tree: list[dict[str, Any]],
) -> None:
    """Fetch a shallow, credential-isolated checkout without invoking a shell."""
    try:
        await _materialize(path, full_name, branch, expected_sha, expected_tree_sha, token, tree)
    except TimeoutError as exc:
        raise RepositoryWorktreeError("Repository checkout could not complete within its time limit") from exc
