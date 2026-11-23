"""Fixed verification commands selected from a validated project manifest."""
from __future__ import annotations

from pathlib import Path

from solvyn.sandbox.dependencies import DependencyBlockedError, _read_regular_file
from solvyn.sandbox.npm_dependencies import _json_object, discover_npm_inputs

PYTHON_COMMANDS: dict[str, list[str]] = {
    "test": ["pytest", "-q"],
    "build": ["python", "-m", "compileall", "-q", "."],
    "lint": ["ruff", "check", "."],
    "typecheck": ["mypy", "."],
    "security": [
        "python", "/opt/solvyn/scan_secrets.py", "gitleaks", "dir",
        "--config", "/opt/solvyn/gitleaks.toml",
        "--gitleaks-ignore-path", "/opt/solvyn/gitleaks.ignore",
        "--ignore-gitleaks-allow", "--redact=100", "--no-banner", "--no-color",
        "--max-target-megabytes", "128", "--timeout", "90", "--exit-code", "1", "/workspace",
    ],
    "documentation": ["python", "/opt/solvyn/verify_markdown.py", "/workspace"],
}
_NPM_SCRIPTS = {"test": "test", "build": "build", "lint": "lint", "typecheck": "typecheck"}


def command_for_profile(workspace: Path, kind: str) -> list[str]:
    """Select a fixed profile command; npm projects must declare that exact script."""
    if kind not in PYTHON_COMMANDS:
        raise DependencyBlockedError("Unsupported verification profile")
    manifest_path = workspace / "package.json"
    lock_path = workspace / "package-lock.json"
    if not manifest_path.exists() and not lock_path.exists():
        return PYTHON_COMMANDS[kind].copy()
    if kind not in _NPM_SCRIPTS:
        return PYTHON_COMMANDS[kind].copy()
    if not manifest_path.is_file() or not lock_path.is_file():
        raise DependencyBlockedError("Root package.json and package-lock.json are required as a pair")
    discover_npm_inputs(workspace)
    manifest_bytes = _read_regular_file(manifest_path, 512 * 1024)
    manifest = _json_object(manifest_bytes, label="package.json")
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict) or not isinstance(scripts.get(_NPM_SCRIPTS[kind]), str):
        raise DependencyBlockedError(f"npm project must define a {kind} script")
    return ["npm", "run", _NPM_SCRIPTS[kind]]
