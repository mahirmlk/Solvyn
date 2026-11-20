#!/usr/bin/env python3
"""Scan workspace files with trusted Gitleaks policy and bounded output."""

from __future__ import annotations

import os
import selectors
import stat
import subprocess
import sys
import time
from pathlib import Path

_MAX_FILES = 20_000
_MAX_FILE_BYTES = 128_000_000
_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_OUTPUT_BYTES = 96 * 1024
_TOTAL_TIMEOUT_SECONDS = 95
_CONTROL_ARGUMENTS = [
    "gitleaks",
    "dir",
    "--config",
    "/opt/solvyn/gitleaks.toml",
    "--gitleaks-ignore-path",
    "/opt/solvyn/gitleaks.ignore",
    "--ignore-gitleaks-allow",
    "--redact=100",
    "--no-banner",
    "--no-color",
    "--max-target-megabytes",
    "128",
    "--timeout",
    "90",
    "--exit-code",
]


def _workspace_files(root: Path) -> list[Path]:
    files: list[Path] = []
    total_bytes = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in list(dirnames):
            path = current / name
            if name == ".git":
                dirnames.remove(name)
            elif path.is_symlink():
                raise ValueError("Workspace contains an unsupported symlink")
        for name in filenames:
            path = current / name
            metadata = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Workspace contains an unsupported filesystem entry")
            if metadata.st_size > _MAX_FILE_BYTES:
                raise ValueError("Workspace file exceeds the Gitleaks scan size limit")
            total_bytes += metadata.st_size
            if total_bytes > _MAX_TOTAL_BYTES:
                raise ValueError("Workspace exceeds the Gitleaks scan byte limit")
            files.append(path)
            if len(files) > _MAX_FILES:
                raise ValueError("Workspace has too many files for a bounded secret scan")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _bounded_chunk(data: bytes, written: int) -> tuple[bytes, int, bool]:
    remaining = max(0, _MAX_OUTPUT_BYTES - written)
    chunk = data[:remaining]
    return chunk, written + len(chunk), len(chunk) != len(data)


def _run_bounded(argv: list[str], deadline: float, written: int) -> tuple[int, int, bool]:
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if process.stdout is None:
        process.kill()
        process.wait()
        raise OSError("Unable to capture secret scanner output")

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    truncated = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, _TOTAL_TIMEOUT_SECONDS)
            events = selector.select(timeout=min(remaining, 0.25))
            if not events and process.poll() is not None:
                continue
            for key, _mask in events:
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output, written, was_truncated = _bounded_chunk(chunk, written)
                if output:
                    sys.stdout.buffer.write(output)
                    sys.stdout.buffer.flush()
                truncated = truncated or was_truncated
        return process.wait(timeout=max(0.01, deadline - time.monotonic())), written, truncated
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != len(_CONTROL_ARGUMENTS) + 1 or args[:-1] != _CONTROL_ARGUMENTS or args[-1] != "/workspace":
        print("Secret scan command is invalid.", file=sys.stderr)
        return 2

    root = Path("/workspace")
    if not root.is_dir():
        print("Secret scan workspace is unavailable.", file=sys.stderr)
        return 2

    deadline = time.monotonic() + _TOTAL_TIMEOUT_SECONDS
    output_bytes = 0
    truncated = False
    findings = False
    files_scanned = 0
    try:
        files = _workspace_files(root)
        for path in files:
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(args[0], _TOTAL_TIMEOUT_SECONDS)
            scan_args = [*args[:-1], str(path)]
            exit_code, output_bytes, output_truncated = _run_bounded(scan_args, deadline, output_bytes)
            files_scanned += 1
            truncated = truncated or output_truncated
            if exit_code != 0:
                findings = True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("Secret scan could not complete.", file=sys.stderr)
        return 2

    if truncated:
        print("Secret scan output truncated.", file=sys.stderr)
    if findings:
        return 1
    print(f"Secret scan passed ({files_scanned} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
