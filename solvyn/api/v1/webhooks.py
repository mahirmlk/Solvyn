"""Signed provider webhooks for human-in-the-loop engineering triage."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership
from solvyn.config import settings
from solvyn.db.models import Organization, ProactiveEvent, Project, Repository
from solvyn.db.session import get_session
from solvyn.repositories.github import GitHubRepositoryError, validate_full_name

router = APIRouter(tags=["webhooks", "proactive engineering"])
_MAX_WEBHOOK_BYTES = 256_000
_MAX_PROJECT_MATCHES = 100


def _valid_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _parse_workflow_failure(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict) or payload.get("action") != "completed":
        return None
    repository_value = payload.get("repository")
    workflow_value = payload.get("workflow_run")
    repository = repository_value if isinstance(repository_value, dict) else {}
    workflow = workflow_value if isinstance(workflow_value, dict) else {}
    if workflow.get("conclusion") != "failure" or not isinstance(repository.get("full_name"), str):
        return None
    run_id = workflow.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id < 1:
        return None
    run_attempt = workflow.get("run_attempt", 1)
    if isinstance(run_attempt, bool) or not isinstance(run_attempt, int) or run_attempt < 1:
        return None
    owner, repo = validate_full_name(repository["full_name"])
    owner_repo = f"{owner}/{repo}"
    details_url = workflow.get("html_url")
    if not isinstance(details_url, str) or not details_url.startswith("https://github.com/"):
        details_url = ""
    return {
        "repository_name": owner_repo[:255],
        "source_event_id": f"{owner_repo.lower()}:{run_id}:{run_attempt}",
        "workflow_name": str(workflow.get("name") or "GitHub Actions workflow")[:255],
        "conclusion": "failure",
        "branch": str(workflow.get("head_branch") or "")[:255],
        "details_url": details_url[:512],
    }


def _event_dict(event: ProactiveEvent) -> dict[str, object]:
    return {
        "id": str(event.id),
        "project_id": str(event.project_id),
        "delivery_id": event.delivery_id,
        "source_event_id": event.source_event_id,
        "event_type": event.event_type,
        "action": event.action,
        "repository_name": event.repository_name,
        "workflow_name": event.workflow_name,
        "conclusion": event.conclusion,
        "branch": event.branch,
        "details_url": event.details_url,
        "status": event.status,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


@router.post("/webhooks/github", status_code=202, summary="Receive a signed GitHub CI failure event")
async def receive_github_webhook(request: Request, db: AsyncSession = Depends(get_session)) -> dict[str, object]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_WEBHOOK_BYTES:
                raise HTTPException(status_code=413, detail="GitHub webhook payload is too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid content length") from exc
    body_buffer = bytearray()
    async for chunk in request.stream():
        if len(body_buffer) + len(chunk) > _MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="GitHub webhook payload is too large")
        body_buffer.extend(chunk)
    body = bytes(body_buffer)
    if not _valid_signature(settings.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Invalid GitHub webhook signature")

    delivery_id = request.headers.get("x-github-delivery", "")
    try:
        normalized_delivery_id = str(uuid.UUID(delivery_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GitHub delivery identifier") from exc

    event_type = request.headers.get("x-github-event", "")
    if event_type != "workflow_run":
        return {"accepted": True, "ignored": True, "reason": "unsupported_event"}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GitHub webhook JSON") from exc
    try:
        failure = _parse_workflow_failure(payload)
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    if failure is None:
        return {"accepted": True, "ignored": True, "reason": "not_a_failed_workflow_run"}

    project_matches = (
        await db.execute(
            select(Project.id, Project.organization_id)
            .join(Repository, Repository.project_id == Project.id)
            .join(Organization, Organization.id == Project.organization_id)
            .where(
                Repository.provider == "github",
                Repository.is_active == True,  # noqa: E712
                Project.is_active == True,  # noqa: E712
                Organization.is_active == True,  # noqa: E712
                # GitHub full names are case-insensitive; equality avoids LIKE wildcards.
                func.lower(Repository.full_name) == failure["repository_name"].lower(),
            )
            .limit(_MAX_PROJECT_MATCHES + 1)
        )
    ).all()
    if len(project_matches) > _MAX_PROJECT_MATCHES:
        raise HTTPException(status_code=413, detail="Repository is connected to too many projects for webhook fan-out")
    if not project_matches:
        return {"accepted": True, "ignored": True, "reason": "repository_not_connected"}

    events: list[ProactiveEvent] = []
    duplicate_count = 0
    for project_id, organization_id in project_matches:
        existing = (
            await db.execute(
                select(ProactiveEvent).where(
                    ProactiveEvent.organization_id == organization_id,
                    ProactiveEvent.project_id == project_id,
                    (ProactiveEvent.delivery_id == normalized_delivery_id)
                    | (ProactiveEvent.source_event_id == failure["source_event_id"]),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            duplicate_count += 1
            events.append(existing)
            continue
        event = ProactiveEvent(
            organization_id=organization_id,
            project_id=project_id,
            delivery_id=normalized_delivery_id,
            source_event_id=failure["source_event_id"],
            event_type=event_type,
            action="completed",
            repository_name=failure["repository_name"],
            workflow_name=failure["workflow_name"],
            conclusion=failure["conclusion"],
            branch=failure["branch"],
            details_url=failure["details_url"],
            status="pending",
        )
        db.add(event)
        events.append(event)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Rebuild the fan-out after a unique-key race. The failed transaction
        # may have rolled back other projects' rows, so retry every missing one.
        for attempt in range(3):
            events = []
            duplicate_count = 0
            for project_id, organization_id in project_matches:
                existing = (
                    await db.execute(
                        select(ProactiveEvent).where(
                            ProactiveEvent.organization_id == organization_id,
                            ProactiveEvent.project_id == project_id,
                            (ProactiveEvent.delivery_id == normalized_delivery_id)
                            | (ProactiveEvent.source_event_id == failure["source_event_id"]),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    duplicate_count += 1
                    events.append(existing)
                    continue
                event = ProactiveEvent(
                    organization_id=organization_id,
                    project_id=project_id,
                    delivery_id=normalized_delivery_id,
                    source_event_id=failure["source_event_id"],
                    event_type=event_type,
                    action="completed",
                    repository_name=failure["repository_name"],
                    workflow_name=failure["workflow_name"],
                    conclusion=failure["conclusion"],
                    branch=failure["branch"],
                    details_url=failure["details_url"],
                    status="pending",
                )
                db.add(event)
                events.append(event)
            try:
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
        else:
            raise HTTPException(status_code=503, detail="GitHub delivery could not be recorded; retry the webhook")
    for event in events:
        await db.refresh(event)
    return {
        "accepted": True,
        "ignored": False,
        "created_count": max(0, len(events) - duplicate_count),
        "duplicate_count": duplicate_count,
        "event_ids": [str(event.id) for event in events],
    }


@router.get("/organizations/{organization_id}/proactive-events", summary="List verified proactive events")
async def list_proactive_events(
    organization_id: uuid.UUID,
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=ANY_ROLE)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 100")
    result = await db.execute(
        select(ProactiveEvent)
        .where(ProactiveEvent.organization_id == organization_id)
        .order_by(ProactiveEvent.created_at.desc())
        .limit(limit)
    )
    items = [_event_dict(event) for event in result.scalars().all()]
    return {"events": items, "count": len(items), "content_trust": "untrusted_github_event"}


@router.post("/organizations/{organization_id}/proactive-events/{event_id}/acknowledge")
async def acknowledge_proactive_event(
    organization_id: uuid.UUID,
    event_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    await require_org_membership(db, organization_id, roles=OWNER_ADMIN)
    event = (
        await db.execute(
            select(ProactiveEvent).where(
                ProactiveEvent.id == event_id,
                ProactiveEvent.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Proactive event not found")
    event.status = "acknowledged"
    await db.commit()
    await db.refresh(event)
    return _event_dict(event)
