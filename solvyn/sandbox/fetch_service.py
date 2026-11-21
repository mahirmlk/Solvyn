"""Restricted, stateless PyPI wheel fetch service.

The service receives only a bounded pyproject.toml and uv.lock pair. It has no
workspace mount, provider credentials, Docker socket, or writable shared cache.
Network access is enforced by the deployment network and the egress proxy.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from solvyn.sandbox.dependencies import (
    DependencyBlockedError,
    _MAX_BUNDLE_BYTES,
    _MAX_LOCK_BYTES,
    _MAX_PYPROJECT_BYTES,
    build_bundle_manifest,
    create_bundle_archive,
    validate_uv_project,
)
import tomllib

_MAX_REQUEST_BYTES = 3 * 1024 * 1024
_PROCESS_TIMEOUT = 180
_PROXY = os.environ.get("HTTPS_PROXY", "http://dependency-proxy:3128")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


async def _read_bounded(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Dependency request is too large")
        body.extend(chunk)
    return bytes(body)


def _run_tool(args: list[str], *, cwd: Path, env: dict[str, str]) -> bytes:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_PROCESS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DependencyBlockedError("Dependency resolution exceeded its time limit") from exc
    if result.returncode != 0:
        # Do not expose registry URLs, proxy diagnostics, or filesystem paths.
        raise DependencyBlockedError("Locked dependencies could not be exported or fetched safely")
    return result.stdout


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/wheelhouse")
async def wheelhouse(request: Request) -> StreamingResponse:
    try:
        payload = json.loads(await _read_bounded(request))
        if not isinstance(payload, dict) or set(payload) != {"pyproject", "uv_lock"}:
            raise DependencyBlockedError("Dependency request must contain only pyproject and uv.lock")
        if not isinstance(payload["pyproject"], str) or not isinstance(payload["uv_lock"], str):
            raise DependencyBlockedError("Dependency request fields must be base64 text")
        pyproject_bytes = base64.b64decode(payload["pyproject"], validate=True)
        lock_bytes = base64.b64decode(payload["uv_lock"], validate=True)
        if not pyproject_bytes or len(pyproject_bytes) > _MAX_PYPROJECT_BYTES:
            raise DependencyBlockedError("pyproject.toml exceeds its configured limit")
        if not lock_bytes or len(lock_bytes) > _MAX_LOCK_BYTES:
            raise DependencyBlockedError("uv.lock exceeds its configured limit")
        project = tomllib.loads(pyproject_bytes.decode("utf-8"))
        lock = tomllib.loads(lock_bytes.decode("utf-8"))
        validate_uv_project(project, lock)
        lock_sha = hashlib.sha256(lock_bytes).hexdigest()

        with tempfile.TemporaryDirectory(prefix="solvyn-deps-") as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_bytes(pyproject_bytes)
            (root / "uv.lock").write_bytes(lock_bytes)
            wheel_dir = root / "wheelhouse"
            wheel_dir.mkdir(mode=0o700)
            env = {
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": str(root),
                "TMPDIR": str(root),
                "UV_NO_CONFIG": "1",
                "UV_CACHE_DIR": str(root / "uv-cache"),
                "UV_NO_CACHE": "1",
                "PIP_CONFIG_FILE": os.devnull,
                "PIP_INDEX_URL": "https://pypi.org/simple",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1",
                "HTTPS_PROXY": _PROXY,
                "HTTP_PROXY": _PROXY,
                "https_proxy": _PROXY,
                "http_proxy": _PROXY,
                "NO_PROXY": "",
                "no_proxy": "",
            }
            exported = await asyncio.to_thread(
                _run_tool,
                [
                    "uv", "export", "--locked", "--all-groups", "--all-extras",
                    "--no-emit-project", "--format", "requirements-txt",
                ],
                cwd=root,
                env=env,
            )
            if len(exported) > 1_048_576:
                raise DependencyBlockedError("Exported dependency set exceeds its configured limit")
            requirements = exported
            # The resolver exports exact versions and hashes. pip is restricted
            # to binary artifacts and verifies every artifact against those hashes.
            (root / "requirements.txt").write_bytes(requirements)
            await asyncio.to_thread(
                _run_tool,
                [
                    "python", "-m", "pip", "download", "--disable-pip-version-check",
                    "--no-input", "--only-binary=:all:", "--require-hashes",
                    "--no-deps", "--dest", str(wheel_dir), "-r", str(root / "requirements.txt"),
                ],
                cwd=root,
                env=env,
            )
            manifest, _ = build_bundle_manifest(wheel_dir, lock_sha256=lock_sha, requirements=requirements)
            bundle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
            bundle_size = create_bundle_archive(wheel_dir, requirements, manifest, bundle)
            if bundle_size > _MAX_BUNDLE_BYTES:
                bundle.close()
                raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
            bundle.seek(0)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while chunk := await asyncio.to_thread(bundle.read, 1024 * 1024):
                    yield chunk
            finally:
                bundle.close()

        return StreamingResponse(
            chunks(),
            media_type="application/x-tar",
            headers={"Content-Length": str(bundle_size), "Cache-Control": "no-store"},
        )
    except (DependencyBlockedError, tomllib.TOMLDecodeError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Dependency retrieval failed") from exc
