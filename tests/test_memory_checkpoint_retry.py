from unittest.mock import AsyncMock

import pytest

from solvyn.agents.retry import DeadLetterHandler, RetryHandler


@pytest.mark.asyncio
async def test_retry_handler_execute_with_retry_success() -> None:
    handler = RetryHandler(max_retries=3, base_delay=0.01)

    async def success() -> str:
        return "success"

    result = await handler.execute_with_retry(success)
    assert result == "success"


@pytest.mark.asyncio
async def test_retry_handler_retry_then_succeed() -> None:
    call_count = 0

    async def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise ValueError("temporary")
        return "ok"

    handler = RetryHandler(max_retries=3, base_delay=0.01)
    result = await handler.execute_with_retry(flaky, retryable_exceptions=(ValueError,))
    assert result == "ok"
    assert call_count == 2


@pytest.mark.asyncio
async def test_retry_handler_exhausted() -> None:
    handler = RetryHandler(max_retries=2, base_delay=0.01)

    async def always_fails() -> None:
        raise ValueError("always")

    with pytest.raises(ValueError, match="always"):
        await handler.execute_with_retry(always_fails)


@pytest.mark.asyncio
async def test_dead_letter_handler_handle_failure() -> None:
    handler = DeadLetterHandler()
    from solvyn.message_bus.redis import message_bus
    message_bus.redis = AsyncMock()
    await handler.handle_failure("test-agent", {"input": "data"}, "error occurred", "s1", "t1")
    message_bus.redis.xadd.assert_awaited_once()
