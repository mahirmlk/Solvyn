import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class RetryHandler:
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor

    async def execute_with_retry(
        self,
        coro_factory: Any,
        retryable_exceptions: tuple[type[Exception], ...] = (ConnectionError, TimeoutError),
        context: dict[str, Any] | None = None,
    ) -> Any:
        if self.max_retries <= 0:
            raise RuntimeError("max_retries must be > 0")
        last_exception: Exception | None = None
        ctx = context or {}

        for attempt in range(1, self.max_retries + 1):
            try:
                return await coro_factory()
            except retryable_exceptions as e:
                last_exception = e
                if attempt < self.max_retries:
                    delay = min(self.base_delay * (self.backoff_factor ** (attempt - 1)), self.max_delay)
                    logger.warning(f"Attempt {attempt}/{self.max_retries} failed: {e}. Retrying in {delay:.1f}s... | {ctx}")
                    await asyncio.sleep(delay)

        logger.error(f"All {self.max_retries} attempts failed: {last_exception} | {ctx}")
        raise last_exception  # type: ignore[misc]


class DeadLetterHandler:
    async def handle_failure(
        self,
        agent_type: str,
        input_data: dict[str, Any],
        error: str,
        session_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        from solvyn.message_bus.redis import message_bus

        await message_bus.add_to_dead_letter(
            channel=f"agent:{agent_type}",
            message={
                "agent_type": agent_type,
                "session_id": session_id,
                "input": input_data,
                "tenant_id": tenant_id,
            },
            error=error,
        )


retry_handler = RetryHandler()
dead_letter_handler = DeadLetterHandler()
