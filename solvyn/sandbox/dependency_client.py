"""Authenticated-boundary-free client for the isolated public registry fetcher.

The fetch service is reachable only on a dedicated internal Docker network shared
with the trusted supervisor. No app credentials or workspace files are sent.
"""
from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlparse

import httpx

from solvyn.sandbox.dependencies import (
    DependencyEvidence,
    DependencyInputs,
    _MAX_BUNDLE_BYTES,
    DependencyBlockedError,
    extract_bundle_archive,
)


@dataclass
class DependencyBundle:
    """Validated, per-run temporary wheelhouse owned by the supervisor."""

    temporary: tempfile.TemporaryDirectory[str]
    wheel_dir: Path
    requirements_path: Path
    evidence: DependencyEvidence

    def close(self) -> None:
        self.temporary.cleanup()


async def fetch_dependency_bundle(
    inputs: DependencyInputs,
    *,
    base_url: str,
    timeout_seconds: float = 210.0,
) -> DependencyBundle:
    parsed = urlparse(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "dependency-fetcher"
        or parsed.port != 8002
        or parsed.path not in {"", "/"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise DependencyBlockedError("Dependency fetcher URL is not the configured internal service")
    if not 1 <= timeout_seconds <= 240:
        raise ValueError("Dependency fetch timeout is outside its allowed range")

    temporary = tempfile.TemporaryDirectory(prefix="solvyn-verified-deps-")
    root = Path(temporary.name)
    bundle_file = root / "bundle.tar"
    try:
        async with httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_seconds, connect=5.0),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                "/v1/wheelhouse",
                json={
                    "pyproject": base64.b64encode(inputs.pyproject).decode("ascii"),
                    "uv_lock": base64.b64encode(inputs.lock).decode("ascii"),
                },
                headers={"Accept": "application/x-tar", "Cache-Control": "no-store"},
            ) as response:
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/x-tar":
                    raise DependencyBlockedError("Locked dependencies could not be fetched safely")
                content_length = response.headers.get("content-length")
                if content_length is None or not content_length.isdecimal() or int(content_length) > _MAX_BUNDLE_BYTES:
                    raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
                written = 0
                with bundle_file.open("xb") as target:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > _MAX_BUNDLE_BYTES or written > int(content_length):
                            raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
                        target.write(chunk)
                if written != int(content_length):
                    raise DependencyBlockedError("Dependency bundle transfer was truncated")

        wheel_dir = root / "wheels"
        with bundle_file.open("rb") as source:
            evidence = extract_bundle_archive(
                source,
                wheel_dir,
                inputs.lock_sha256,
            )
        requirements_path = root / "requirements.txt"
        # extract_bundle_archive consumes requirements as an input; re-read it
        # from the validated response tar is intentionally avoided. The helper
        # writes verified wheel contents only, so use this canonical pinned
        # export only after the fetch service supplied and validated it.
        # The requirement file is included in the archive and is copied through
        # a second bounded pass by the helper below.
        with bundle_file.open("rb") as stream:
            import tarfile

            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    if member.name == "requirements.txt":
                        item = archive.extractfile(member)
                        if item is None or member.size > 1_048_576:
                            raise DependencyBlockedError("Dependency bundle requirements are invalid")
                        content = item.read(member.size + 1)
                        if len(content) != member.size:
                            raise DependencyBlockedError("Dependency bundle requirements are truncated")
                        requirements_path.write_bytes(content)
                        break
        if not requirements_path.is_file():
            raise DependencyBlockedError("Dependency bundle is missing its requirements")
        return DependencyBundle(temporary, wheel_dir, requirements_path, evidence)
    except Exception:
        temporary.cleanup()
        raise
