"""Persistent AI teammate profiles and assigned-project runtime endpoint.

Direct teammate runs remain tool-less. Delegated task handoffs may receive
permission-checked workspace tools when the task has a managed workspace.
"""

from __future__ import annotations

import re
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.agents.conversation_context import build_conversation_prompt, serialize_assistant_message
from solvyn.agents.teammate_runtime import (
    ROLE_AGENT_TYPES,
    RepositoryContextUnavailableError,
    TeammateRunConflictError,
    TeammateRunDeniedError,
    run_teammate,
    validate_role_agent_type,
)
from solvyn.api.v1._rbac import ANY_ROLE, MEMBER_OR_ABOVE, OWNER_ADMIN, require_org_membership, require_tenant
from solvyn.db.models import (
    Message,
    PermissionLevel,
    Project,
    ProjectKnowledge,
    Session,
    TeammateConversation,
    TeammateMemory,
    TeammatePermission,
    TeammateProfile,
    TeammateProjectAssignment,
    TeammateStatus,
)
from solvyn.db.session import get_session
from solvyn.multi_tenant.middleware import tenant_context

router = APIRouter(tags=["teammates"])

# Least-privilege defaults per role. Writers grant the minimum needed to do
# the job inside an isolated workspace; merge/deploy always require approval.
_DEFAULT_PERMISSIONS: dict[str, list[str]] = {
    "Tech Lead": [PermissionLevel.READ_ONLY.value, PermissionLevel.PROPOSE_CHANGES.value],
    "Software Engineer": [
        PermissionLevel.READ_ONLY.value,
        PermissionLevel.PROPOSE_CHANGES.value,
        PermissionLevel.MODIFY_FILES.value,
        PermissionLevel.EXECUTE_COMMANDS.value,
        PermissionLevel.CREATE_BRANCH.value,
        PermissionLevel.CREATE_PULL_REQUEST.value,
    ],
    "QA Engineer": [
        PermissionLevel.READ_ONLY.value,
        PermissionLevel.EXECUTE_COMMANDS.value,
    ],
    "Security Engineer": [PermissionLevel.READ_ONLY.value],
}

_VALID_PERMISSIONS = {p.value for p in PermissionLevel}


async def _load_teammate(db: AsyncSession, teammate_id: uuid.UUID, tenant_id: uuid.UUID) -> TeammateProfile:
    teammate = await db.get(TeammateProfile, teammate_id)
    if teammate is None or teammate.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Teammate not found")
    return teammate


def _teammate_dict(t: TeammateProfile, project_ids: list[uuid.UUID] | None = None) -> dict[str, object]:
    return {
        "id": str(t.id),
        "organization_id": str(t.organization_id),
        "project_ids": [str(project_id) for project_id in project_ids or []],
        "name": t.name,
        "role": t.role,
        "description": t.description or "",
        "responsibilities": t.responsibilities or "",
        "agent_type": t.agent_type,
        "is_enabled": bool(t.is_enabled),
        "status": t.status,
        "model_provider": t.model_provider,
        "model_name": t.model_name,
        "max_tokens_per_run": t.max_tokens_per_run,
        "max_run_seconds": t.max_run_seconds,
        "created_at": t.created_at.isoformat() if t.created_at else "",
        "updated_at": t.updated_at.isoformat() if t.updated_at else "",
    }


class TeammateCreate(BaseModel):
    organization_id: uuid.UUID
    name: str = Field(..., min_length=1, max_length=255)
    role: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    responsibilities: str = ""
    system_instructions: str = ""
    agent_type: str | None = Field(default=None, max_length=100)
    model_provider: str | None = None
    model_name: str | None = None
    max_tokens_per_run: int | None = Field(default=None, ge=1, le=16_000)
    max_run_seconds: int | None = Field(default=None, ge=1, le=900)
    permissions: list[str] | None = None
    project_ids: list[uuid.UUID] | None = None


class TeammateUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    responsibilities: str | None = None
    system_instructions: str | None = None
    is_enabled: bool | None = None
    model_provider: str | None = None
    model_name: str | None = None
    max_tokens_per_run: int | None = Field(default=None, ge=1, le=16_000)
    max_run_seconds: int | None = Field(default=None, ge=1, le=900)
    permissions: list[str] | None = None
    project_ids: list[uuid.UUID] | None = None


class TeammateRun(BaseModel):
    project_id: uuid.UUID
    repository_id: uuid.UUID | None = None
    repository_paths: list[str] = Field(default_factory=list, max_length=4)
    input_data: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_repository_paths(self) -> TeammateRun:
        if self.repository_paths and self.repository_id is None:
            raise ValueError("repository_id is required when repository_paths are provided")
        for path in self.repository_paths:
            if (
                not path
                or len(path) > 512
                or "\x00" in path
                or path.startswith(("/", "\\"))
                or "\\" in path
                or ".." in path.split("/")
                or any(segment in {"", "."} for segment in path.split("/"))
            ):
                raise ValueError("repository_paths must contain safe, relative repository file paths")
        return self


class TeammateConversationCreate(BaseModel):
    project_id: uuid.UUID


class TeammateConversationMessageIn(BaseModel):
    content: str = Field(..., min_length=1, max_length=10_000)


class TeammateMemoryIn(BaseModel):
    project_id: uuid.UUID
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=10_000)


async def _assigned_project(
    db: AsyncSession,
    teammate: TeammateProfile,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> Project:
    result = await db.execute(
        select(Project)
        .join(TeammateProjectAssignment, TeammateProjectAssignment.project_id == Project.id)
        .where(
            TeammateProjectAssignment.teammate_id == teammate.id,
            Project.id == project_id,
            Project.tenant_id == tenant_id,
            Project.organization_id == teammate.organization_id,
        )
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Assigned project not found")
    return project


async def _teammate_project_ids(db: AsyncSession, teammate_id: uuid.UUID) -> list[uuid.UUID]:
    result = await db.execute(
        select(TeammateProjectAssignment.project_id)
        .where(TeammateProjectAssignment.teammate_id == teammate_id)
        .order_by(TeammateProjectAssignment.project_id)
    )
    return list(result.scalars().all())


@router.get("/organizations/{org_id}/teammates", summary="List teammates in an organization")
async def list_teammates(org_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    await require_org_membership(db, org_id, roles=ANY_ROLE)
    result = await db.execute(select(TeammateProfile).where(TeammateProfile.organization_id == org_id).order_by(TeammateProfile.name))
    teammates = result.scalars().all()
    assignment_result = await db.execute(
        select(TeammateProjectAssignment.teammate_id, TeammateProjectAssignment.project_id)
        .join(TeammateProfile, TeammateProfile.id == TeammateProjectAssignment.teammate_id)
        .where(TeammateProfile.organization_id == org_id)
    )
    projects_by_teammate: dict[uuid.UUID, list[uuid.UUID]] = {}
    for teammate_id, project_id in assignment_result.all():
        projects_by_teammate.setdefault(teammate_id, []).append(project_id)
    items = [_teammate_dict(t, projects_by_teammate.get(t.id, [])) for t in teammates]
    return {"teammates": items, "count": len(items)}


@router.post("/teammates", status_code=201, summary="Create a persistent teammate")
async def create_teammate(payload: TeammateCreate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    await require_org_membership(db, payload.organization_id, roles=OWNER_ADMIN)
    org_id = payload.organization_id

    agent_type = payload.agent_type or ROLE_AGENT_TYPES.get(payload.role)
    if agent_type is None:
        raise HTTPException(status_code=400, detail=f"Unsupported teammate role: {payload.role}")
    try:
        validate_role_agent_type(payload.role, agent_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    duplicate = (
        await db.execute(
            select(TeammateProfile).where(TeammateProfile.organization_id == org_id, TeammateProfile.name == payload.name)
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise HTTPException(status_code=409, detail=f"Teammate '{payload.name}' already exists in this organization")

    granted = payload.permissions if payload.permissions is not None else _DEFAULT_PERMISSIONS.get(payload.role, [PermissionLevel.READ_ONLY.value])
    unknown = [p for p in granted if p not in _VALID_PERMISSIONS]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}")

    # Verify project visibility before persisting anything.
    project_rows = []
    for project_id in payload.project_ids or []:
        project = await db.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id or project.organization_id != org_id:
            raise HTTPException(status_code=400, detail=f"Project {project_id} is not visible to this tenant")
        project_rows.append(project)

    teammate = TeammateProfile(
        organization_id=org_id,
        tenant_id=tenant_id,
        name=payload.name,
        role=payload.role,
        description=payload.description,
        responsibilities=payload.responsibilities,
        system_instructions=payload.system_instructions,
        agent_type=agent_type,
        model_provider=payload.model_provider,
        model_name=payload.model_name,
        max_tokens_per_run=payload.max_tokens_per_run,
        max_run_seconds=payload.max_run_seconds,
    )
    db.add(teammate)
    await db.flush()

    for perm in granted:
        db.add(TeammatePermission(teammate_id=teammate.id, permission=perm, granted=True))
    for project in project_rows:
        db.add(TeammateProjectAssignment(teammate_id=teammate.id, project_id=project.id))

    await db.commit()
    await db.refresh(teammate)
    return _teammate_dict(teammate, [project.id for project in project_rows])


@router.get("/teammates/{teammate_id}", summary="Get a teammate with permissions")
async def get_teammate(teammate_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=ANY_ROLE)
    perms = (await db.execute(select(TeammatePermission).where(TeammatePermission.teammate_id == teammate_id))).scalars().all()
    project_ids = await _teammate_project_ids(db, teammate_id)
    out = _teammate_dict(teammate, project_ids)
    out["permissions"] = [p.permission for p in perms if p.granted]
    return out


@router.patch("/teammates/{teammate_id}", summary="Update a teammate")
async def update_teammate(teammate_id: uuid.UUID, payload: TeammateUpdate, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)

    if payload.permissions is not None:
        unknown = [p for p in payload.permissions if p not in _VALID_PERMISSIONS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}")
    project_rows: list[Project] = []
    for project_id in payload.project_ids or []:
        project = await db.get(Project, project_id)
        if project is None or project.tenant_id != tenant_id or project.organization_id != teammate.organization_id:
            raise HTTPException(status_code=400, detail=f"Project {project_id} is not visible to this tenant")
        project_rows.append(project)

    data = payload.model_dump(exclude_unset=True, exclude={"permissions", "project_ids"})
    for field_name, value in data.items():
        if value is not None:
            setattr(teammate, field_name, value)
    if payload.is_enabled is True and teammate.status == TeammateStatus.DISABLED:
        teammate.status = TeammateStatus.IDLE
    elif payload.is_enabled is False and teammate.status != TeammateStatus.BUSY:
        teammate.status = TeammateStatus.DISABLED
    if payload.permissions is not None:
        existing_permissions = (await db.execute(select(TeammatePermission).where(TeammatePermission.teammate_id == teammate_id))).scalars().all()
        for permission_row in existing_permissions:
            await db.delete(permission_row)
        for perm in payload.permissions:
            db.add(TeammatePermission(teammate_id=teammate.id, permission=perm, granted=True))
    if payload.project_ids is not None:
        existing_assignments = (await db.execute(select(TeammateProjectAssignment).where(TeammateProjectAssignment.teammate_id == teammate_id))).scalars().all()
        for assignment_row in existing_assignments:
            await db.delete(assignment_row)
        for project in project_rows:
            db.add(TeammateProjectAssignment(teammate_id=teammate.id, project_id=project.id))
    db.add(teammate)
    await db.commit()
    await db.refresh(teammate)
    return _teammate_dict(teammate, await _teammate_project_ids(db, teammate_id))


@router.delete("/teammates/{teammate_id}", summary="Disable a teammate (soft delete)")
async def disable_teammate(teammate_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)
    teammate.is_enabled = False
    if teammate.status != TeammateStatus.BUSY:
        teammate.status = TeammateStatus.DISABLED
    db.add(teammate)
    await db.commit()
    return {"ok": True, "teammate_id": str(teammate_id), "is_enabled": False}


@router.post("/teammates/{teammate_id}/run", summary="Run a teammate on an assigned project")
async def execute_teammate(
    teammate_id: uuid.UUID,
    payload: TeammateRun,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    try:
        output = await run_teammate(
            db,
            teammate,
            tenant_id=tenant_id,
            project_id=payload.project_id,
            input_data=payload.input_data,
            repository_id=payload.repository_id,
            repository_paths=payload.repository_paths,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TeammateRunDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TeammateRunConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RepositoryContextUnavailableError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Teammate run exceeded its time limit") from exc
    return {"teammate_id": str(teammate_id), "project_id": str(payload.project_id), "output": output}


def _conversation_dict(conversation: TeammateConversation) -> dict[str, object]:
    return {
        "id": str(conversation.id),
        "teammate_id": str(conversation.teammate_id),
        "project_id": str(conversation.project_id),
        "title": conversation.title,
        "created_at": conversation.created_at.isoformat() if conversation.created_at else "",
        "updated_at": conversation.updated_at.isoformat() if conversation.updated_at else "",
    }


def _message_dict(message: Message) -> dict[str, object]:
    return {
        "id": str(message.id),
        "role": message.role,
        "content": message.content,
        "created_at": message.created_at.isoformat() if message.created_at else "",
    }


async def _load_conversation(
    db: AsyncSession,
    teammate: TeammateProfile,
    conversation_id: uuid.UUID,
    tenant_id: uuid.UUID,
) -> TeammateConversation:
    result = await db.execute(
        select(TeammateConversation).where(
            TeammateConversation.id == conversation_id,
            TeammateConversation.tenant_id == tenant_id,
            TeammateConversation.teammate_id == teammate.id,
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    await _assigned_project(db, teammate, conversation.project_id, tenant_id)
    return conversation


@router.post("/teammates/{teammate_id}/conversations", status_code=201, summary="Start a persistent teammate conversation")
async def create_teammate_conversation(
    teammate_id: uuid.UUID,
    payload: TeammateConversationCreate,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    project = await _assigned_project(db, teammate, payload.project_id, tenant_id)
    session = Session(tenant_id=tenant_id, title=f"{teammate.name}: {project.name}"[:255], status="active")
    db.add(session)
    await db.flush()
    conversation = TeammateConversation(
        tenant_id=tenant_id,
        organization_id=teammate.organization_id,
        teammate_id=teammate.id,
        project_id=project.id,
        session_id=session.id,
        title="New conversation",
    )
    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)
    return {**_conversation_dict(conversation), "messages": []}


@router.get("/teammates/{teammate_id}/conversations", summary="List a teammate's project conversations")
async def list_teammate_conversations(
    teammate_id: uuid.UUID,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    await _assigned_project(db, teammate, project_id, tenant_id)
    result = await db.execute(
        select(TeammateConversation)
        .where(
            TeammateConversation.tenant_id == tenant_id,
            TeammateConversation.teammate_id == teammate_id,
            TeammateConversation.project_id == project_id,
        )
        .order_by(TeammateConversation.updated_at.desc(), TeammateConversation.id)
        .limit(50)
    )
    conversations = [_conversation_dict(row) for row in result.scalars().all()]
    return {"conversations": conversations, "count": len(conversations)}


@router.get("/teammates/{teammate_id}/conversations/{conversation_id}/messages", summary="Read persistent conversation history")
async def list_teammate_conversation_messages(
    teammate_id: uuid.UUID,
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    conversation = await _load_conversation(db, teammate, conversation_id, tenant_id)
    result = await db.execute(
        select(Message)
        .where(Message.session_id == conversation.session_id, Message.role.in_(["user", "assistant"]))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(200)
    )
    messages = [_message_dict(row) for row in reversed(result.scalars().all())]
    return {"conversation": _conversation_dict(conversation), "messages": messages, "count": len(messages)}


@router.post("/teammates/{teammate_id}/conversations/{conversation_id}/messages", summary="Send a message to a teammate")
async def send_teammate_conversation_message(
    teammate_id: uuid.UUID,
    conversation_id: uuid.UUID,
    payload: TeammateConversationMessageIn,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    conversation = await _load_conversation(db, teammate, conversation_id, tenant_id)

    prior_result = await db.execute(
        select(Message)
        .where(Message.session_id == conversation.session_id, Message.role.in_(["user", "assistant"]))
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(12)
    )
    prior_messages = list(reversed(prior_result.scalars().all()))
    message_content = payload.content.strip()
    if not message_content:
        raise HTTPException(status_code=422, detail="Message cannot be blank")
    prompt = build_conversation_prompt(
        [(row.role, row.content) for row in prior_messages],
        teammate.name,
        message_content,
    )

    try:
        output = await run_teammate(
            db,
            teammate,
            tenant_id=tenant_id,
            project_id=conversation.project_id,
            input_data={"text": prompt},
            session_id=str(conversation.session_id),
        )
    except LookupError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except TeammateRunDeniedError as exc:
        await db.rollback()
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TeammateRunConflictError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RepositoryContextUnavailableError as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except TimeoutError as exc:
        await db.rollback()
        raise HTTPException(status_code=504, detail="Teammate run exceeded its time limit") from exc
    except Exception:
        await db.rollback()
        raise

    user_message = Message(session_id=conversation.session_id, role="user", content=message_content)
    assistant_message = Message(
        session_id=conversation.session_id,
        role="assistant",
        content=serialize_assistant_message(output),
    )
    if not prior_messages:
        conversation.title = message_content[:120]
    conversation.updated_at = func.now()
    db.add(user_message)
    db.add(assistant_message)
    db.add(conversation)
    await db.commit()
    await db.refresh(user_message)
    await db.refresh(assistant_message)
    await db.refresh(conversation)
    return {
        "conversation": _conversation_dict(conversation),
        "messages": [_message_dict(user_message), _message_dict(assistant_message)],
        "output": output,
    }


@router.get("/teammates/{teammate_id}/memory", summary="List persistent teammate memory")
async def list_teammate_memory(
    teammate_id: uuid.UUID,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=MEMBER_OR_ABOVE)
    await _assigned_project(db, teammate, project_id, tenant_id)
    result = await db.execute(
        select(TeammateMemory)
        .where(
            TeammateMemory.tenant_id == tenant_id,
            TeammateMemory.teammate_id == teammate_id,
            TeammateMemory.project_id == project_id,
        )
        .order_by(TeammateMemory.key)
    )
    entries = [
        {"key": row.key, "value": row.value, "updated_at": row.updated_at.isoformat() if row.updated_at else ""}
        for row in result.scalars().all()
    ]
    return {"teammate_id": str(teammate_id), "project_id": str(project_id), "memory": entries, "count": len(entries)}


@router.put("/teammates/{teammate_id}/memory", summary="Store persistent teammate memory")
async def upsert_teammate_memory(
    teammate_id: uuid.UUID,
    payload: TeammateMemoryIn,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)
    await _assigned_project(db, teammate, payload.project_id, tenant_id)
    secret_match = _looks_like_secret(f"{payload.key}\n{payload.value}")
    if secret_match is not None:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to store possible secret material ({secret_match}). Teammate memory must not contain credentials.",
        )
    existing = (
        await db.execute(
            select(TeammateMemory).where(
                TeammateMemory.tenant_id == tenant_id,
                TeammateMemory.teammate_id == teammate_id,
                TeammateMemory.project_id == payload.project_id,
                TeammateMemory.key == payload.key,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = TeammateMemory(
            tenant_id=tenant_id,
            teammate_id=teammate_id,
            project_id=payload.project_id,
            key=payload.key,
            value=payload.value,
        )
        db.add(existing)
    else:
        existing.value = payload.value
        db.add(existing)
    await db.commit()
    await db.refresh(existing)
    return {
        "teammate_id": str(teammate_id),
        "project_id": str(payload.project_id),
        "key": existing.key,
        "updated_at": existing.updated_at.isoformat() if existing.updated_at else "",
    }


@router.delete("/teammates/{teammate_id}/memory/{key}", summary="Delete persistent teammate memory")
async def delete_teammate_memory(
    teammate_id: uuid.UUID,
    key: str,
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    tenant_id = require_tenant()
    teammate = await _load_teammate(db, teammate_id, tenant_id)
    await require_org_membership(db, teammate.organization_id, roles=OWNER_ADMIN)
    await _assigned_project(db, teammate, project_id, tenant_id)
    record = (
        await db.execute(
            select(TeammateMemory).where(
                TeammateMemory.tenant_id == tenant_id,
                TeammateMemory.teammate_id == teammate_id,
                TeammateMemory.project_id == project_id,
                TeammateMemory.key == key,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Teammate memory not found")
    await db.delete(record)
    await db.commit()
    return {"ok": True, "teammate_id": str(teammate_id), "project_id": str(project_id), "key": key}


# ---------------------------------------------------------------------------
# Project knowledge (validated, scoped — never stores secrets)
# ---------------------------------------------------------------------------


class KnowledgeIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=50)
    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1)


_SECRET_PATTERNS = (
    ("AWS access key pattern", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private key pattern", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token pattern", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("Slack token pattern", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("credential assignment pattern", re.compile(r"(?:password|api_key|secret|client_secret)\s*[:=]\s*\S+", re.I)),
    ("API token pattern", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
)


def _looks_like_secret(value: str) -> str | None:
    for label, pattern in _SECRET_PATTERNS:
        if pattern.search(value) is not None:
            return label
    return None


@router.get("/projects/{project_id}/knowledge", summary="List project knowledge")
async def list_knowledge(project_id: uuid.UUID, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_org_membership(db, project.organization_id, roles=ANY_ROLE)
    result = await db.execute(
        select(ProjectKnowledge)
        .where(ProjectKnowledge.project_id == project_id)
        .order_by(ProjectKnowledge.category, ProjectKnowledge.key)
    )
    items = [
        {"id": str(k.id), "category": k.category, "key": k.key, "value": k.value, "updated_at": k.updated_at.isoformat() if k.updated_at else ""}
        for k in result.scalars().all()
    ]
    return {"knowledge": items, "count": len(items)}


@router.put("/projects/{project_id}/knowledge", summary="Upsert a project knowledge entry")
async def upsert_knowledge(project_id: uuid.UUID, payload: KnowledgeIn, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    tenant_id = require_tenant()
    project = await db.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    await require_org_membership(db, project.organization_id, roles=OWNER_ADMIN)
    hit = _looks_like_secret(f"{payload.key}\n{payload.value}")
    if hit:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to store possible secret material ({hit}). Knowledge records must never contain credentials.",
        )
    existing = (
        await db.execute(
            select(ProjectKnowledge).where(
                ProjectKnowledge.project_id == project_id,
                ProjectKnowledge.category == payload.category,
                ProjectKnowledge.key == payload.key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.value = payload.value
        db.add(existing)
        await db.commit()
        await db.refresh(existing)
        row = existing
    else:
        row = ProjectKnowledge(project_id=project_id, category=payload.category, key=payload.key, value=payload.value, created_by=tenant_context.user_id)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return {"id": str(row.id), "category": row.category, "key": row.key, "updated_at": row.updated_at.isoformat() if row.updated_at else ""}
