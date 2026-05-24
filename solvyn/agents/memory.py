import json
import logging
import uuid
from typing import Any

from sqlalchemy import select

from solvyn.db.models import Memory as MemoryModel
from solvyn.db.session import async_session_factory

logger = logging.getLogger(__name__)

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


class AgentMemory:
    MEMORY_PREFIX = "memory:"

    def __init__(self, tenant_id: str, session_id: str | None = None) -> None:
        self.tenant_id = tenant_id
        self.session_id = session_id

    def _prefixed_key(self, key: str) -> str:
        if key.startswith(self.MEMORY_PREFIX):
            return key
        return f"{self.MEMORY_PREFIX}{key}"

    async def store(
        self,
        agent_type: str,
        key: str,
        value: Any,
        embedding: str | None = None,
    ) -> None:
        prefixed = self._prefixed_key(key)
        async with async_session_factory() as session:
            record = MemoryModel(
                id=uuid.uuid4(),
                tenant_id=_DEFAULT_UUID if self.tenant_id == "default" else uuid.UUID(self.tenant_id),
                session_id=uuid.UUID(self.session_id) if self.session_id else None,
                agent_type=agent_type,
                key=prefixed,
                value=json.dumps(value) if not isinstance(value, str) else value,
                embedding=embedding,
            )
            session.add(record)
            await session.commit()
        logger.debug(f"Stored memory: {agent_type}/{prefixed}")

    async def retrieve(self, agent_type: str, key: str) -> Any | None:
        prefixed = self._prefixed_key(key)
        async with async_session_factory() as session:
            result = await session.execute(
                select(MemoryModel)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key == prefixed)
                .order_by(MemoryModel.created_at.desc())
                .limit(1),
            )
            record = result.scalar_one_or_none()
            if record is None:
                return None
            try:
                return json.loads(record.value)
            except (json.JSONDecodeError, TypeError):
                return record.value

    async def search(self, agent_type: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
        async with async_session_factory() as session:
            result = await session.execute(
                select(MemoryModel)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                .order_by(MemoryModel.created_at.desc())
                .limit(limit),
            )
            records = result.scalars().all()
            return [
                {
                    "key": r.key,
                    "value": json.loads(r.value) if r.value else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ]

    async def list_keys(self, agent_type: str) -> list[str]:
        async with async_session_factory() as session:
            result = await session.execute(
                select(MemoryModel.key)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                .distinct()
                .limit(100),
            )
            return [row[0] for row in result.all()]

    async def delete(self, agent_type: str, key: str) -> bool:
        prefixed = self._prefixed_key(key)
        async with async_session_factory() as session:
            result = await session.execute(
                select(MemoryModel)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key == prefixed),
            )
            record = result.scalar_one_or_none()
            if record:
                await session.delete(record)
                await session.commit()
                return True
            return False
