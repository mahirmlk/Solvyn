"""Restricted, stateless PyPI wheel fetch service.

Only bounded pyproject.toml and uv.lock bytes cross this boundary. The service
has no workspace mount, credentials, Docker socket, or shared writable cache.
Docker network topology and a host-allowlisted proxy enforce its egress policy.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import http.server
import json
import os
import subprocess
import tempfile
import threading
import tomllib
from pathlib import Path
from typing import BinaryIO

from solvyn.sandbox.dependencies import (
    DependencyBlockedError,
    _MAX_BUNDLE_BYTES,
    _MAX_LOCK_BYTES,
    _MAX_PYPROJECT_BYTES,
    build_bundle_manifest,
    create_bundle_archive,
    validate_uv_project,
)

_MAX_REQUEST_BYTES = 3 * 1024 * 1024
_PROCESS_TIMEOUT = 180
_MAX_ACTIVE_REQUESTS = 2
_PROXY = "http://dependency-proxy:3128"
_slots = threading.BoundedSemaphore(_MAX_ACTIVE_REQUESTS)


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
        raise DependencyBlockedError("Locked dependencies could not be exported or fetched safely")
    return result.stdout


def _build_wheelhouse(pyproject_bytes: bytes, lock_bytes: bytes) -> tuple[BinaryIO, int]:
    project = tomllib.loads(pyproject_bytes.decode("utf-8"))
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    validate_uv_project(project, lock)
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()

    temporary = tempfile.TemporaryDirectory(prefix="solvyn-deps-")
    root = Path(temporary.name)
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
    try:
        exported = _run_tool(
            [
                "uv", "export", "--locked", "--all-groups", "--all-extras",
                "--no-emit-project", "--format", "requirements-txt",
            ],
            cwd=root,
            env=env,
        )
        if not exported or len(exported) > 1_048_576:
            raise DependencyBlockedError("Exported dependency set is empty or too large")
        requirements = exported
        (root / "requirements.txt").write_bytes(requirements)
        _run_tool(
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
        size = create_bundle_archive(wheel_dir, requirements, manifest, bundle)
        if size > _MAX_BUNDLE_BYTES:
            bundle.close()
            raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
        bundle.seek(0)
        return _OwnedBundle(bundle, temporary), size
    except Exception:
        temporary.cleanup()
        raise


class _OwnedBundle:
    def __init__(self, file: BinaryIO, temporary: tempfile.TemporaryDirectory[str]) -> None:
        self.file = file
        self.temporary = temporary

    def read(self, size: int) -> bytes:
        return self.file.read(size)

    def close(self) -> None:
        self.file.close()
        self.temporary.cleanup()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "SolvynDependencyFetcher"
    sys_version = ""

    def do_GET(self) -> None:
        if self.path == "/health":
            self._reply(200, b'{"status":"ok"}', "application/json")
        else:
            self._reply(404, b'{"detail":"Not found"}', "application/json")

    def do_POST(self) -> None:
        if self.path != "/v1/wheelhouse":
            self._reply(404, b'{"detail":"Not found"}', "application/json")
            return
        if not _slots.acquire(blocking=False):
            self._reply(429, b'{"detail":"Fetcher capacity is full"}', "application/json")
            return
        bundle: _OwnedBundle | None = None
        try:
            if self.headers.get("Transfer-Encoding") is not None:
                raise ValueError("Chunked request bodies are not accepted")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                raise ValueError("A bounded Content-Length is required")
            length = int(raw_length)
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                self._reply(413, b'{"detail":"Dependency request is too large"}', "application/json")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError("Request body was truncated")
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"pyproject", "uv_lock"}:
                raise ValueError("Only pyproject and uv.lock are accepted")
            if not isinstance(payload["pyproject"], str) or not isinstance(payload["uv_lock"], str):
                raise ValueError("Dependency fields must be base64 text")
            pyproject = base64.b64decode(payload["pyproject"], validate=True)
            lock = base64.b64decode(payload["uv_lock"], validate=True)
            if not pyproject or len(pyproject) > _MAX_PYPROJECT_BYTES or not lock or len(lock) > _MAX_LOCK_BYTES:
                self._reply(413, b'{"detail":"Dependency input exceeds its configured limit"}', "application/json")
                return
            bundle, size = _build_wheelhouse(pyproject, lock)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk := bundle.read(1024 * 1024):
                self.wfile.write(chunk)
            self.wfile.flush()
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            detail = str(exc) if isinstance(exc, DependencyBlockedError) else "Invalid dependency request"
            self._reply(422, json.dumps({"detail": detail}).encode(), "application/json")
        except Exception:
            self._reply(502, b'{"detail":"Dependency retrieval failed"}', "application/json")
        finally:
            if bundle is not None:
                bundle.close()
            _slots.release()

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Requests contain repository-controlled metadata; do not log it.
        return


def main() -> None:
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8002), _Handler)
    server.daemon_threads = True
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
