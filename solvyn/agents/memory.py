from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import select, text

from solvyn.db.models import Memory as MemoryModel
from solvyn.db.session import async_session_factory

logger = logging.getLogger(__name__)

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


class AgentMemory:
    """Long-term memory with pgvector semantic search support."""

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
        embedding: list[float] | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        prefixed = self._prefixed_key(key)
        embedding_str: str | None = None
        if embedding is not None:
            embedding_str = json.dumps(embedding)
        async with async_session_factory() as session:
            record = MemoryModel(
                id=uuid.uuid4(),
                tenant_id=_DEFAULT_UUID if self.tenant_id == "default" else uuid.UUID(self.tenant_id),
                session_id=uuid.UUID(self.session_id) if self.session_id else None,
                agent_type=agent_type,
                key=prefixed,
                value=json.dumps(value) if not isinstance(value, str) else value,
                embedding=embedding_str,
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

    async def search(
        self,
        agent_type: str,
        query: str,
        limit: int = 5,
        query_embedding: list[float] | None = None,
    ) -> list[dict[str, Any]]:
        if query_embedding is not None:
            return await self._vector_search(agent_type, query_embedding, limit)
        return await self._keyword_search(agent_type, query, limit)

    async def _vector_search(
        self,
        agent_type: str,
        query_embedding: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        embedding_str = json.dumps(query_embedding)
        try:
            async with async_session_factory() as session:
                sql = text("""
                    SELECT id, agent_type, key, value, embedding, created_at,
                           1 - (embedding::vector <=> :query_embedding::vector) AS similarity
                    FROM memory
                    WHERE agent_type = :agent_type
                      AND embedding IS NOT NULL
                    ORDER BY embedding::vector <=> :query_embedding::vector
                    LIMIT :limit
                """)
                result = await session.execute(
                    sql,
                    {
                        "agent_type": agent_type,
                        "query_embedding": embedding_str,
                        "limit": limit,
                    },
                )
                rows = result.fetchall()
                return [
                    {
                        "id": str(row[0]),
                        "agent_type": row[1],
                        "key": row[2],
                        "value": json.loads(row[3]) if row[3] else None,
                        "similarity": float(row[6]) if row[6] is not None else 0.0,
                        "created_at": row[5].isoformat() if row[5] else None,
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.warning(f"Vector search failed, falling back to keyword search: {e}")
            return await self._keyword_search(agent_type, "", limit)

    async def _keyword_search(self, agent_type: str, query: str, limit: int) -> list[dict[str, Any]]:
        async with async_session_factory() as session:
            stmt = (
                select(MemoryModel)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            if query:
                stmt = stmt.where(MemoryModel.value.ilike(f"%{query}%"))
            result = await session.execute(stmt)
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
                select(MemoryModel.key).where(MemoryModel.agent_type == agent_type).where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%")).distinct().limit(100),
            )
            return [row[0] for row in result.all()]

    async def delete(self, agent_type: str, key: str) -> bool:
        prefixed = self._prefixed_key(key)
        async with async_session_factory() as session:
            result = await session.execute(
                select(MemoryModel).where(MemoryModel.agent_type == agent_type).where(MemoryModel.key == prefixed),
            )
            record = result.scalar_one_or_none()
            if record:
                await session.delete(record)
                await session.commit()
                return True
            return False

    async def compact(self, agent_type: str, max_age_days: int = 90) -> int:
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        async with async_session_factory() as session:
            result = await session.execute(
                select(MemoryModel)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                .where(MemoryModel.created_at < cutoff)
                .limit(500),
            )
            records = result.scalars().all()
            deleted = 0
            for record in records:
                await session.delete(record)
                deleted += 1
            if deleted > 0:
                await session.commit()
                logger.info(f"Compacted {deleted} expired memories for agent '{agent_type}'")
            return deleted
