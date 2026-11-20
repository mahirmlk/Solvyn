#!/usr/bin/env python3
"""Run Gitleaks with trusted policy while treating workspace config as scan input."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

_CONTROL_FILES = (".gitleaks.toml", ".gitleaksignore")
_SCAN_TIMEOUT_SECONDS = 95


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 4 or args[0:2] != ["gitleaks", "dir"]:
        print("Secret scan command is invalid.", file=sys.stderr)
        return 2

    workspace = Path(args[-1])
    try:
        root = workspace.resolve(strict=True)
    except OSError:
        print("Secret scan workspace is unavailable.", file=sys.stderr)
        return 2
    if not root.is_dir():
        print("Secret scan workspace is not a directory.", file=sys.stderr)
        return 2

    control_dir: Path | None = None
    moved: list[tuple[Path, Path]] = []
    result: subprocess.CompletedProcess[bytes] | None = None
    failed = False
    try:
        for name in _CONTROL_FILES:
            source = root / name
            if not source.exists():
                continue
            if source.is_symlink():
                print("Secret scan workspace contains an unsupported control file.", file=sys.stderr)
                return 2
            if control_dir is None:
                control_dir = Path(tempfile.mkdtemp(prefix=".solvyn-gitleaks-", dir=root))
            destination = control_dir / name
            os.replace(source, destination)
            moved.append((destination, source))

        result = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_SCAN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    finally:
        try:
            for source, destination in reversed(moved):
                os.replace(source, destination)
            if control_dir is not None:
                control_dir.rmdir()
        except OSError:
            failed = True

    if failed or result is None:
        print("Secret scan could not complete.", file=sys.stderr)
        return 2

    if result.stdout:
        sys.stdout.buffer.write(result.stdout)
    if result.stderr:
        sys.stderr.buffer.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
