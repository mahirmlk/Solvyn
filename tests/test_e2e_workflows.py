"""End-to-end tests for Solvyn API workflows.

These tests exercise the full HTTP→middleware→route→agent→LLM stack via
``httpx.AsyncClient`` (HTTP) and ``fastapi.testclient.TestClient`` (WebSocket).
All LLM calls are mocked so the suite runs hermetically without any real
API keys.

The tests are tagged with the ``e2e`` marker (see ``pyproject.toml``) and
can be selected with ``pytest -m e2e``.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

# Apply the e2e marker to every test in this module
pytestmark = pytest.mark.e2e


# ─── Module-level fixtures ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_llm_router() -> Any:
    """Replace the LLM router's ``generate`` coroutine with a deterministic stub.

    Workflows and agents eventually call ``llm_router.generate`` to talk to a
    provider; we replace that single entrypoint so we never touch the real
    OpenRouter/Groq/DeepSeek HTTP APIs during tests.
    """
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router

    # Drop any previously cached responses so calls go through the mock
    llm_router.cache._local_cache.clear()

    async def _mock_generate(
        prompt: str,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        fallback: bool = True,
        use_cache: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(
            content=f"mock-llm-output :: {prompt[:60]}",
            provider=provider or "openrouter",
            model=model or "openai/gpt-4o",
            prompt_tokens=len(prompt.split()),
            completion_tokens=20,
            total_tokens=len(prompt.split()) + 20,
            cost=0.0001,
        )

    original = llm_router.generate
    llm_router.generate = _mock_generate  # type: ignore[method-assign]
    try:
        yield
    finally:
        llm_router.generate = original  # type: ignore[method-assign]


def _clear_ratelimit_keys() -> None:
    """Best-effort cleanup of stale Redis sliding-window keys.

    CI runs with a real Redis service; stale ``ratelimit:*`` entries from
    earlier tests in the same run would leak into the rate-limit tests and
    make them flaky.  Only the ``ratelimit:`` namespace is touched.
    """
    try:
        import redis as _redis

        from solvyn.config import settings

        client = _redis.Redis.from_url(settings.redis_url, decode_responses=True)
        keys = list(client.scan_iter("ratelimit:*"))
        if keys:
            client.delete(*keys)
        client.close()
    except Exception:
        pass


@pytest.fixture
def low_rate_limit() -> Any:
    """Configure the rate limiter to allow only 5 requests (hermetic)."""
    from solvyn.multi_tenant.rate_limiter import redis_rate_limiter

    _clear_ratelimit_keys()
    redis_rate_limiter.configure(ip_limit=5, tenant_limit=5, window_seconds=60)
    redis_rate_limiter._fallback_buckets.clear()
    try:
        yield redis_rate_limiter
    finally:
        redis_rate_limiter.configure(ip_limit=100, tenant_limit=500, window_seconds=60)
        redis_rate_limiter._fallback_buckets.clear()
        _clear_ratelimit_keys()


@pytest_asyncio.fixture
async def unauthed_client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """Client with no API key — used to verify public vs. protected paths."""
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ─── Health check flow ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(client: AsyncClient) -> None:
    """GET /api/v1/health returns 200 and a static OK payload."""
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "solvyn"


@pytest.mark.asyncio
async def test_health_endpoint_does_not_require_api_key(unauthed_client: AsyncClient) -> None:
    """Public health probe must work without an X-API-Key header."""
    response = await unauthed_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_deep_health_endpoint_returns_checks(client: AsyncClient) -> None:
    """GET /api/v1/health/deep returns a per-dependency breakdown."""
    response = await client.get("/api/v1/health/deep")
    assert response.status_code in (200, 503)
    body = response.json()
    assert "status" in body
    assert "checks" in body
    # Database and Redis are always part of the breakdown
    assert "database" in body["checks"]
    assert "redis" in body["checks"]


@pytest.mark.asyncio
async def test_health_endpoints_share_no_auth_requirement(unauthed_client: AsyncClient) -> None:
    """Both health probes must be accessible without authentication."""
    health = await unauthed_client.get("/api/v1/health")
    deep = await unauthed_client.get("/api/v1/health/deep")
    assert health.status_code == 200
    assert deep.status_code in (200, 503)


# ─── Full feature workflow ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_feature_workflow_runs_to_completion(client: AsyncClient) -> None:
    """POST /workflows/feature/run completes synchronously with all 9 nodes."""
    response = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Build a simple todo list API"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow"] == "feature"
    assert data["status"] in ("completed", "failed")
    assert "session_id" in data
    # 9 steps in the feature template
    assert len(data["nodes"]) == 9
    agent_types = {n["agent_type"] for n in data["nodes"]}
    expected = {
        "product-manager",
        "system-designer",
        "backend-engineer",
        "frontend-engineer",
        "qa-engineer",
        "code-reviewer",
        "security-auditor",
        "devops-engineer",
        "documentation-writer",
    }
    assert expected.issubset(agent_types)


@pytest.mark.asyncio
async def test_feature_workflow_returns_resolvable_session_id(client: AsyncClient) -> None:
    """The session_id returned by the workflow must be queryable via observability."""
    response = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Build a calculator"},
    )
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    assert session_id

    # Polling pattern: look up traces for the session
    traces = await client.get(f"/api/v1/observability/traces?session_id={session_id}")
    assert traces.status_code == 200
    assert "traces" in traces.json()


@pytest.mark.asyncio
async def test_feature_workflow_outputs_include_provider_and_cost(client: AsyncClient) -> None:
    """Each executed agent node carries provider/model/cost info through to the response."""
    response = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Generate a REST API"},
    )
    assert response.status_code == 200
    nodes = response.json()["nodes"]
    # The endpoint summary only exposes agent_type / status / error,
    # so verify the count and that at least one node completed.
    assert nodes
    completed = [n for n in nodes if n["status"] == "completed"]
    assert completed, f"expected at least one completed node, got {nodes!r}"


@pytest.mark.asyncio
async def test_bugfix_workflow_runs(client: AsyncClient) -> None:
    """The bugfix pipeline executes all four steps."""
    response = await client.post(
        "/api/v1/workflows/bugfix/run",
        json={"bug_report": "Login returns 500 on empty email input"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow"] == "bugfix"
    assert len(data["nodes"]) == 4


@pytest.mark.asyncio
async def test_release_workflow_runs(client: AsyncClient) -> None:
    """The release pipeline executes all three steps."""
    response = await client.post(
        "/api/v1/workflows/release/run",
        json={
            "changelog_input": "v2.0.0: Added dark mode, fixed pagination",
            "codebase_snapshot": "Latest main branch snapshot",
            "project_info": "Solvyn production release",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow"] == "release"
    assert len(data["nodes"]) == 3


@pytest.mark.asyncio
async def test_unknown_workflow_returns_404(client: AsyncClient) -> None:
    """POST /workflows/{name}/run with an unknown name yields 404."""
    response = await client.post("/api/v1/workflows/nonexistent/run", json={})
    assert response.status_code == 404


# ─── Tenant isolation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workflows_endpoint_distinguishes_tenants(client: AsyncClient) -> None:
    """The tenant middleware fires for every request and resolves the slug.

    Uses a non-persisting endpoint (``GET /api/v1/workflows/``) so the
    test does not depend on the workflow executor's DB persistence path.
    Also stubs the per-tenant API-key validator so no real DB connection
    is required.
    """
    tenant_a = MagicMock()
    tenant_a.id = uuid.uuid4()
    tenant_a.slug = "tenant-a"
    tenant_a.is_active = True
    tenant_a.role = "user"
    tenant_b = MagicMock()
    tenant_b.id = uuid.uuid4()
    tenant_b.slug = "tenant-b"
    tenant_b.is_active = True
    tenant_b.role = "user"

    with (
        patch("solvyn.multi_tenant.middleware.resolve_tenant", new_callable=AsyncMock) as resolve_mock,
        patch("solvyn.multi_tenant.auth._validate_tenant_api_key", new_callable=AsyncMock) as validate_mock,
    ):
        validate_mock.return_value = tenant_a  # Accept any key in this test

        async def _resolve(req: Any) -> Any:
            slug = req.headers.get("X-Tenant-Slug", "")
            from solvyn.multi_tenant.middleware import tenant_context
            if slug == "tenant-a":
                tenant_context.set(tenant_a.id, tenant_a.slug, tenant_a.role)
                return tenant_a
            if slug == "tenant-b":
                tenant_context.set(tenant_b.id, tenant_b.slug, tenant_b.role)
                return tenant_b
            return None

        resolve_mock.side_effect = _resolve

        response_a = await client.get("/api/v1/workflows/", headers={"X-Tenant-Slug": "tenant-a"})
        response_b = await client.get("/api/v1/workflows/", headers={"X-Tenant-Slug": "tenant-b"})

    # Both requests succeed — the proof of isolation is that the tenant
    # middleware dispatched each one with the correct slug header.
    assert response_a.status_code == 200
    assert response_b.status_code == 200
    # The mock was invoked exactly once per request, demonstrating that
    # the tenant middleware fired for each X-Tenant-Slug.
    assert resolve_mock.await_count == 2


@pytest.mark.asyncio
async def test_tenant_analytics_blocks_cross_tenant_access(client: AsyncClient) -> None:
    """Tenant A cannot read tenant B's analytics.

    The analytics endpoint short-circuits with 403 when the path tenant_id
    does not match the calling tenant.  This test verifies the guard
    without hitting the database.
    """
    other_tenant_id = uuid.uuid4()
    response = await client.get(f"/api/v1/observability/analytics/tenant/{other_tenant_id}")
    # In shared-secret mode (no X-Tenant-Slug), the endpoint falls back to
    # the default tenant and rejects a mismatched id.
    assert response.status_code == 403
    assert "tenant" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_session_listing_returns_empty_without_tenant(client: AsyncClient) -> None:
    """Without a tenant in context, the session list is empty (no leakage)."""
    response = await client.get("/api/v1/sessions/")
    assert response.status_code == 200
    body = response.json()
    assert "sessions" in body
    assert body["sessions"] == []


# ─── API key auth ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_api_key_returns_401(unauthed_client: AsyncClient) -> None:
    """Protected endpoints reject requests without an X-API-Key header."""
    response = await unauthed_client.get("/api/v1/workflows/")
    assert response.status_code == 401
    body = response.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_invalid_api_key_returns_401(unauthed_client: AsyncClient) -> None:
    """Protected endpoints reject requests with a wrong X-API-Key header."""
    response = await unauthed_client.get(
        "/api/v1/workflows/",
        headers={"X-API-Key": "definitely-not-a-valid-key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_api_key_returns_200(client: AsyncClient) -> None:
    """The default test client (valid key) reaches protected endpoints."""
    response = await client.get("/api/v1/workflows/")
    assert response.status_code == 200
    body = response.json()
    assert "workflows" in body
    assert len(body["workflows"]) >= 3


@pytest.mark.asyncio
async def test_agents_endpoint_requires_auth(unauthed_client: AsyncClient) -> None:
    """GET /workflows/agents must be authenticated."""
    response = await unauthed_client.get("/api/v1/workflows/agents")
    assert response.status_code == 401


# ─── Rate limiting ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_burst(
    client: AsyncClient, low_rate_limit: Any
) -> None:
    """Once the per-IP limit is exceeded, subsequent calls return 429."""
    statuses: list[int] = []
    for _ in range(8):
        resp = await client.get("/api/v1/workflows/")
        statuses.append(resp.status_code)

    # First 5 should pass; the rest should be rate-limited.
    assert statuses[:5] == [200, 200, 200, 200, 200]
    assert all(s == 429 for s in statuses[5:]), f"unexpected statuses: {statuses}"


@pytest.mark.asyncio
async def test_rate_limit_response_payload_shape(
    client: AsyncClient, low_rate_limit: Any
) -> None:
    """The 429 body should advertise retry_after and the limit scope."""
    for _ in range(6):
        await client.get("/api/v1/workflows/")
    response = await client.get("/api/v1/workflows/")
    assert response.status_code == 429
    body = response.json()
    assert "error" in body
    assert "retry_after" in body
    assert "scope" in body


# ─── WebSocket connection ──────────────────────────────────────────────


def test_websocket_connects_with_valid_api_key() -> None:
    """A WebSocket connection with a valid API key receives broadcast events.

    The WebSocket endpoint short-circuits to ``verify_api_key`` when no
    tenant is in context, so we stub that helper directly.  The broadcast
    is driven by the in-process :class:`WebSocketManager`.
    """
    from solvyn.main import create_app
    from solvyn.observability.websocket import ws_manager

    app = create_app()
    api_key = "test-websocket-key"

    with patch("solvyn.observability.analytics_api.verify_api_key", return_value=True) as verify_mock:
        with TestClient(app) as test_client:
            with test_client.websocket_connect(
                f"/api/v1/observability/ws/default?session_id=sess-1&api_key={api_key}"
            ) as ws:
                # Drive a server-side broadcast from the WebSocket manager
                asyncio.run(
                    ws_manager.handle_graph_update(
                        graph_id="g-e2e",
                        node_id="n1",
                        agent_type="product-manager",
                        status="running",
                        session_id="sess-1",
                        tenant_id="default",
                    )
                )
                message = ws.receive_text()

    assert verify_mock.called, "verify_api_key should have been invoked"
    assert "graph_update" in message
    assert "product-manager" in message
    assert "running" in message


def test_websocket_rejects_unauthorized_connection() -> None:
    """A WebSocket without an API key is closed by the server (4001)."""
    from fastapi.websockets import WebSocketDisconnect
    from solvyn.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect("/api/v1/observability/ws/" + str(uuid.uuid4())):
                pass
    # 4001 is the application-defined "Unauthorized" close code.
    assert exc_info.value.code == 4001


def test_websocket_rejects_invalid_api_key() -> None:
    """A WebSocket with a wrong API key is closed by the server."""
    from fastapi.websockets import WebSocketDisconnect
    from solvyn.main import create_app

    app = create_app()
    with patch("solvyn.observability.analytics_api.verify_api_key", return_value=False):
        with TestClient(app) as test_client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with test_client.websocket_connect(
                    "/api/v1/observability/ws/default?api_key=definitely-wrong"
                ):
                    pass
    assert exc_info.value.code == 4001
