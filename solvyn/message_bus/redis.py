import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as aioredis
from redis.typing import EncodableT

from solvyn.config import settings

logger = logging.getLogger(__name__)


def _flatten(payload: dict[str, Any]) -> dict[EncodableT, EncodableT]:
    """Flatten a payload for Redis Streams; non-scalar values become JSON strings."""
    flat: dict[EncodableT, EncodableT] = {}
    for key, value in payload.items():
        if isinstance(value, (bytes, bytearray, memoryview, str, int, float)):
            flat[str(key)] = value
        else:
            flat[str(key)] = json.dumps(value)
    return flat


class RedisMessageBus:
    def __init__(self) -> None:
        self.redis: aioredis.Redis | None = None
        self.pubsub: aioredis.client.PubSub | None = None

    async def connect(self) -> aioredis.Redis:
        if self.redis is not None:
            return self.redis
        self.redis = await aioredis.from_url(settings.redis_url, decode_responses=True)
        self.pubsub = self.redis.pubsub()
        logger.info("Connected to Redis")
        return self.redis

    async def disconnect(self) -> None:
        if self.pubsub:
            # redis-py ships incomplete type hints for aclose().
            aclose = getattr(self.pubsub, "aclose")  # noqa: B009 — untyped stub
            await aclose()
            self.pubsub = None
        if self.redis:
            await self.redis.aclose()
            self.redis = None
        logger.info("Disconnected from Redis")

    async def _ensure_connected(self) -> None:
        if self.redis is None:
            await self.connect()

    async def _get_redis(self) -> aioredis.Redis:
        await self._ensure_connected()
        assert self.redis is not None
        return self.redis

    async def _get_pubsub(self) -> aioredis.client.PubSub:
        await self._ensure_connected()
        assert self.pubsub is not None
        return self.pubsub

    async def publish(self, channel: str, message: dict[str, Any], tenant_id: str | None = None) -> str:
        redis = await self._get_redis()
        msg_id = str(uuid.uuid4())
        payload = {
            "id": msg_id,
            "tenant_id": tenant_id or "default",
            "timestamp": datetime.now(UTC).isoformat(),
            **message,
        }
        await redis.publish(channel, json.dumps(payload))
        await redis.xadd(f"stream:{channel}", _flatten(payload))
        logger.debug(f"Published to {channel}: {msg_id}")
        return msg_id

    async def subscribe(self, channel: str) -> AsyncGenerator[dict[str, Any]]:
        pubsub = await self._get_pubsub()
        await pubsub.subscribe(channel)
        async for message in pubsub.listen():
            if message and message.get("type") == "message":
                yield json.loads(message["data"])

    async def send_to_agent(self, agent_id: str, message: dict[str, Any], tenant_id: str | None = None) -> str:
        return await self.publish(f"agent:{agent_id}", message, tenant_id)

    async def send_to_session(self, session_id: str, message: dict[str, Any], tenant_id: str | None = None) -> str:
        return await self.publish(f"session:{session_id}", message, tenant_id)

    async def get_stream(self, channel: str, count: int = 10) -> list[dict[str, Any]]:
        redis = await self._get_redis()
        # decode_responses=True → entry ids and field payloads are strings.
        entries = cast(list[tuple[str, dict[str, str]]], await redis.xrevrange(f"stream:{channel}", count=count))
        result: list[dict[str, Any]] = []
        for entry_id, data in entries:
            item: dict[str, Any] = dict(data) if data else {}
            item["_id"] = entry_id
            result.append(item)
        return result

    async def add_to_dead_letter(self, channel: str, message: dict[str, Any], error: str) -> None:
        redis = await self._get_redis()
        payload = {"original_channel": channel, "error": error, **message}
        await redis.xadd("dead_letter", _flatten(payload))
        logger.warning(f"Message sent to dead letter: {message.get('id', 'unknown')}")


message_bus = RedisMessageBus()
