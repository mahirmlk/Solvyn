"""Private API for the trusted sandbox supervisor process.

This service owns Docker Engine access. Untrusted commands run only in child
containers created by DockerSandboxRunner and never receive the engine socket.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from solvyn.config import settings
from solvyn.sandbox.runtime import DockerSandboxRunner
from solvyn.sandbox.workspaces import WorkspaceError, WorkspaceManager

_MAX_COMMAND_ARG_BYTES = 4096
_MAX_COMMAND_TOTAL_BYTES = 16_384


class SandboxRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: uuid.UUID
    command: list[str] = Field(min_length=1, max_length=128)

    @field_validator("command")
    @classmethod
    def validate_arguments(cls, command: list[str]) -> list[str]:
        total_bytes = 0
        for argument in command:
            if not argument or "\x00" in argument:
                raise ValueError("Command arguments must be non-empty and contain no NUL bytes")
            argument_size = len(argument.encode("utf-8"))
            if argument_size > _MAX_COMMAND_ARG_BYTES:
                raise ValueError("Command argument exceeds the configured limit")
            total_bytes += argument_size
        if total_bytes > _MAX_COMMAND_TOTAL_BYTES:
            raise ValueError("Command arguments exceed the configured limit")
        return command


def create_sandbox_service(token: str | None = None) -> FastAPI:
    service_token = settings.sandbox_runtime_token if token is None else token

    @asynccontextmanager
    async def lifespan(service: FastAPI) -> AsyncIterator[None]:
        if not service_token:
            raise RuntimeError("SANDBOX_RUNTIME_TOKEN is required by the sandbox supervisor")
        manager = WorkspaceManager(Path(settings.sandbox_workspace_root))
        service.state.workspaces = manager
        service.state.runner = DockerSandboxRunner(manager, image=settings.sandbox_image)
        service.state.capacity = asyncio.Semaphore(2)
        yield

    service = FastAPI(title="Solvyn Sandbox Supervisor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @service.get("/health")
    async def health() -> dict[str, str]:
        if not await service.state.runner.check_ready():
            raise HTTPException(status_code=503, detail="Sandbox runtime is not ready")
        return {"status": "ok"}

    @service.post("/v1/runs")
    async def run_sandbox(
        payload: SandboxRunRequest,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        scheme, _, credential = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(credential, service_token):
            raise HTTPException(status_code=401, detail="Invalid sandbox service credential")
        capacity = service.state.capacity
        try:
            await asyncio.wait_for(capacity.acquire(), timeout=0.1)
        except TimeoutError as exc:
            raise HTTPException(status_code=503, detail="Sandbox execution capacity is full") from exc
        try:
            workspace = service.state.workspaces.get(payload.workspace_id)
            result = await service.state.runner.run(workspace, payload.command)
        except (WorkspaceError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid sandbox workspace or command") from exc
        except Exception as exc:
            # Do not return Docker diagnostics or host paths across the service boundary.
            raise HTTPException(status_code=502, detail="Sandbox execution failed") from exc
        finally:
            capacity.release()
        return {
            "workspace_id": str(payload.workspace_id),
            "result": {
                "run_id": str(result.run_id),
                "command": list(result.command),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "output_truncated": result.output_truncated,
                "duration_seconds": result.duration_seconds,
            },
        }

    return service


app = create_sandbox_service()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("SANDBOX_RUNTIME_PORT", "8001")))
