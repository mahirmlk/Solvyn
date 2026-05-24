import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from solvyn.db.models import Memory
from solvyn.db.session import async_session_factory

logger = logging.getLogger(__name__)

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _to_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    if value == "default":
        return _DEFAULT_UUID
    return uuid.UUID(value)


class AuditLogger:
    """Persists audit records in the shared Memory table.

    Audit logs are co-mingled with agent memories and checkpoints in the
    ``Memory`` table. They are discriminated by the ``key`` column, which
    always uses the ``audit:<uuid>`` prefix. The ``search`` method filters
    on this prefix to ensure only audit records are returned.
    """

    AUDIT_PREFIX = "audit:"

    async def log(
        self,
        action: str,
        agent_type: str,
        details: dict[str, Any],
        session_id: str | None = None,
        tenant_id: str = "default",
    ) -> str:
        audit_id = f"{self.AUDIT_PREFIX}{uuid.uuid4()}"
        record = {
            "action": action,
            "agent_type": agent_type,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "details": details,
        }

        try:
            async with async_session_factory() as db:
                memory_record = Memory(
                    id=uuid.uuid4(),
                    tenant_id=_to_uuid(tenant_id),
                    session_id=_to_uuid(session_id),
                    agent_type=agent_type,
                    key=audit_id,
                    value=json.dumps(record),
                )
                db.add(memory_record)
                await db.commit()
        except Exception as e:
            logger.warning(f"Audit log persistence failed: {e}")

        logger.info(f"AUDIT [{action}] {agent_type} | {json.dumps(details)[:200]}")
        return audit_id

    async def search(
        self,
        action: str | None = None,
        agent_type: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            query = select(Memory).where(Memory.key.like(f"{self.AUDIT_PREFIX}%"))
            if agent_type:
                query = query.where(Memory.agent_type == agent_type)
            query = query.order_by(Memory.created_at.desc()).limit(limit)
            result = await db.execute(query)
            records = result.scalars().all()

            entries = []
            for r in records:
                try:
                    data = json.loads(r.value) if r.value else {}
                    if action and data.get("action") != action:
                        continue
                    if session_id and data.get("session_id") != session_id:
                        continue
                    data["id"] = r.key
                    data["created_at"] = r.created_at.isoformat() if r.created_at else None
                    entries.append(data)
                except (json.JSONDecodeError, TypeError):
                    continue

            return entries


audit_logger = AuditLogger()
