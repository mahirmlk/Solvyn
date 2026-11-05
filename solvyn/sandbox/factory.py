"""Build the configured sandbox runner for one managed workspace manager."""

from __future__ import annotations

from solvyn.config import settings
from solvyn.sandbox.remote import RemoteSandboxRunner
from solvyn.sandbox.runtime import DockerSandboxRunner, SandboxRuntimeError
from solvyn.sandbox.workspaces import WorkspaceManager


def create_sandbox_runner(workspaces: WorkspaceManager) -> DockerSandboxRunner | RemoteSandboxRunner:
    if settings.sandbox_runtime_url:
        if not settings.sandbox_runtime_token:
            raise SandboxRuntimeError("Remote sandbox runtime is missing its service credential")
        return RemoteSandboxRunner(
            workspaces,
            base_url=settings.sandbox_runtime_url,
            token=settings.sandbox_runtime_token,
        )
    return DockerSandboxRunner(workspaces, image=settings.sandbox_image)
