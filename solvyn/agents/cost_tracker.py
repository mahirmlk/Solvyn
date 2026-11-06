import uuid
from typing import Any

from sqlalchemy import func as sqlfunc
from sqlalchemy import select

from solvyn.db.models import TokenUsage
from solvyn.db.session import async_session_factory

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


def _to_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    if value == "default":
        return _DEFAULT_UUID
    return uuid.UUID(value)


class CostTracker:
    async def record(
        self,
        tenant_id: str,
        session_id: str | None,
        agent_type: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        cost: float,
    ) -> None:
        async with async_session_factory() as db:
            record = TokenUsage(
                id=uuid.uuid4(),
                tenant_id=_to_uuid(tenant_id),
                session_id=_to_uuid(session_id),
                agent_type=agent_type,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost=cost,
            )
            db.add(record)
            await db.commit()

    async def get_session_totals(self, session_id: str, tenant_id: str) -> dict[str, Any]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(
                    sqlfunc.sum(TokenUsage.total_tokens).label("total_tokens"),
                    sqlfunc.sum(TokenUsage.cost).label("total_cost"),
                    sqlfunc.sum(TokenUsage.prompt_tokens).label("prompt_tokens"),
                    sqlfunc.sum(TokenUsage.completion_tokens).label("completion_tokens"),
                ).where(
                    TokenUsage.session_id == _to_uuid(session_id),
                    TokenUsage.tenant_id == _to_uuid(tenant_id),
                ),
            )
            row = result.one_or_none()
            if row is None:
                return {"total_tokens": 0, "total_cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0}
            return {
                "total_tokens": row.total_tokens or 0,
                "total_cost": float(row.total_cost or 0.0),
                "prompt_tokens": row.prompt_tokens or 0,
                "completion_tokens": row.completion_tokens or 0,
            }

    async def get_tenant_totals(self, tenant_id: str) -> dict[str, Any]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(
                    sqlfunc.sum(TokenUsage.total_tokens).label("total_tokens"),
                    sqlfunc.sum(TokenUsage.cost).label("total_cost"),
                ).where(TokenUsage.tenant_id == _to_uuid(tenant_id)),
            )
            row = result.one_or_none()
            if row is None:
                return {"total_tokens": 0, "total_cost": 0.0}
            return {
                "total_tokens": row.total_tokens or 0,
                "total_cost": float(row.total_cost or 0.0),
            }

    async def get_agent_breakdown(self, session_id: str, tenant_id: str) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            result = await db.execute(
                select(
                    TokenUsage.agent_type,
                    sqlfunc.sum(TokenUsage.total_tokens).label("tokens"),
                    sqlfunc.sum(TokenUsage.cost).label("cost"),
                    sqlfunc.count(TokenUsage.id).label("calls"),
                )
                .where(
                    TokenUsage.session_id == _to_uuid(session_id),
                    TokenUsage.tenant_id == _to_uuid(tenant_id),
                )
                .group_by(TokenUsage.agent_type),
            )
            return [{"agent_type": r.agent_type, "tokens": r.tokens or 0, "cost": float(r.cost or 0.0), "calls": r.calls or 0} for r in result.all()]


cost_tracker = CostTracker()
