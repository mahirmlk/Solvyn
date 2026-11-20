#!/usr/bin/env python3
"""Scan workspace files with trusted Gitleaks policy and bounded output."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path

_MAX_FILES = 20_000
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
            if path.is_symlink() or not path.is_file():
                raise ValueError("Workspace contains an unsupported filesystem entry")
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Workspace contains an unsupported filesystem entry")
            files.append(path)
            if len(files) > _MAX_FILES:
                raise ValueError("Workspace has too many files for a bounded secret scan")
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _write_bounded(stream, data: bytes, current: int) -> int:
    if current >= _MAX_OUTPUT_BYTES:
        return current
    remaining = _MAX_OUTPUT_BYTES - current
    chunk = data[:remaining]
    stream.buffer.write(chunk)
    current += len(chunk)
    return current


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != len(_CONTROL_ARGUMENTS) + 1 or args[:-1] != _CONTROL_ARGUMENTS or args[-1] != "/workspace":
        print("Secret scan command is invalid.", file=sys.stderr)
        return 2

    root = Path("/workspace")
    deadline = time.monotonic() + _TOTAL_TIMEOUT_SECONDS
    output_bytes = 0
    findings = False
    files_scanned = 0
    try:
        files = _workspace_files(root)
        for path in files:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args[0], _TOTAL_TIMEOUT_SECONDS)
            scan_args = [*args[:-1], str(path)]
            result = subprocess.run(
                scan_args,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
            )
            files_scanned += 1
            output_bytes = _write_bounded(sys.stdout, result.stdout, output_bytes)
            output_bytes = _write_bounded(sys.stderr, result.stderr, output_bytes)
            if result.returncode != 0:
                findings = True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        print("Secret scan could not complete.", file=sys.stderr)
        return 2

    if output_bytes >= _MAX_OUTPUT_BYTES:
        print("\nSecret scan output truncated.", file=sys.stderr)
    if findings:
        return 1
    print(f"Secret scan passed ({files_scanned} files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
