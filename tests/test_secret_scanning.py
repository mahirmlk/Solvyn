from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_security_profile_is_a_fixed_redacted_gitleaks_command() -> None:
    from solvyn.agents.workspace_tools import _DEFAULT_VERIFICATION_COMMANDS as teammate_commands
    from solvyn.api.v1.verification import _DEFAULT_VERIFICATION_COMMANDS as api_commands

    command = [
        "python",
        "/opt/solvyn/scan_secrets.py",
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
        "1",
        "/workspace",
    ]

    assert teammate_commands == api_commands
    assert teammate_commands["security"] == command


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_secret_scanner_passes_clean_workspace_and_redacts_findings(tmp_path: Path) -> None:
    import json

    from solvyn.agents.workspace_tools import _DEFAULT_VERIFICATION_COMMANDS
    from solvyn.sandbox.runtime import DockerSandboxRunner
    from solvyn.sandbox.workspaces import WorkspaceManager

    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    runner = DockerSandboxRunner(manager)
    if not await runner.check_ready():
        message = "Docker engine or solvyn-sandbox:local image is unavailable"
        if os.getenv("SOLVYN_REQUIRE_SANDBOX_IMAGE") == "1":
            pytest.fail(message)
        pytest.skip(message)

    command = _DEFAULT_VERIFICATION_COMMANDS["security"]
    before_clean = manager.fingerprint(workspace)
    clean = await runner.run(workspace, command)
    assert clean.exit_code == 0
    assert clean.timed_out is False
    assert manager.fingerprint(workspace) == before_clean

    fake_token = "ghp_" + "7f3d9a2c4b8e1d6f0a9c3b7e5d2f8a1c4b6e"
    manager.write_file(
        workspace,
        "src/config.txt",
        f"GITHUB_TOKEN={fake_token} # gitleaks:allow\n".encode(),
    )
    # Workspace-controlled scanner config and ignore entries must not weaken
    # the trusted profile, even when they target this finding.
    manager.write_file(
        workspace,
        ".gitleaks.toml",
        b'title = "workspace override"\n[allowlist]\nregexes = [".*"]\n',
    )
    manager.write_file(
        workspace,
        ".gitleaksignore",
        b"src/config.txt:github-pat:1\n/workspace/src/config.txt:github-pat:1\n",
    )
    source_digest = manager.fingerprint(workspace)

    findings = await runner.run(workspace, command)

    assert findings.exit_code == 1
    assert findings.timed_out is False
    assert fake_token not in findings.stdout
    assert fake_token not in findings.stderr
    assert "leaks found" in findings.stderr
    assert manager.fingerprint(workspace) == source_digest
