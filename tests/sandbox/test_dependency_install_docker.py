from __future__ import annotations

import base64
import csv
import hashlib
import io
import os
import shutil
import socket
import tempfile
import zipfile
from pathlib import Path

import pytest

from solvyn.sandbox.dependencies import DependencyEvidence, DependencyInputs
from solvyn.sandbox.runtime import DockerSandboxRunner
from solvyn.sandbox.workspaces import WorkspaceManager


def _make_wheel(path: Path) -> str:
    import base64 as b64

    files = {
        "demo_pkg/__init__.py": b"VALUE = 'installed offline'\n",
        "demo_pkg-0.1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: demo-pkg\nVersion: 0.1.0\n\n",
        "demo_pkg-0.1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: solvyn-ci\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rows = []
    for filename, body in files.items():
        checksum = b64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
        rows.append((filename, f"sha256={checksum}", str(len(body))))
    rows.append(("demo_pkg-0.1.0.dist-info/RECORD", "", ""))
    output = io.StringIO()
    csv.writer(output, lineterminator="\n").writerows(rows)
    files["demo_pkg-0.1.0.dist-info/RECORD"] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, body in files.items():
            archive.writestr(filename, body)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_locked_wheel_installs_offline_without_changing_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is unavailable")
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    manager.write_file(workspace, "pyproject.toml", b"[project]\nname='demo'\nversion='0.1.0'\n")
    manager.write_file(workspace, "uv.lock", b"version = 1\n")
    before = manager.fingerprint(workspace)

    wheel_dir = tmp_path / "wheelhouse"
    wheel_dir.mkdir()
    wheel = wheel_dir / "demo_pkg-0.1.0-py3-none-any.whl"
    digest = _make_wheel(wheel)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(f"demo-pkg==0.1.0 --hash=sha256:{digest}\n", encoding="utf-8")
    temporary = tempfile.TemporaryDirectory(prefix="solvyn-test-bundle-")
    evidence = DependencyEvidence("a" * 64, "b" * 64, 1, wheel.stat().st_size)

    async def fake_fetch(_inputs: DependencyInputs, *, base_url: str):
        from solvyn.sandbox.dependency_client import DependencyBundle

        assert base_url == "http://dependency-fetcher:8002"
        return DependencyBundle(temporary, wheel_dir, requirements, evidence)

    monkeypatch.setattr("solvyn.sandbox.runtime.discover_dependency_inputs", lambda *_args, **_kwargs: DependencyInputs(b"project", b"lock"))
    monkeypatch.setattr("solvyn.sandbox.runtime.fetch_dependency_bundle", fake_fetch)
    runner = DockerSandboxRunner(manager, image="solvyn-sandbox:local")
    if not await runner.check_ready():
        if os.getenv("SOLVYN_REQUIRE_SANDBOX_IMAGE") == "1":
            pytest.fail("The required sandbox Docker image is unavailable")
        pytest.skip("Sandbox Docker image is unavailable")

    code = """import demo_pkg
import socket
print(demo_pkg.VALUE)
try:
    socket.create_connection(("1.1.1.1", 443), timeout=.2)
except OSError:
    print("network disabled")
else:
    raise SystemExit("network unexpectedly available")
"""
    result = await runner.run(workspace, ["python", "-c", code], install_dependencies=True)
    assert result.exit_code == 0, result.stderr
    assert "installed offline" in result.stdout
    assert "network disabled" in result.stdout
    assert result.dependency_evidence == evidence
    assert manager.fingerprint(workspace) == before
