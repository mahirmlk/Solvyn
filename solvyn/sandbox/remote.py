"""Authenticated client for the separately trusted sandbox supervisor."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from solvyn.sandbox.dependencies import DependencyEvidence
from solvyn.sandbox.runtime import SandboxRuntimeError
from solvyn.sandbox.workspaces import ManagedWorkspace, WorkspaceManager


@dataclass(frozen=True)
class RemoteSandboxResult:
    run_id: str
    command: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str
    output_truncated: bool
    duration_seconds: float
    dependency_evidence: DependencyEvidence | None = None


class RemoteSandboxRunner:
    """Dispatch a workspace ID to a trusted supervisor sharing the workspace volume."""

    def __init__(
        self,
        workspaces: WorkspaceManager,
        *,
        base_url: str,
        token: str,
        timeout_seconds: float = 330.0,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "http" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Sandbox runtime URL must be an internal HTTP service URL")
        if not token or "\x00" in token:
            raise ValueError("Sandbox runtime token is required")
        if not 1 <= timeout_seconds <= 360:
            raise ValueError("Remote sandbox timeout must be between 1 and 360 seconds")
        self.workspaces = workspaces
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    async def run(self, workspace: ManagedWorkspace, command: Sequence[str], *, install_dependencies: bool = False) -> RemoteSandboxResult:
        self.workspaces.validate_tree(workspace)
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds, connect=5.0),
            ) as client:
                response = await client.post(
                    "/v1/runs",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={
                        "workspace_id": str(workspace.id),
                        "command": list(command),
                        "install_dependencies": install_dependencies,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SandboxRuntimeError("Trusted sandbox runtime is unavailable or returned an invalid response") from exc

        if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
            raise SandboxRuntimeError("Trusted sandbox runtime returned an invalid response")
        result = payload["result"]
        if payload.get("workspace_id") != str(workspace.id):
            raise SandboxRuntimeError("Trusted sandbox runtime returned a mismatched workspace")
        if not isinstance(result.get("command"), list) or tuple(result["command"]) != tuple(command):
            raise SandboxRuntimeError("Trusted sandbox runtime returned a mismatched command")
        if not isinstance(result.get("run_id"), str) or not result["run_id"]:
            raise SandboxRuntimeError("Trusted sandbox runtime returned an invalid run identifier")
        if result.get("exit_code") is not None and not isinstance(result["exit_code"], int):
            raise SandboxRuntimeError("Trusted sandbox runtime returned an invalid exit code")
        if not isinstance(result.get("timed_out"), bool) or not isinstance(result.get("output_truncated"), bool):
            raise SandboxRuntimeError("Trusted sandbox runtime returned invalid execution flags")
        if not isinstance(result.get("stdout"), str) or not isinstance(result.get("stderr"), str):
            raise SandboxRuntimeError("Trusted sandbox runtime returned invalid output")
        if len(result["stdout"]) > 1024 * 1024 or len(result["stderr"]) > 1024 * 1024:
            raise SandboxRuntimeError("Trusted sandbox runtime returned oversized output")
        evidence_payload = result.get("dependency_evidence")
        evidence: DependencyEvidence | None = None
        if evidence_payload is not None:
            if (
                not isinstance(evidence_payload, dict)
                or set(evidence_payload) != {"lock_sha256", "wheelhouse_sha256", "package_count", "wheelhouse_bytes", "runtime_fingerprint", "uv_version", "pip_version"}
                or any(not isinstance(evidence_payload.get(key), str) or len(evidence_payload[key]) != 64 for key in ("lock_sha256", "wheelhouse_sha256", "runtime_fingerprint"))
                or any(char not in "0123456789abcdef" for key in ("lock_sha256", "wheelhouse_sha256") for char in evidence_payload[key])
                or not isinstance(evidence_payload.get("package_count"), int)
                or not isinstance(evidence_payload.get("wheelhouse_bytes"), int)
                or not 0 <= evidence_payload["package_count"] <= 500
                or not 0 <= evidence_payload["wheelhouse_bytes"] <= 384 * 1024 * 1024
                or evidence_payload.get("uv_version") != "0.13.7"
                or evidence_payload.get("pip_version") != "26.3"
            ):
                raise SandboxRuntimeError("Trusted sandbox runtime returned invalid dependency evidence")
            evidence = DependencyEvidence(
                evidence_payload["lock_sha256"],
                evidence_payload["wheelhouse_sha256"],
                evidence_payload["package_count"],
                evidence_payload["wheelhouse_bytes"],
                evidence_payload["runtime_fingerprint"],
                evidence_payload["uv_version"],
                evidence_payload["pip_version"],
            )
        if install_dependencies and evidence is None:
            raise SandboxRuntimeError("Trusted sandbox runtime omitted dependency evidence")
        self.workspaces.validate_tree(workspace)

        return RemoteSandboxResult(
            run_id=result["run_id"],
            command=tuple(result["command"]),
            exit_code=result["exit_code"],
            timed_out=result["timed_out"],
            stdout=result["stdout"],
            stderr=result["stderr"],
            output_truncated=result["output_truncated"],
            duration_seconds=float(result.get("duration_seconds", 0.0)),
            dependency_evidence=evidence,
        )
