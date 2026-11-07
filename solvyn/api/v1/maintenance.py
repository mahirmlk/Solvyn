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
from solvyn.db.models import (
    MaintenanceSchedule,
    Project,
    Task,
    TaskHandoff,
    TaskWorkspace,
    TeammateProfile,
    TeammateProjectAssignment,
)
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["proactive engineering"])
_MAX_ENABLED_SCHEDULES_PER_PROJECT = 10


class MaintenanceScheduleCreate(BaseModel):
    project_id: uuid.UUID
    task_id: uuid.UUID | None = None
    maintenance_type: Literal["code_quality", "dependency_review", "documentation_review"]
    title: str = Field(min_length=1, max_length=255)
    interval_days: int = Field(ge=1, le=30)


class MaintenanceScheduleUpdate(BaseModel):
    enabled: bool


def _schedule_dict(schedule: MaintenanceSchedule, *, task_title: str | None = None) -> dict[str, object]:
    return {
        "id": str(schedule.id),
        "organization_id": str(schedule.organization_id),
        "project_id": str(schedule.project_id),
        "task_id": str(schedule.task_id) if schedule.task_id else None,
        "task_title": task_title,
        "maintenance_type": schedule.maintenance_type,
        "title": schedule.title,
        "interval_days": schedule.interval_days,
        "next_run_at": schedule.next_run_at.isoformat(),
        "enabled": schedule.enabled,
        "created_at": schedule.created_at.isoformat() if schedule.created_at else None,
    }


async def _validate_check_target(db: AsyncSession, *, task_id: uuid.UUID, project: Project) -> None:
    task = (
        await db.execute(
            select(Task).where(Task.id == task_id, Task.tenant_id == project.tenant_id)
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Maintenance task not found")
    if task.execution_id is None:
        raise HTTPException(status_code=409, detail="Maintenance checks require a task linked to a workflow execution")
    workspace = (
        await db.execute(
            select(TaskWorkspace).where(
                TaskWorkspace.tenant_id == project.tenant_id,
                TaskWorkspace.task_id == task.id,
            )
        )
    ).scalar_one_or_none()
    handoff = (
        await db.execute(
            select(TaskHandoff).where(
                TaskHandoff.tenant_id == project.tenant_id,
                TaskHandoff.child_task_id == task.id,
                TaskHandoff.project_id == project.id,
                TaskHandoff.status.in_(("pending", "running", "failed", "completed")),
            )
        )
    ).scalar_one_or_none()
    if workspace is None or handoff is None:
        raise HTTPException(status_code=409, detail="Maintenance checks require an assigned task with a ready workspace")
    teammate_id = await db.scalar(
        select(TeammateProfile.id).where(
            TeammateProfile.id == handoff.teammate_id,
            TeammateProfile.tenant_id == project.tenant_id,
            TeammateProfile.organization_id == project.organization_id,
            TeammateProfile.is_enabled.is_(True),
        )
    )
    assignment_id = await db.scalar(
        select(TeammateProjectAssignment.id).where(
            TeammateProjectAssignment.teammate_id == handoff.teammate_id,
            TeammateProjectAssignment.project_id == project.id,
        )
    )
    if teammate_id is None or assignment_id is None:
        raise HTTPException(status_code=409, detail="Maintenance teammate assignment is no longer active")


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

    if payload.maintenance_type == "code_quality" and payload.task_id is None:
        raise HTTPException(status_code=422, detail="Code quality checks require a task with a ready workspace")
    if payload.task_id is not None:
        await _validate_check_target(db, task_id=payload.task_id, project=project)

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
        tenant_id=project.tenant_id,
        organization_id=organization_id,
        project_id=payload.project_id,
        task_id=payload.task_id,
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
    membership = await require_org_membership(db, organization_id, roles=ANY_ROLE)
    result = await db.execute(
        select(MaintenanceSchedule, Task.title)
        .outerjoin(
            Task,
            (Task.id == MaintenanceSchedule.task_id) & (Task.tenant_id == MaintenanceSchedule.tenant_id),
        )
        .where(MaintenanceSchedule.organization_id == organization_id)
        .order_by(MaintenanceSchedule.created_at.desc())
    )
    items = [_schedule_dict(schedule, task_title=task_title) for schedule, task_title in result.all()]
    return {"schedules": items, "count": len(items), "can_manage": membership.role in OWNER_ADMIN}


@router.get("/organizations/{organization_id}/projects/{project_id}/maintenance-task-targets")
async def list_maintenance_task_targets(
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    """List bounded project tasks that can run an approval-gated quality check."""
    await require_org_membership(db, organization_id, roles=ANY_ROLE)
    project = (
        await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.organization_id == organization_id,
                Project.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Active project not found in this organization")

    rows = (
        await db.execute(
            select(Task.id, Task.execution_id, Task.title, Task.status)
            .join(
                TaskWorkspace,
                (TaskWorkspace.task_id == Task.id) & (TaskWorkspace.tenant_id == Task.tenant_id),
            )
            .join(
                TaskHandoff,
                (TaskHandoff.child_task_id == Task.id)
                & (TaskHandoff.tenant_id == Task.tenant_id)
                & (TaskHandoff.project_id == project.id),
            )
            .join(
                TeammateProfile,
                (TeammateProfile.id == TaskHandoff.teammate_id)
                & (TeammateProfile.tenant_id == TaskHandoff.tenant_id)
                & (TeammateProfile.organization_id == organization_id)
                & TeammateProfile.is_enabled.is_(True),
            )
            .join(
                TeammateProjectAssignment,
                (TeammateProjectAssignment.teammate_id == TeammateProfile.id)
                & (TeammateProjectAssignment.project_id == project.id),
            )
            .where(
                Task.tenant_id == project.tenant_id,
                Task.execution_id.is_not(None),
                TaskHandoff.status.in_(("pending", "running", "failed", "completed")),
            )
            .order_by(Task.updated_at.desc(), Task.id)
            .limit(100)
        )
    ).all()
    return {
        "tasks": [
            {"id": str(task_id), "execution_id": str(execution_id), "title": title, "status": status}
            for task_id, execution_id, title, status in rows
        ],
        "count": len(rows),
    }


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
        if schedule.maintenance_type == "code_quality" and schedule.task_id is None:
            raise HTTPException(status_code=409, detail="Set a task with a ready workspace before re-enabling this check")
        if schedule.task_id is not None:
            await _validate_check_target(db, task_id=schedule.task_id, project=project)
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
