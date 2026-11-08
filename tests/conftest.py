from collections.abc import AsyncGenerator, Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from solvyn.agents.schema import AgentCapability, AgentMessage, AgentMetadata, Checkpoint
from solvyn.config import Settings
from solvyn.multi_tenant.middleware import tenant_context


@pytest.fixture(autouse=True)
def test_settings(monkeypatch) -> Generator[Settings, None, None]:
    """Ensure Settings() can be constructed in tests.

    Production Settings now fail fast when API_KEY_SECRET / JWT_SECRET are
    missing (P0-002). Tests set harmless dummy values via monkeypatch so the
    module-level `settings = Settings()` never raises.
    """
    monkeypatch.setenv("ALLOW_INSECURE_DEV", "true")
    monkeypatch.setenv("API_KEY_SECRET", "test-api-key-secret")
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret")
    with patch("solvyn.config.settings") as mock:
        mock.database_url = "sqlite+aiosqlite:///:memory:"
        mock.redis_url = "redis://localhost:6379/0"
        mock.openrouter_api_key = "sk-test"
        mock.groq_api_key = "gsk-test"
        mock.deepseek_api_key = "sk-ds-test"
        mock.llm_default_provider = "openrouter"
        mock.llm_default_model = "openai/gpt-4o"
        mock.default_tenant_id = "test-tenant"
        mock.host = "0.0.0.0"
        mock.port = 8000
        mock.log_level = "DEBUG"
        mock.json_logging = False
        mock.api_key_secret = "change-me-to-a-random-secret"
        mock.jwt_secret = "test-jwt-secret"
        mock.jwt_algorithm = "HS256"
        mock.access_token_ttl_minutes = 15
        mock.refresh_token_ttl_days = 30
        mock.allow_insecure_dev = False
        mock.db_pool_size = 20
        mock.db_max_overflow = 10
        mock.db_pool_recycle_seconds = 1800
        # Many API tests share one loopback client IP during this suite.
        # Dedicated rate-limit tests configure their own low caps explicitly.
        mock.rate_limit_per_ip = 5000
        # E2E cases share the test tenant within the minute window; dedicated
        # rate-limit tests configure their own low caps explicitly.
        mock.rate_limit_per_tenant = 5000
        mock.rate_limit_window = 60
        mock.allowed_origins = ["http://localhost:3000"]
        mock.sandbox_workspace_root = ".solvyn/test-workspaces"
        mock.sandbox_image = "solvyn-sandbox:local"
        mock.sandbox_runtime_url = ""
        mock.sandbox_runtime_token = ""
        mock.enable_workflow_scheduler = False
        mock.otel_enabled = False
        mock.otlp_endpoint = None
        from solvyn import main

        monkeypatch.setattr(main.settings, "enable_workflow_scheduler", False)
        yield mock


@pytest.fixture
def tenant_ctx() -> Generator[None, None, None]:
    import uuid
    tenant_context.set(uuid.uuid4(), "test-tenant")
    yield
    tenant_context.clear()


@pytest.fixture
def app() -> Any:
    from solvyn.main import create_app
    return create_app()


@pytest_asyncio.fixture(autouse=True)
async def mock_redis_connection() -> AsyncGenerator[None, None]:
    from solvyn.message_bus.redis import message_bus
    message_bus.redis = AsyncMock()
    message_bus.pubsub = AsyncMock()
    yield


@pytest_asyncio.fixture(autouse=True)
async def mock_db_session() -> AsyncGenerator[None, None]:
    from solvyn.db.session import async_session_factory
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session.__aexit__.return_value = None
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute.return_value = mock_result
    with patch.object(async_session_factory, "__call__", return_value=mock_session):
        yield


@pytest_asyncio.fixture
async def client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={"X-API-Key": "change-me-to-a-random-secret"},
        ) as ac:
            yield ac


@pytest.fixture
def sample_agent_metadata() -> AgentMetadata:
    return AgentMetadata(
        agent_type="test-agent",
        name="Test Agent",
        description="A test agent",
        capabilities=[AgentCapability.CODE_GENERATION, AgentCapability.CODE_REVIEW],
        version="1.0.0",
    )


@pytest.fixture
def sample_checkpoint() -> Checkpoint:
    return Checkpoint(
        id="cp-1",
        session_id="session-1",
        agent_type="test-agent",
        state={"step": "done", "result": "ok"},
        status="completed",
    )


@pytest.fixture
def sample_message() -> AgentMessage:
    return AgentMessage(
        id="msg-1",
        from_agent="agent-a",
        to_agent="agent-b",
        session_id="s1",
        tenant_id="t1",
        type="request",
        content={"text": "Hello"},
    )


@pytest.fixture
def mock_llm_provider() -> Generator[None, None, None]:
    from solvyn.llm.router import llm_router
    mock_provider = AsyncMock()
    mock_provider.generate.return_value = {
        "content": "Mock response",
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    llm_router.providers["test"] = mock_provider
    yield
    llm_router.providers.pop("test", None)
