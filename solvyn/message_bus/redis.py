import json
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from solvyn.config import settings

logger = logging.getLogger(__name__)


class RedisMessageBus:
    def __init__(self) -> None:
        self.redis: aioredis.Redis | None = None
        self.pubsub: aioredis.client.PubSub | None = None

    async def connect(self) -> None:
        self.redis = await aioredis.from_url(settings.redis_url, decode_responses=True)
        self.pubsub = self.redis.pubsub()
        logger.info("Connected to Redis")

    async def disconnect(self) -> None:
        if self.redis:
            await self.redis.aclose()
        if self.pubsub:
            await self.pubsub.aclose()  # type: ignore[no-untyped-call]
        logger.info("Disconnected from Redis")

    async def _ensure_connected(self) -> None:
        if self.redis is None:
            await self.connect()

    async def publish(self, channel: str, message: dict[str, Any], tenant_id: str | None = None) -> str:
        await self._ensure_connected()
        msg_id = str(uuid.uuid4())
        payload = {
            "id": msg_id,
            "tenant_id": tenant_id or "default",
            "timestamp": datetime.now(UTC).isoformat(),
            **message,
        }
        await self.redis.publish(channel, json.dumps(payload))  # type: ignore[union-attr]
        # Redis XADD requires all values to be strings; serialize nested dicts/lists to JSON
        flat_payload = {k: v if isinstance(v, str) else json.dumps(v) for k, v in payload.items()}
        await self.redis.xadd(f"stream:{channel}", flat_payload)  # type: ignore[union-attr,arg-type]
        logger.debug(f"Published to {channel}: {msg_id}")
        return msg_id

    async def subscribe(self, channel: str) -> AsyncGenerator[dict[str, Any]]:
        await self._ensure_connected()
        await self.pubsub.subscribe(channel)  # type: ignore[union-attr]
        async for message in self.pubsub.listen():  # type: ignore[union-attr]
            if message["type"] == "message":
                yield json.loads(message["data"])

    async def send_to_agent(self, agent_id: str, message: dict[str, Any], tenant_id: str | None = None) -> str:
        return await self.publish(f"agent:{agent_id}", message, tenant_id)

    async def send_to_session(self, session_id: str, message: dict[str, Any], tenant_id: str | None = None) -> str:
        return await self.publish(f"session:{session_id}", message, tenant_id)

    async def get_stream(self, channel: str, count: int = 10) -> list[dict[str, Any]]:
        await self._ensure_connected()
        entries = await self.redis.xrevrange(f"stream:{channel}", count=count)  # type: ignore[union-attr]
        result = []
        for entry_id, data in entries:
            data["_id"] = entry_id
            result.append(data)
        return result

    async def add_to_dead_letter(self, channel: str, message: dict[str, Any], error: str) -> None:
        await self._ensure_connected()
        payload = {"original_channel": channel, "error": error, **message}
        await self.redis.xadd("dead_letter", payload)  # type: ignore[union-attr,arg-type]
        logger.warning(f"Message sent to dead letter: {message.get('id', 'unknown')}")


message_bus = RedisMessageBus()
