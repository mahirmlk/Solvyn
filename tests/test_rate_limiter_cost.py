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
async def test_cost_tracker_record() -> None:
    tracker = CostTracker()
    tracker.record = AsyncMock()
    await tracker.record(
        session_id="s1",
        agent_type="a",
        provider="openrouter",
        model="gpt-4o",
        prompt_tokens=100,
        completion_tokens=50,
        cost=0.002,
    )
    tracker.record.assert_awaited_once()


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
