from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_health_endpoint(client) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["service"] == "solvyn"


@pytest.mark.asyncio
async def test_deep_health_endpoint(client) -> None:
    response = await client.get("/api/v1/health/deep")
    assert response.status_code in (200, 503)
    data = response.json()
    assert "status" in data
    assert "checks" in data


@pytest.mark.asyncio
async def test_list_workflows(client) -> None:
    response = await client.get("/api/v1/workflows/")
    assert response.status_code == 200
    data = response.json()
    assert "workflows" in data
    assert len(data["workflows"]) >= 3


@pytest.mark.asyncio
async def test_get_workflow(client) -> None:
    response = await client.get("/api/v1/workflows/feature")
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "feature"
    assert "steps" in data


@pytest.mark.asyncio
async def test_get_workflow_not_found(client) -> None:
    response = await client.get("/api/v1/workflows/nonexistent")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_list_agents_fixed_route(client) -> None:
    response = await client.get("/api/v1/workflows/agents")
    assert response.status_code == 200
    assert any(agent["type"] == "product-manager" for agent in response.json()["agents"])


@pytest.mark.asyncio
async def test_generate_tasks_accepts_json_body(client) -> None:
    generated = {"tasks": [{"title": "Implement API", "priority": "high"}]}
    with patch("solvyn.api.v1.workflows.TaskGenerator.generate_from_spec", AsyncMock(return_value=generated)):
        response = await client.post("/api/v1/workflows/generate-tasks", json={"specification": "Build an API"})

    assert response.status_code == 200
    assert response.json()["tasks"] == generated["tasks"]


@pytest.mark.asyncio
async def test_metrics_endpoint(client) -> None:
    response = await client.get("/metrics/")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_traces_endpoint(client) -> None:
    response = await client.get("/api/v1/observability/traces")
    assert response.status_code == 200
    data = response.json()
    assert "traces" in data


@pytest.mark.asyncio
async def test_deep_health_is_degraded_when_redis_is_disconnected(client) -> None:
    from solvyn.message_bus.redis import message_bus

    message_bus.redis = None
    response = await client.get("/api/v1/health/deep")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
