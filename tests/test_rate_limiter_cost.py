import uuid
from unittest.mock import AsyncMock

import pytest

from solvyn.agents.cost_tracker import CostTracker
from solvyn.agents.rate_limiter import RateLimiter


def test_rate_limiter_configure_and_check() -> None:
    limiter = RateLimiter()
    limiter.configure_llm("tenant-1", rate=100, burst=5)
    limiter.configure_agent("agent-a", rate=100, burst=3)

    # First 3 should pass for agent
    for _ in range(3):
        assert limiter.check_agent("agent-a") is True
    # 4th should fail
    assert limiter.check_agent("agent-a") is False

    # First 5 should pass for LLM
    for _ in range(5):
        assert limiter.check_llm("tenant-1") is True


def test_rate_limiter_unconfigured_passes() -> None:
    limiter = RateLimiter()
    assert limiter.check_llm("unknown") is True
    assert limiter.check_agent("unknown") is True


def test_rate_limiter_check_both() -> None:
    limiter = RateLimiter()
    limiter.configure_llm("t1", rate=100, burst=2)
    limiter.configure_agent("a1", rate=100, burst=2)
    assert limiter.check("t1", "a1") is True
    assert limiter.check("t1", "a1") is True
    # should fail because agent bucket is empty
    assert limiter.check("t1", "a1") is False


def test_rate_limiter_get_state() -> None:
    limiter = RateLimiter()
    limiter.configure_llm("t1", rate=10, burst=5)
    state = limiter.get_state()
    assert "llm:t1" in state
    assert state["llm:t1"]["burst"] == 5
    assert state["llm:t1"]["rate"] == 10


@pytest.mark.asyncio
async def test_cost_tracker_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.agents import cost_tracker as cost_tracker_module

    class Session:
        added = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def add(self, record):
            self.added = record

        async def commit(self):
            return None

    session = Session()
    monkeypatch.setattr(cost_tracker_module, "async_session_factory", lambda: session)
    monkeypatch.setattr(cost_tracker_module, "bind_tenant_scope", AsyncMock())
    tracker = CostTracker()
    tenant_id, session_id, task_id = (str(uuid.uuid4()) for _ in range(3))
    await tracker.record(
        tenant_id=tenant_id,
        session_id=session_id,
        task_id=task_id,
        agent_type="a",
        provider="openrouter",
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        cost=0.002,
    )
    assert session.added is not None
    assert session.added.tenant_id == uuid.UUID(tenant_id)
    assert session.added.session_id == uuid.UUID(session_id)
    assert session.added.task_id == uuid.UUID(task_id)
    assert session.added.total_tokens == 150


@pytest.mark.asyncio
async def test_cost_tracker_get_session_stats() -> None:
    tracker = CostTracker()
    tracker.get_session_stats = AsyncMock(return_value={
        "total_cost": 0.005,
        "total_tokens": 300,
        "agent_breakdown": {},
    })
    stats = await tracker.get_session_stats("s1")
    assert stats["total_cost"] == 0.005
    assert stats["total_tokens"] == 300


@pytest.mark.asyncio
async def test_cost_tracker_get_tenant_stats() -> None:
    tracker = CostTracker()
    tracker.get_tenant_stats = AsyncMock(return_value={
        "total_cost": 0.01,
        "total_tokens": 500,
    })
    stats = await tracker.get_tenant_stats("t1")
    assert stats["total_cost"] == 0.01
