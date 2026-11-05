"""Organization-scoped recurring maintenance trigger configuration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership
from solvyn.db.models import MaintenanceSchedule, Project
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["proactive engineering"])
_MAX_ENABLED_SCHEDULES_PER_PROJECT = 10


class MaintenanceScheduleCreate(BaseModel):
    project_id: uuid.UUID
    maintenance_type: Literal["code_quality", "dependency_review", "documentation_review"]
    title: str = Field(min_length=1, max_length=255)
    interval_days: int = Field(ge=1, le=30)


class MaintenanceScheduleUpdate(BaseModel):
    enabled: bool


def _schedule_dict(schedule: MaintenanceSchedule) -> dict[str, object]:
    return {
        "id": str(schedule.id),
        "organization_id": str(schedule.organization_id),
        "project_id": str(schedule.project_id),
        "maintenance_type": schedule.maintenance_type,
        "title": schedule.title,
        "interval_days": schedule.interval_days,
        "next_run_at": schedule.next_run_at.isoformat(),
        "enabled": schedule.enabled,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
    }


@router.post("/organizations/{organization_id}/maintenance-schedules", status_code=201)
async def create_maintenance_schedule(
    organization_id: uuid.UUID,
    payload: MaintenanceScheduleCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    if not payload.title.strip():
        raise HTTPException(status_code=422, detail="Schedule title cannot be empty")
    project = (
        await db.execute(
            select(Project).where(
                Project.id == payload.project_id,
                Project.organization_id == organization_id,
                Project.is_active == True,  # noqa: E712
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Active project not found in this organization")

    active_count = await db.scalar(
        select(func.count(MaintenanceSchedule.id)).where(
            MaintenanceSchedule.project_id == payload.project_id,
            MaintenanceSchedule.enabled == True,  # noqa: E712
        )
    )
    if (active_count or 0) >= _MAX_ENABLED_SCHEDULES_PER_PROJECT:
        raise HTTPException(status_code=409, detail="Project has reached the enabled maintenance schedule limit")

    now = datetime.now(UTC)
    schedule = MaintenanceSchedule(
        organization_id=organization_id,
        project_id=payload.project_id,
        created_by=tenant_context.user_id,
        maintenance_type=payload.maintenance_type,
        title=payload.title.strip(),
        interval_days=payload.interval_days,
        next_run_at=now + timedelta(days=payload.interval_days),
        enabled=True,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return _schedule_dict(schedule)


@router.get("/organizations/{organization_id}/maintenance-schedules")
async def list_maintenance_schedules(
    organization_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=ANY_ROLE)
    result = await db.execute(
        select(MaintenanceSchedule)
        .where(MaintenanceSchedule.organization_id == organization_id)
        .order_by(MaintenanceSchedule.created_at.desc())
    )
    items = [_schedule_dict(schedule) for schedule in result.scalars().all()]
    return {"schedules": items, "count": len(items)}


@router.patch("/organizations/{organization_id}/maintenance-schedules/{schedule_id}")
async def update_maintenance_schedule(
    organization_id: uuid.UUID,
    schedule_id: uuid.UUID,
    payload: MaintenanceScheduleUpdate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    schedule = (
        await db.execute(
            select(MaintenanceSchedule).where(
                MaintenanceSchedule.id == schedule_id,
                MaintenanceSchedule.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if schedule is None:
        raise HTTPException(status_code=404, detail="Maintenance schedule not found")
    if payload.enabled and not schedule.enabled:
        project = (
            await db.execute(
                select(Project)
                .where(
                    Project.id == schedule.project_id,
                    Project.organization_id == organization_id,
                    Project.is_active == True,  # noqa: E712
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(status_code=404, detail="Active project not found in this organization")
        active_count = await db.scalar(
            select(func.count(MaintenanceSchedule.id)).where(
                MaintenanceSchedule.project_id == schedule.project_id,
                MaintenanceSchedule.enabled == True,  # noqa: E712
            )
        )
        if (active_count or 0) >= _MAX_ENABLED_SCHEDULES_PER_PROJECT:
            raise HTTPException(status_code=409, detail="Project has reached the enabled maintenance schedule limit")
        schedule.next_run_at = datetime.now(UTC) + timedelta(days=schedule.interval_days)
    schedule.enabled = payload.enabled
    await db.commit()
    await db.refresh(schedule)
    return _schedule_dict(schedule)
