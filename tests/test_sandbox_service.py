"""Tests for the private sandbox supervisor's authenticated control API."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from solvyn.config import settings
from solvyn.sandbox.service import SandboxRunRequest, create_sandbox_service
from solvyn.sandbox.workspaces import WorkspaceManager


@pytest.mark.asyncio
async def test_sandbox_request_bounds_argv() -> None:
    valid = SandboxRunRequest(workspace_id=uuid.uuid4(), command=["python", "-m", "pytest"])
    assert valid.command == ["python", "-m", "pytest"]

    for command in ([""], ["nul\x00arg"], ["x" * 4097], ["x" * 4096] * 5):
        try:
            SandboxRunRequest(workspace_id=uuid.uuid4(), command=command)
        except ValueError:
            continue
        raise AssertionError(f"unsafe command accepted: {command!r}")


@pytest.mark.asyncio
async def test_private_sandbox_service_authenticates_and_dispatches(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "sandbox_workspace_root", str(tmp_path / "workspaces"))
    monkeypatch.setattr(settings, "sandbox_image", "solvyn-sandbox:test")
    service = create_sandbox_service("internal-test-token")

    class FakeRunner:
        async def run(self, workspace, command):
            return SimpleNamespace(
                run_id=uuid.uuid4(),
                command=tuple(command),
                exit_code=0,
                timed_out=False,
                stdout="ok\n",
                stderr="",
                output_truncated=False,
                duration_seconds=0.01,
            )

    manager = WorkspaceManager(Path(settings.sandbox_workspace_root))
    service.state.workspaces = manager
    service.state.runner = FakeRunner()
    service.state.capacity = asyncio.Semaphore(2)
    workspace = manager.create()
    manager.write_file(workspace, "main.py", b"pass")
    async with AsyncClient(transport=ASGITransport(app=service), base_url="http://test") as client:
        body = {"workspace_id": str(workspace.id), "command": ["python", "main.py"]}
        denied = await client.post("/v1/runs", json=body, headers={"Authorization": "Bearer wrong"})
        assert denied.status_code == 401

        result = await client.post(
            "/v1/runs",
            json=body,
            headers={"Authorization": "Bearer internal-test-token"},
        )
        assert result.status_code == 200
        assert result.json()["workspace_id"] == str(workspace.id)
        assert result.json()["result"]["stdout"] == "ok\n"
