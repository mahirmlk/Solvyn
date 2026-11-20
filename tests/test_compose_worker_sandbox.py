from pathlib import Path

import yaml

COMPOSE_FILE = Path(__file__).parents[1] / "docker-compose.prod.yml"


def test_production_worker_has_authenticated_shared_sandbox_access() -> None:
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    services = compose["services"]
    worker = services["worker"]
    sandbox = services["sandbox"]

    assert worker["environment"]["ENABLE_WORKFLOW_SCHEDULER"] == "true"
    assert worker["environment"]["SANDBOX_WORKSPACE_ROOT"] == "/app/.solvyn/workspaces"
    assert worker["environment"]["SANDBOX_RUNTIME_URL"] == "http://sandbox:8001"
    assert worker["environment"]["SANDBOX_RUNTIME_TOKEN"] == (
        "${SANDBOX_RUNTIME_TOKEN:?Set SANDBOX_RUNTIME_TOKEN in .env.production}"
    )
    assert "sandbox_workspaces:/app/.solvyn/workspaces" in worker["volumes"]
    assert not any(volume.endswith(":ro") for volume in worker["volumes"])
    assert "sandbox-control" in worker["networks"]
    assert worker["depends_on"]["sandbox"]["condition"] == "service_healthy"

    assert "sandbox_workspaces:/app/.solvyn/workspaces" in sandbox["volumes"]
    assert "sandbox-control" in sandbox["networks"]
    assert not any("/var/run/docker.sock" in volume for volume in worker["volumes"])
