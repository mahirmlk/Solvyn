import time
from typing import Any


class TokenBucket:
    def __init__(self, rate: float, burst: int) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()

    def consume(self, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
        self.last_refill = now

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


class RateLimiter:
    """In-memory token-bucket rate limiter.

    WARNING: All state is stored in process memory. For multi-worker
    deployments (multiple uvicorn/Gunicorn workers, or multiple pods),
    rate limits are NOT shared across processes, allowing each worker
    to burst independently. Use a Redis-backed rate limiter for
    production multi-worker setups.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}

    def _key(self, scope: str, identifier: str) -> str:
        return f"{scope}:{identifier}"

    def configure_llm(self, tenant_id: str, rate: float = 10.0, burst: int = 20) -> None:
        key = self._key("llm", tenant_id)
        self._buckets[key] = TokenBucket(rate=rate, burst=burst)

    def configure_agent(self, agent_type: str, rate: float = 5.0, burst: int = 10) -> None:
        key = self._key("agent", agent_type)
        self._buckets[key] = TokenBucket(rate=rate, burst=burst)

    def check_llm(self, tenant_id: str) -> bool:
        key = self._key("llm", tenant_id)
        bucket = self._buckets.get(key)
        if bucket is None:
            return True
        return bucket.consume()

    def check_agent(self, agent_type: str) -> bool:
        key = self._key("agent", agent_type)
        bucket = self._buckets.get(key)
        if bucket is None:
            return True
        return bucket.consume()

    def check(self, tenant_id: str, agent_type: str) -> bool:
        return self.check_llm(tenant_id) and self.check_agent(agent_type)

    def get_state(self) -> dict[str, Any]:
        return {key: {"tokens": b.tokens, "rate": b.rate, "burst": b.burst} for key, b in self._buckets.items()}


rate_limiter = RateLimiter()
