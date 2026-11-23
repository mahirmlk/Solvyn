"""Authenticated-boundary-free client for the isolated public registry fetcher.

The fetch service is reachable only on a dedicated internal Docker network shared
with the trusted supervisor. No app credentials or workspace files are sent.
"""
from __future__ import annotations

import base64
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from solvyn.sandbox.dependencies import (
    _MAX_BUNDLE_BYTES,
    DependencyBlockedError,
    DependencyEvidence,
    DependencyInputs,
    extract_bundle_archive,
)
from solvyn.sandbox.npm_dependencies import (
    MAX_BUNDLE_BYTES as _MAX_NPM_BUNDLE_BYTES,
)
from solvyn.sandbox.npm_dependencies import (
    NpmDependencyInputs,
    extract_npm_bundle,
)


@dataclass
class DependencyBundle:
    """Validated, per-run temporary wheelhouse owned by the supervisor."""

    temporary: tempfile.TemporaryDirectory[str]
    wheel_dir: Path | None
    requirements_path: Path | None
    evidence: DependencyEvidence
    node_modules_dir: Path | None = None

    def close(self) -> None:
        self.temporary.cleanup()


async def fetch_dependency_bundle(
    inputs: DependencyInputs | NpmDependencyInputs,
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

    is_npm = isinstance(inputs, NpmDependencyInputs)
    bundle_limit = _MAX_NPM_BUNDLE_BYTES if is_npm else _MAX_BUNDLE_BYTES
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
                json=(
                    {
                        "package_json": base64.b64encode(inputs.manifest).decode("ascii"),
                        "package_lock": base64.b64encode(inputs.lock).decode("ascii"),
                    }
                    if isinstance(inputs, NpmDependencyInputs)
                    else {
                        "pyproject": base64.b64encode(inputs.pyproject).decode("ascii"),
                        "uv_lock": base64.b64encode(inputs.lock).decode("ascii"),
                    }
                ),
                headers={"Accept": "application/x-tar", "Cache-Control": "no-store"},
            ) as response:
                if response.status_code != 200 or response.headers.get("content-type", "").split(";")[0] != "application/x-tar":
                    raise DependencyBlockedError("Locked dependencies could not be fetched safely")
                content_length = response.headers.get("content-length")
                if content_length is None or not content_length.isdecimal() or int(content_length) > bundle_limit:
                    raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
                written = 0
                with bundle_file.open("xb") as target:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > bundle_limit or written > int(content_length):
                            raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
                        target.write(chunk)
                if written != int(content_length):
                    raise DependencyBlockedError("Dependency bundle transfer was truncated")

        if is_npm:
            if not isinstance(inputs, NpmDependencyInputs):
                raise DependencyBlockedError("Dependency input ecosystem changed during fetch")
            node_modules_dir = root / "node_modules"
            with bundle_file.open("rb") as source:
                evidence = extract_npm_bundle(source, node_modules_dir, inputs.lock_sha256, len(inputs.packages))
            return DependencyBundle(temporary, None, None, evidence, node_modules_dir)

        wheel_dir = root / "wheels"
        with bundle_file.open("rb") as source:
            evidence = extract_bundle_archive(source, wheel_dir, inputs.lock_sha256)
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
