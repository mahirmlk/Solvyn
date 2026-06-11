import json
import logging
import uuid
from typing import Any, cast

from sqlalchemy import select

from solvyn.db.models import Memory as MemoryModel
from solvyn.db.session import async_session_factory

logger = logging.getLogger(__name__)

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _to_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    if value == "default":
        return _DEFAULT_UUID
    return uuid.UUID(value)


class CheckpointManager:
    CHECKPOINT_PREFIX = "checkpoint:"

    async def save(self, session_id: str, agent_type: str, state: dict[str, Any], tenant_id: str) -> str:
        checkpoint_id = self.CHECKPOINT_PREFIX + str(uuid.uuid4())
        value = json.dumps(state)

        async with async_session_factory() as db:
            record = MemoryModel(
                id=uuid.uuid4(),
                tenant_id=_to_uuid(tenant_id),
                session_id=_to_uuid(session_id),
                agent_type=agent_type,
                key=checkpoint_id,
                value=value,
            )
            db.add(record)
            await db.commit()

        logger.info(f"Checkpoint saved: {checkpoint_id}")
        return checkpoint_id

    async def load(self, checkpoint_id: str) -> dict[str, Any] | None:
        async with async_session_factory() as db:
            result = await db.execute(select(MemoryModel).where(MemoryModel.key == checkpoint_id))
            record = result.scalar_one_or_none()
            if record is None:
                return None
            return cast("dict[str, Any]", json.loads(record.value))

    async def list_for_session(self, session_id: str) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            query = select(MemoryModel).where(MemoryModel.key.like(f"{self.CHECKPOINT_PREFIX}%"))
            if session_id:
                try:
                    query = query.where(MemoryModel.session_id == uuid.UUID(session_id))
                except ValueError:
                    return []
            result = await db.execute(query.order_by(MemoryModel.created_at.desc()))
            records = result.scalars().all()
            return [
                {
                    "id": r.key,
                    "agent_type": r.agent_type,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "state_summary": json.loads(r.value) if r.value else {},
                }
                for r in records
            ]

    async def delete(self, checkpoint_id: str) -> bool:
        async with async_session_factory() as db:
            result = await db.execute(select(MemoryModel).where(MemoryModel.key == checkpoint_id))
            record = result.scalar_one_or_none()
            if record:
                await db.delete(record)
                await db.commit()
                return True
            return False


checkpoint_manager = CheckpointManager()
