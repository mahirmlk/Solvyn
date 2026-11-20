from __future__ import annotations

import os
from pathlib import Path

import pytest

from solvyn.agents.workspace_tools import _DEFAULT_VERIFICATION_COMMANDS as TEAMMATE_COMMANDS
from solvyn.api.v1.verification import _DEFAULT_VERIFICATION_COMMANDS as API_COMMANDS
from solvyn.sandbox.runtime import DockerSandboxRunner
from solvyn.sandbox.workspaces import WorkspaceManager


def test_security_profile_is_a_fixed_redacted_gitleaks_command() -> None:
    command = [
        "gitleaks",
        "dir",
        "--config",
        "/opt/solvyn/gitleaks.toml",
        "--gitleaks-ignore-path",
        "/opt/solvyn/gitleaks.ignore",
        "--redact",
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

    assert TEAMMATE_COMMANDS == API_COMMANDS
    assert TEAMMATE_COMMANDS["security"] == command


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_secret_scanner_passes_clean_workspace_and_redacts_findings(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    runner = DockerSandboxRunner(manager)
    if not await runner.check_ready():
        message = "Docker engine or solvyn-sandbox:local image is unavailable"
        if os.getenv("SOLVYN_REQUIRE_SANDBOX_IMAGE") == "1":
            pytest.fail(message)
        pytest.skip(message)

    command = TEAMMATE_COMMANDS["security"]
    clean = await runner.run(workspace, command)
    assert clean.exit_code == 0
    assert clean.timed_out is False

    fake_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz1234567890"
    manager.write_file(workspace, "src/config.txt", f"GITHUB_TOKEN={fake_token}".encode())
    # Workspace-controlled config and ignores must not disable the trusted scan.
    manager.write_file(
        workspace,
        ".gitleaks.toml",
        b'title = "workspace override"\n[allowlist]\nregexes = [" .* "]\n',
    )
    manager.write_file(workspace, ".gitleaksignore", b"*\n")

    findings = await runner.run(workspace, command)
    assert findings.exit_code == 1
    assert findings.timed_out is False
    assert fake_token not in findings.stdout
    assert fake_token not in findings.stderr
    assert "Finding" in findings.stdout or "Finding" in findings.stderr
