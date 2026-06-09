from __future__ import annotations

import time

import redis.asyncio as aioredis

from solvyn.config import settings


class RedisRateLimiter:
    """Redis-backed sliding window rate limiter for multi-worker deployments."""

    SLIDING_WINDOW_SCRIPT = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local now = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, now .. '-' .. math.random(100000))
    redis.call('EXPIRE', key, math.ceil(window / 1000))
    return {1, limit - count - 1}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 0
    if #oldest > 0 then
        retry_after = math.ceil((tonumber(oldest[2]) + window - now) / 1000)
    end
    return {0, retry_after}
end
"""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None
        self._ip_limits: dict[str, int] = {"default": 100}
        self._tenant_limits: dict[str, int] = {"default": 500}
        self._window_seconds: int = 60
        self._fallback_buckets: dict[str, list[float]] = {}
        self._last_redis_failure: float = 0
        self._redis_cooldown: float = 30.0

    async def _get_redis(self) -> aioredis.Redis | None:
        if self._redis is None:
            now = time.time()
            if now - self._last_redis_failure < self._redis_cooldown:
                return None
            try:
                self._redis = await aioredis.from_url(settings.redis_url, decode_responses=True)
            except Exception:
                self._last_redis_failure = now
                return None
        return self._redis

    async def check_ip(self, client_ip: str) -> tuple[bool, int]:
        redis = await self._get_redis()
        if redis is None:
            return self._fallback_check(f"ip:{client_ip}", self._ip_limits.get("default", 100))
        try:
            now_ms = int(time.time() * 1000)
            window_ms = self._window_seconds * 1000
            key = f"ratelimit:ip:{client_ip}"
            result = await redis.eval(self.SLIDING_WINDOW_SCRIPT, 1, key, window_ms, self._ip_limits.get("default", 100), now_ms)
            allowed = bool(result[0])
            retry_after = int(result[1]) if not allowed else 0
            return allowed, retry_after
        except Exception:
            return self._fallback_check(f"ip:{client_ip}", self._ip_limits.get("default", 100))

    async def check_tenant(self, tenant_id: str) -> tuple[bool, int]:
        redis = await self._get_redis()
        if redis is None:
            return self._fallback_check(f"tenant:{tenant_id}", self._tenant_limits.get("default", 500))
        try:
            now_ms = int(time.time() * 1000)
            window_ms = self._window_seconds * 1000
            key = f"ratelimit:tenant:{tenant_id}"
            limit = self._tenant_limits.get("default", 500)
            result = await redis.eval(self.SLIDING_WINDOW_SCRIPT, 1, key, window_ms, limit, now_ms)
            allowed = bool(result[0])
            retry_after = int(result[1]) if not allowed else 0
            return allowed, retry_after
        except Exception:
            return self._fallback_check(f"tenant:{tenant_id}", self._tenant_limits.get("default", 500))

    def _fallback_check(self, key: str, limit: int) -> tuple[bool, int]:
        now = time.time()
        window = self._window_seconds
        timestamps = self._fallback_buckets.get(key, [])
        timestamps = [t for t in timestamps if now - t < window]
        if len(timestamps) >= limit:
            self._fallback_buckets[key] = timestamps
            return False, window
        timestamps.append(now)
        self._fallback_buckets[key] = timestamps
        if len(self._fallback_buckets) > 10000:
            expired_keys = [k for k, v in self._fallback_buckets.items() if not v or now - v[-1] > window * 2]
            for k in expired_keys:
                del self._fallback_buckets[k]
        return True, 0

    def configure(self, ip_limit: int = 100, tenant_limit: int = 500, window_seconds: int = 60) -> None:
        self._ip_limits["default"] = ip_limit
        self._tenant_limits["default"] = tenant_limit
        self._window_seconds = window_seconds


redis_rate_limiter = RedisRateLimiter()
