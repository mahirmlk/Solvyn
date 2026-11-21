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
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import tomllib
from pathlib import Path
from typing import BinaryIO, cast

from solvyn.sandbox.dependencies import (
    _MAX_BUNDLE_BYTES,
    _MAX_LOCK_BYTES,
    _MAX_PYPROJECT_BYTES,
    _PIP_VERSION,
    _UV_VERSION,
    DependencyBlockedError,
    build_bundle_manifest,
    create_bundle_archive,
    validate_uv_project,
)

_MAX_REQUEST_BYTES = 3 * 1024 * 1024
_PROCESS_TIMEOUT = 180
_MAX_ACTIVE_REQUESTS = 1
_PROXY = "http://dependency-proxy:3128"
_slots = threading.BoundedSemaphore(_MAX_ACTIVE_REQUESTS)


def _run_tool(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    capture_stdout: bool = True,
    output_limit: int = 1_048_576,
) -> bytes:
    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        raise DependencyBlockedError("Pinned dependency tooling is unavailable") from exc
    if not capture_stdout:
        try:
            return_code = process.wait(timeout=_PROCESS_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise DependencyBlockedError("Dependency resolution exceeded its time limit") from exc
        if return_code != 0:
            raise DependencyBlockedError("Locked dependencies could not be exported or fetched safely")
        return b""
    assert process.stdout is not None
    output = bytearray()
    deadline = time.monotonic() + _PROCESS_TIMEOUT
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        stdout_fd = process.stdout.fileno()
        try:
            while process.poll() is None or selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                for key, _ in selector.select(min(0.25, remaining)):
                    chunk = os.read(stdout_fd, min(64 * 1024, output_limit + 1 - len(output)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        raise DependencyBlockedError("Dependency tooling exceeded its output limit")
        except (TimeoutError, DependencyBlockedError) as exc:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            if isinstance(exc, DependencyBlockedError):
                raise
            raise DependencyBlockedError("Dependency resolution exceeded its time limit") from exc
    if process.wait() != 0:
        raise DependencyBlockedError("Locked dependencies could not be exported or fetched safely")
    return bytes(output)

def _toml_key(value: str) -> str:
    if value and all(character.isalnum() or character in "_-" for character in value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        rendered: list[str] = []
        for item in value:
            if isinstance(item, str):
                rendered.append(_toml_value(item))
            elif isinstance(item, dict) and set(item) == {"include-group"} and isinstance(item["include-group"], str):
                rendered.append("{ include-group = " + _toml_value(item["include-group"]) + " }")
            else:
                raise DependencyBlockedError("Dependency groups contain unsupported metadata")
        return "[" + ", ".join(rendered) + "]"
    raise DependencyBlockedError("Project dependency metadata has an unsupported value")


def _render_export_pyproject(project_data: dict[str, object]) -> bytes:
    """Render only static dependency declarations; omit all build/plugin configuration."""
    project = project_data.get("project")
    if not isinstance(project, dict) or project.get("dynamic"):
        raise DependencyBlockedError("Dynamic project metadata is unsupported by the dependency fetcher")
    required = ("name", "version", "requires-python")
    if any(not isinstance(project.get(key), str) for key in required):
        raise DependencyBlockedError("Project name, version, and requires-python must be static")

    lines = ["[project]"]
    for key in required:
        lines.append(f"{key} = {_toml_value(project[key])}")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(item, str) for item in dependencies):
        raise DependencyBlockedError("Project dependencies must be static PEP 508 strings")
    lines.append("dependencies = " + _toml_value(dependencies))

    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise DependencyBlockedError("Optional dependencies must be a static table")
    if optional:
        lines.extend(["", "[project.optional-dependencies]"])
        for key, value in sorted(optional.items()):
            if not isinstance(key, str) or not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise DependencyBlockedError("Optional dependencies must be static PEP 508 strings")
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")

    groups = project_data.get("dependency-groups", {})
    if not isinstance(groups, dict):
        raise DependencyBlockedError("Dependency groups must be a static table")
    if groups:
        lines.extend(["", "[dependency-groups]"])
        for key, value in sorted(groups.items()):
            if not isinstance(key, str):
                raise DependencyBlockedError("Dependency group names must be strings")
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _build_wheelhouse(pyproject_bytes: bytes, lock_bytes: bytes) -> tuple[_OwnedBundle, int]:
    project = tomllib.loads(pyproject_bytes.decode("utf-8"))
    lock = tomllib.loads(lock_bytes.decode("utf-8"))
    validate_uv_project(project, lock)
    lock_sha = hashlib.sha256(lock_bytes).hexdigest()

    temporary = tempfile.TemporaryDirectory(prefix="solvyn-deps-")
    root = Path(temporary.name)
    (root / "pyproject.toml").write_bytes(_render_export_pyproject(project))
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
        "PIP_NO_CACHE_DIR": "1",
        "HTTPS_PROXY": _PROXY,
        "HTTP_PROXY": _PROXY,
        "https_proxy": _PROXY,
        "http_proxy": _PROXY,
        "NO_PROXY": "",
        "no_proxy": "",
    }
    try:
        uv_version_output = _run_tool(["uv", "--version"], cwd=root, env=env).decode("ascii").strip()
        pip_version = _run_tool(["python", "-m", "pip", "--version"], cwd=root, env=env).decode("ascii").strip()
        if uv_version_output.split()[:2] != ["uv", _UV_VERSION] or not pip_version.startswith(f"pip {_PIP_VERSION} "):
            raise DependencyBlockedError("Dependency fetcher toolchain version is unsupported")
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
                "--no-deps", "--no-cache-dir", "--dest", str(wheel_dir), "-r", str(root / "requirements.txt"),
            ],
            cwd=root,
            env=env,
            capture_stdout=False,
        )
        manifest, _ = build_bundle_manifest(wheel_dir, lock_sha256=lock_sha, requirements=requirements)
        manifest["toolchain"] = {"uv": _UV_VERSION, "pip": _PIP_VERSION}
        bundle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        size = create_bundle_archive(wheel_dir, requirements, manifest, cast(BinaryIO, bundle))
        if size > _MAX_BUNDLE_BYTES:
            bundle.close()
            raise DependencyBlockedError("Dependency bundle exceeds its transfer limit")
        bundle.seek(0)
        return _OwnedBundle(cast(BinaryIO, bundle), temporary), size
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
        owned_bundle: _OwnedBundle | None = None
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
            owned_bundle, size = _build_wheelhouse(pyproject, lock)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-tar")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while chunk := owned_bundle.read(1024 * 1024):
                self.wfile.write(chunk)
            self.wfile.flush()
        except (ValueError, TypeError, binascii.Error, json.JSONDecodeError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
            detail = str(exc) if isinstance(exc, DependencyBlockedError) else "Invalid dependency request"
            self._reply(422, json.dumps({"detail": detail}).encode(), "application/json")
        except Exception:
            self._reply(502, b'{"detail":"Dependency retrieval failed"}', "application/json")
        finally:
            if owned_bundle is not None:
                owned_bundle.close()
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
