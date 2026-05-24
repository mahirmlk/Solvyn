
import uuid
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import Session
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(prefix="/sessions", tags=["sessions"])

_DEFAULT_UUID = uuid.UUID("00000000-0000-0000-0000-000000000000")


@router.get("/")
async def list_sessions(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    result = await db.execute(select(Session).limit(50))
    sessions = result.scalars().all()
    return {
        "sessions": [
            {"id": str(s.id), "title": s.title, "status": s.status, "created_at": s.created_at.isoformat()}
            for s in sessions
        ]
    }


@router.post("/")
async def create_session(db: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    session = Session(
        title="New Session",
        tenant_id=tenant_context.tenant_id or _DEFAULT_UUID,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return {"id": str(session.id), "title": session.title, "status": session.status}
