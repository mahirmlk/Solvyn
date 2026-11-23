from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from solvyn.sandbox.dependency_client import DependencyBundle
from solvyn.sandbox.dependencies import DependencyEvidence
from solvyn.sandbox.npm_dependencies import NpmDependencyInputs, NpmPackage
from solvyn.sandbox.runtime import DockerSandboxRunner, SandboxRuntimeError
from solvyn.sandbox.workspaces import WorkspaceManager


@pytest.mark.asyncio
async def test_npm_profile_runs_offline_and_keeps_node_modules_out_of_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not shutil.which("docker"):
        pytest.skip("Docker CLI is unavailable")
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    package = {"name": "demo", "version": "1.0.0", "main": "index.js"}
    package_bytes = json.dumps(package).encode()
    project = {
        "name": "fixture", "version": "1.0.0", "dependencies": {"demo": "1.0.0"},
        "scripts": {"test": "node check.js"},
    }
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(b"fixture").digest()).decode()
    lock = {
        "name": "fixture", "version": "1.0.0", "lockfileVersion": 3,
        "packages": {
            "": {"name": "fixture", "version": "1.0.0", "dependencies": {"demo": "1.0.0"}},
            "node_modules/demo": {
                "name": "demo", "version": "1.0.0",
                "resolved": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
                "integrity": integrity,
            },
        },
    }
    manager.write_file(workspace, "package.json", json.dumps(project).encode())
    manager.write_file(workspace, "package-lock.json", json.dumps(lock).encode())
    manager.write_file(
        workspace,
        "check.js",
        b"const fs=require('fs'); const demo=require('demo'); if(demo!=='offline module') process.exit(2); "
        b"try { require('demo/injected.js'); process.exit(3); } catch (error) { "
        b"if (error.code !== 'MODULE_NOT_FOUND') throw error; } "
        b"fs.writeFileSync('profile-result.txt','passed');\n",
    )
    manager.write_file(workspace, "node_modules/demo/injected.js", b"module.exports='unverified injected file';\n")
    inputs = NpmDependencyInputs(
        json.dumps(project).encode(), json.dumps(lock).encode(),
        (NpmPackage("node_modules/demo", "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz", integrity, "demo", "1.0.0"),),
    )
    node_modules = tmp_path / "node_modules"
    demo_dir = node_modules / "demo"
    demo_dir.mkdir(parents=True)
    (demo_dir / "package.json").write_bytes(package_bytes)
    (demo_dir / "index.js").write_text("module.exports='offline module';\n", encoding="utf-8")
    temporary = tempfile.TemporaryDirectory(prefix="solvyn-test-npm-")
    identity = json.dumps({"node": "v22.23.3", "npm": "10.9.9", "platform": "linux", "architecture": "x64"}, sort_keys=True, separators=(",", ":"))
    evidence = DependencyEvidence(
        hashlib.sha256(inputs.lock).hexdigest(), "b" * 64, 1, 4096,
        hashlib.sha256(identity.encode()).hexdigest(), None, None, "npm",
        "v22.23.3", "10.9.9", "linux", "x64",
    )

    async def fake_fetch(_inputs: NpmDependencyInputs, *, base_url: str) -> DependencyBundle:
        assert base_url == "http://dependency-fetcher:8002"
        return DependencyBundle(temporary, None, None, evidence, node_modules)

    if not await DockerSandboxRunner(manager, image="solvyn-sandbox:local").check_ready():
        if os.getenv("SOLVYN_REQUIRE_SANDBOX_IMAGE") == "1":
            pytest.fail("The required sandbox Docker image is unavailable")
        pytest.skip("Sandbox Docker image is unavailable")
    monkeypatch.setattr("solvyn.sandbox.runtime.discover_dependency_inputs", lambda *_args, **_kwargs: inputs)
    monkeypatch.setattr("solvyn.sandbox.runtime.fetch_dependency_bundle", fake_fetch)
    runner = DockerSandboxRunner(manager, image="solvyn-sandbox:local")
    result = await runner.run(workspace, ["npm", "run", "test"], install_dependencies=True)
    assert result.exit_code == 0, result.stderr
    assert "passed" in (workspace.path / "profile-result.txt").read_text(encoding="utf-8")
    injected = workspace.path / "node_modules" / "demo" / "injected.js"
    assert injected.read_text(encoding="utf-8") == "module.exports='unverified injected file';\n"
    assert sorted(path.name for path in injected.parent.iterdir()) == ["injected.js"]
    assert result.dependency_evidence is not None
    assert result.dependency_evidence.ecosystem == "npm"
    assert result.dependency_evidence.runtime_version == "v22.23.3"
    assert result.dependency_evidence.installer_version == "10.9.9"
    assert manager.fingerprint(workspace) != ""
    project["dependencies"]["demo"] = "^2.0.0"
    manager.write_file(workspace, "package.json", json.dumps(project).encode())
    with pytest.raises(SandboxRuntimeError, match="does not satisfy"):
        await runner.run(workspace, ["npm", "run", "test"], install_dependencies=True)
    assert injected.read_text(encoding="utf-8") == "module.exports='unverified injected file';\n"
    temporary.cleanup()
