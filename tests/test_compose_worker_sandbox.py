from pathlib import Path

COMPOSE_FILE = Path(__file__).parents[1] / "docker-compose.prod.yml"


def _service_block(compose: str, service: str, next_service: str) -> str:
    start = compose.index(f"  {service}:\n")
    end = compose.index(f"  {next_service}:\n", start)
    return compose[start:end]


def test_production_worker_has_authenticated_shared_sandbox_access() -> None:
    compose = COMPOSE_FILE.read_text(encoding="utf-8")
    worker = _service_block(compose, "worker", "sandbox-job-image")
    sandbox = _service_block(compose, "sandbox", "frontend")

    assert "SANDBOX_WORKSPACE_ROOT: /app/.solvyn/workspaces" in worker
    assert "SANDBOX_RUNTIME_URL: http://sandbox:8001" in worker
    assert "SANDBOX_RUNTIME_TOKEN: ${SANDBOX_RUNTIME_TOKEN:?Set SANDBOX_RUNTIME_TOKEN in .env.production}" in worker
    assert "- sandbox_workspaces:/app/.solvyn/workspaces" in worker
    assert "- sandbox_workspaces:/app/.solvyn/workspaces:ro" not in worker
    assert "- sandbox-control" in worker
    assert "sandbox:\n        condition: service_healthy" in worker

    assert "- sandbox_workspaces:/app/.solvyn/workspaces" in sandbox
    assert "- sandbox-control" in sandbox
    assert "/var/run/docker.sock" not in worker
