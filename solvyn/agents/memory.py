from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import select, text

from solvyn.db.models import Memory as MemoryModel
from solvyn.db.session import async_session_factory, bind_tenant_scope

logger = logging.getLogger(__name__)

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _to_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    if value == "default":
        return _DEFAULT_UUID
    return uuid.UUID(value)


class AgentMemory:
    """Long-term memory with pgvector semantic search support."""

    MEMORY_PREFIX = "memory:"

    def __init__(self, tenant_id: str, session_id: str | None = None) -> None:
        self.tenant_id = tenant_id
        self.session_id = session_id

    def _tenant_uuid(self) -> uuid.UUID:
        tenant_uuid = _to_uuid(self.tenant_id)
        if tenant_uuid is None:
            raise ValueError("Agent memory requires a tenant ID")
        return tenant_uuid

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
        tenant_uuid = self._tenant_uuid()
        async with async_session_factory() as session:
            await bind_tenant_scope(session, tenant_uuid)
            # Store embedding directly as Vector data (not JSON string)
            record = MemoryModel(
                id=uuid.uuid4(),
                tenant_id=tenant_uuid,
                session_id=_to_uuid(self.session_id),
                agent_type=agent_type,
                key=prefixed,
                value=json.dumps(value) if not isinstance(value, str) else value,
                embedding=embedding,  # Direct assignment to Vector column
            )
            session.add(record)
            await session.commit()
        logger.debug(f"Stored memory: {agent_type}/{prefixed}")

    async def retrieve(self, agent_type: str, key: str) -> Any | None:
        prefixed = self._prefixed_key(key)
        tenant_uuid = self._tenant_uuid()
        async with async_session_factory() as session:
            await bind_tenant_scope(session, tenant_uuid)
            result = await session.execute(
                select(MemoryModel)
                .where(MemoryModel.tenant_id == tenant_uuid)
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
        """Search memory using vector similarity or keyword fallback.

        Args:
            agent_type: Filter by agent type.
            query: Natural language query.
            limit: Maximum results to return.
            query_embedding: Pre-computed embedding vector. If *None*, the
                embedding service generates one from *query*.
        """
        # Vector search is the primary method
        if query_embedding is not None:
            return await self._vector_search(agent_type, query_embedding, limit)

        # Generate embedding via the embedding service (API or hash fallback)
        try:
            from solvyn.llm.embeddings import embedding_service

            response = await embedding_service.embed(query)
            return await self._vector_search(agent_type, response.embedding, limit)
        except Exception as e:
            logger.warning("Embedding generation failed, falling back to keyword search: %s", e)
            return await self._keyword_search(agent_type, query, limit)

    async def _vector_search(
        self,
        agent_type: str,
        query_embedding: list[float],
        limit: int,
    ) -> list[dict[str, Any]]:
        try:
            tenant_uuid = self._tenant_uuid()
            async with async_session_factory() as session:
                await bind_tenant_scope(session, tenant_uuid)
                sql = text("""
                    SELECT id, agent_type, key, value, embedding, created_at,
                           1 - (embedding <=> :query_embedding) AS similarity
                    FROM memory
                    WHERE tenant_id = :tenant_id
                      AND agent_type = :agent_type
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> :query_embedding
                    LIMIT :limit
                """)
                result = await session.execute(
                    sql,
                    {
                        "agent_type": agent_type,
                        "tenant_id": tenant_uuid,
                        "query_embedding": query_embedding,  # Direct Vector data
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
            logger.error(f"Vector search failed: {e}")
            # No fallback - raise the exception so the caller knows
            raise

    async def _keyword_search(self, agent_type: str, query: str, limit: int) -> list[dict[str, Any]]:
        tenant_uuid = self._tenant_uuid()
        async with async_session_factory() as session:
            await bind_tenant_scope(session, tenant_uuid)
            stmt = (
                select(MemoryModel)
                .where(MemoryModel.tenant_id == tenant_uuid)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                .order_by(MemoryModel.created_at.desc())
                .limit(limit)
            )
            if query:
                escaped = re.sub(r"([%_])", r"\\\1", query)
                stmt = stmt.where(MemoryModel.value.ilike(f"%{escaped}%"))
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
        tenant_uuid = self._tenant_uuid()
        async with async_session_factory() as session:
            await bind_tenant_scope(session, tenant_uuid)
            result = await session.execute(
                select(MemoryModel.key)
                .where(MemoryModel.tenant_id == tenant_uuid)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                .distinct()
                .limit(100),
            )
            return [row[0] for row in result.all()]

    async def delete(self, agent_type: str, key: str) -> bool:
        prefixed = self._prefixed_key(key)
        tenant_uuid = self._tenant_uuid()
        async with async_session_factory() as session:
            await bind_tenant_scope(session, tenant_uuid)
            result = await session.execute(
                select(MemoryModel)
                .where(MemoryModel.tenant_id == tenant_uuid)
                .where(MemoryModel.agent_type == agent_type)
                .where(MemoryModel.key == prefixed),
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
        tenant_uuid = self._tenant_uuid()
        total_deleted = 0
        while True:
            async with async_session_factory() as session:
                await bind_tenant_scope(session, tenant_uuid)
                result = await session.execute(
                    select(MemoryModel)
                    .where(MemoryModel.tenant_id == tenant_uuid)
                    .where(MemoryModel.agent_type == agent_type)
                    .where(MemoryModel.key.like(f"{self.MEMORY_PREFIX}%"))
                    .where(MemoryModel.created_at < cutoff)
                    .limit(500),
                )
                records = result.scalars().all()
                if not records:
                    break
                deleted = 0
                for record in records:
                    await session.delete(record)
                    deleted += 1
                if deleted > 0:
                    await session.commit()
                    total_deleted += deleted
        if total_deleted > 0:
            logger.info("Compacted %d expired memories for agent '%s'", total_deleted, agent_type)
        return total_deleted
