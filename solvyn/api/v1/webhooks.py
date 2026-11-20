"""Signed provider webhooks for human-in-the-loop engineering triage."""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
import uuid
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1._rbac import ANY_ROLE, OWNER_ADMIN, require_org_membership
from solvyn.config import settings
from solvyn.db.models import (
    GitHubWorkItemState,
    Organization,
    ProactiveEvent,
    ProactiveEventTransition,
    Project,
    Repository,
    Tenant,
)
from solvyn.db.session import bind_tenant_scope, get_session
from solvyn.proactive_dependabot import dependabot_alert_signal
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


def _parse_github_work_item(event_type: str, payload: Any) -> dict[str, Any] | None:
    """Parse bounded lifecycle metadata from a signed issue/PR webhook."""
    if event_type not in {"issues", "pull_request"} or not isinstance(payload, dict):
        return None
    action = payload.get("action")
    allowed_actions = {"opened", "edited", "closed", "reopened"}
    if event_type == "pull_request":
        allowed_actions.add("synchronize")
    if not isinstance(action, str) or action not in allowed_actions:
        return None
    item_key = "issue" if event_type == "issues" else "pull_request"
    item_value = payload.get(item_key)
    item = item_value if isinstance(item_value, dict) else {}
    repository_value = payload.get("repository")
    repository = repository_value if isinstance(repository_value, dict) else {}
    full_name = repository.get("full_name")
    number = item.get("number")
    state = item.get("state")
    if (
        not isinstance(full_name, str)
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number < 1
        or state not in {"open", "closed"}
        or (event_type == "issues" and "pull_request" in item)
    ):
        return None
    if action in {"opened", "reopened", "synchronize"} and state != "open":
        return None
    if action == "closed" and state != "closed":
        return None

    updated_at_value = item.get("updated_at")
    if not isinstance(updated_at_value, str):
        return None
    try:
        provider_updated_at = datetime.fromisoformat(updated_at_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if provider_updated_at.tzinfo is None or provider_updated_at.utcoffset() is None:
        return None
    provider_updated_at = provider_updated_at.astimezone(UTC)

    owner, repo = validate_full_name(full_name)
    owner_repo = f"{owner}/{repo}"
    user_value = item.get("user")
    user = user_value if isinstance(user_value, dict) else {}
    is_dependabot_update = event_type == "pull_request" and isinstance(user.get("login"), str) and user["login"].lower() == "dependabot[bot]"
    signal_type = "dependabot_update" if is_dependabot_update else event_type
    title = _bounded_text(item.get("title"), limit=255)
    label = "Pull request" if event_type == "pull_request" else "Issue"
    title = title or f"{label} #{number}"
    details_url = item.get("html_url")
    if (
        not isinstance(details_url, str)
        or urlparse(details_url).hostname != "github.com"
        or urlparse(details_url).scheme != "https"
    ):
        details_url = ""
    branch = ""
    if event_type == "pull_request":
        head_value = item.get("head")
        head = head_value if isinstance(head_value, dict) else {}
        branch = _bounded_text(head.get("ref"), limit=255)

    merged = event_type == "pull_request" and item.get("merged") is True and state == "closed"
    lifecycle_state = "merged" if merged else state
    source_type = event_type
    source_event_id = (
        f"{signal_type}:{owner_repo.lower()}:{number}:{action}:"
        f"{provider_updated_at.strftime('%Y%m%dT%H%M%S%fZ')}"
    )
    return {
        "repository_name": owner_repo[:255],
        "repository_key": owner_repo.lower()[:255],
        "source_event_id": source_event_id[:320],
        "item_number": number,
        "item_event_type": source_type,
        "provider_updated_at": provider_updated_at,
        "lifecycle_state": lifecycle_state,
        "workflow_name": title,
        "conclusion": lifecycle_state,
        "branch": branch,
        "details_url": details_url[:512],
        "action": action,
        **({"event_type": signal_type} if is_dependabot_update else {}),
    }


def _bounded_text(value: Any, *, limit: int, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    cleaned = "".join(character for character in value if unicodedata.category(character) != "Cc").strip()
    return cleaned[:limit] if cleaned else fallback


def _parse_security_alert(event_type: str, payload: Any) -> dict[str, str] | None:
    if event_type not in {"dependabot_alert", "code_scanning_alert", "secret_scanning_alert"}:
        return None
    if not isinstance(payload, dict):
        return None
    action = _bounded_text(payload.get("action"), limit=100)
    alert_value = payload.get("alert")
    alert = alert_value if isinstance(alert_value, dict) else {}
    if not action or not alert:
        return None
    if event_type == "dependabot_alert":
        repository_value = payload.get("repository")
        repository = repository_value if isinstance(repository_value, dict) else {}
        full_name = repository.get("full_name")
        return dependabot_alert_signal(full_name, alert, action=action) if isinstance(full_name, str) else None
    # Resolved alerts are not new work for the human triage inbox.
    if action.lower() in {"fixed", "closed_by_user", "dismissed", "resolved", "resolved_by_user", "auto_dismissed"}:
        return None
    state = alert.get("state")
    if isinstance(state, str) and state.lower() not in {"open", "reopened"}:
        return None

    repository_value = payload.get("repository")
    repository = repository_value if isinstance(repository_value, dict) else {}
    full_name = repository.get("full_name")
    if not isinstance(full_name, str):
        return None
    owner, repo = validate_full_name(full_name)
    owner_repo = f"{owner}/{repo}"

    alert_id = alert.get("number", alert.get("id"))
    if isinstance(alert_id, bool) or not isinstance(alert_id, int) or alert_id < 1:
        return None

    rule_value = alert.get("rule")
    rule = rule_value if isinstance(rule_value, dict) else {}
    advisory_value = alert.get("security_advisory")
    advisory = advisory_value if isinstance(advisory_value, dict) else {}
    vulnerability_value = advisory.get("vulnerabilities")
    vulnerability = vulnerability_value[0] if isinstance(vulnerability_value, list) and vulnerability_value and isinstance(vulnerability_value[0], dict) else {}
    package_value = vulnerability.get("package")
    package = package_value if isinstance(package_value, dict) else {}
    dependency_value = alert.get("dependency")
    dependency = dependency_value if isinstance(dependency_value, dict) else {}
    dependency_package_value = dependency.get("package")
    dependency_package = dependency_package_value if isinstance(dependency_package_value, dict) else {}
    security_vulnerability_value = advisory.get("severity")
    severity = security_vulnerability_value if isinstance(security_vulnerability_value, str) else ""
    if not severity:
        severity = _bounded_text(alert.get("security_severity_level"), limit=50) or _bounded_text(rule.get("security_severity_level"), limit=50)
    if not severity:
        severity = _bounded_text(vulnerability.get("severity"), limit=50, fallback="unknown")

    summary = (
        _bounded_text(advisory.get("summary"), limit=255)
        or _bounded_text(rule.get("description"), limit=255)
        or _bounded_text(rule.get("name"), limit=255)
        or _bounded_text(alert.get("secret_type_display_name"), limit=255)
        or _bounded_text(package.get("name"), limit=255)
        or _bounded_text(dependency_package.get("name"), limit=255)
        or f"{event_type.replace('_', ' ').title()} #{alert_id}"
    )
    details_url = alert.get("html_url")
    if not isinstance(details_url, str) or urlparse(details_url).hostname != "github.com" or urlparse(details_url).scheme != "https":
        details_url = ""
    branch = _bounded_text(alert.get("ref"), limit=255)
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    return {
        "repository_name": owner_repo[:255],
        "source_event_id": f"{event_type}:{owner_repo.lower()}:{alert_id}:{action}"[:320],
        "workflow_name": summary,
        "conclusion": severity[:50],
        "branch": branch,
        "details_url": details_url[:512],
        "action": action,
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
        "summary": event.workflow_name,
        "conclusion": event.conclusion,
        "severity": event.conclusion,
        "branch": event.branch,
        "details_url": event.details_url,
        "status": event.status,
        "provider_state": None,
        "provider_updated_at": None,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


def _work_item_identity(event: ProactiveEvent) -> tuple[uuid.UUID, str, str, int] | None:
    """Recover the stable issue/PR identity from current and legacy signal IDs."""
    parts = event.source_event_id.split(":")
    if len(parts) < 4:
        return None
    event_type = "pull_request" if parts[0] == "dependabot_update" else parts[0]
    if event_type not in {"issues", "pull_request"}:
        return None
    try:
        item_number = int(parts[2])
    except ValueError:
        return None
    if item_number < 1:
        return None
    return event.project_id, event_type, parts[1].lower(), item_number


async def _work_item_state_for_events(
    db: AsyncSession,
    organization_id: uuid.UUID,
    events: list[ProactiveEvent],
) -> dict[tuple[uuid.UUID, str, str, int], GitHubWorkItemState]:
    identities = {identity for event in events if (identity := _work_item_identity(event)) is not None}
    if not identities:
        return {}
    result = await db.execute(
        select(GitHubWorkItemState).where(
            GitHubWorkItemState.organization_id == organization_id,
            tuple_(
                GitHubWorkItemState.project_id,
                GitHubWorkItemState.event_type,
                GitHubWorkItemState.repository_key,
                GitHubWorkItemState.item_number,
            ).in_(identities),
        )
    )
    states = result.scalars().all()
    return {
        (state.project_id, state.event_type, state.repository_key, state.item_number): state
        for state in states
        if (state.project_id, state.event_type, state.repository_key, state.item_number) in identities
    }


def _event_dict_with_state(
    event: ProactiveEvent,
    states: dict[tuple[uuid.UUID, str, str, int], GitHubWorkItemState],
) -> dict[str, object]:
    result = _event_dict(event)
    identity = _work_item_identity(event)
    state = states.get(identity) if identity else None
    if state is not None:
        result["provider_state"] = state.lifecycle_state
        result["provider_updated_at"] = state.provider_updated_at.isoformat()
        result["summary"] = state.title
        result["conclusion"] = state.lifecycle_state
        result["branch"] = state.branch
        result["details_url"] = state.details_url
    return result


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
    supported_event_types = {
        "workflow_run",
        "dependabot_alert",
        "code_scanning_alert",
        "secret_scanning_alert",
        "issues",
        "pull_request",
    }
    if event_type not in supported_event_types:
        return {"accepted": True, "ignored": True, "reason": "unsupported_event"}
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GitHub webhook JSON") from exc
    try:
        if event_type == "workflow_run":
            signal = _parse_workflow_failure(payload)
        elif event_type in {"issues", "pull_request"}:
            signal = _parse_github_work_item(event_type, payload)
        else:
            signal = _parse_security_alert(event_type, payload)
    except GitHubRepositoryError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    if signal is None:
        if event_type == "workflow_run":
            reason = "not_a_failed_workflow_run"
        elif event_type in {"issues", "pull_request"}:
            reason = "not_an_open_github_work_item"
        else:
            reason = "not_an_open_security_alert"
        return {"accepted": True, "ignored": True, "reason": reason}

    active_tenants = (
        await db.execute(select(Tenant.id).where(Tenant.is_active == True).order_by(Tenant.id))  # noqa: E712
    ).scalars().all()
    project_matches: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = []
    for tenant_id in active_tenants:
        await bind_tenant_scope(db, tenant_id)
        tenant_matches = (
            await db.execute(
                select(Project.id, Project.organization_id, Organization.tenant_id)
                .join(Repository, Repository.project_id == Project.id)
                .join(Organization, Organization.id == Project.organization_id)
                .where(
                    Repository.provider == "github",
                    Repository.is_active == True,  # noqa: E712
                    Project.is_active == True,  # noqa: E712
                    Organization.is_active == True,  # noqa: E712
                    Project.tenant_id == tenant_id,
                    # GitHub full names are case-insensitive; equality avoids LIKE wildcards.
                    func.lower(Repository.full_name) == signal["repository_name"].lower(),
                )
                .limit(_MAX_PROJECT_MATCHES + 1)
            )
        ).all()
        project_matches.extend((match[0], match[1], match[2]) for match in tenant_matches)
        if len(project_matches) > _MAX_PROJECT_MATCHES:
            raise HTTPException(status_code=413, detail="Repository is connected to too many projects for webhook fan-out")
    if not project_matches:
        return {"accepted": True, "ignored": True, "reason": "repository_not_connected"}

    matches_by_tenant: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID]]] = {}
    for project_id, organization_id, tenant_id in project_matches:
        matches_by_tenant.setdefault(tenant_id, []).append((project_id, organization_id))

    async def _record_events() -> tuple[list[tuple[uuid.UUID, ProactiveEvent]], int, int]:
        events: list[tuple[uuid.UUID, ProactiveEvent]] = []
        duplicates = 0
        processed_transitions = 0
        for tenant_id, matches in matches_by_tenant.items():
            await bind_tenant_scope(db, tenant_id)
            for project_id, organization_id in matches:
                if event_type in {"issues", "pull_request"}:
                    provider_updated_at = cast(datetime, signal["provider_updated_at"])
                    existing_transition = (
                        await db.execute(
                            select(ProactiveEventTransition.id).where(
                                ProactiveEventTransition.organization_id == organization_id,
                                ProactiveEventTransition.project_id == project_id,
                                ProactiveEventTransition.delivery_id == normalized_delivery_id,
                            )
                        )
                    ).scalar_one_or_none()
                    if existing_transition is not None:
                        duplicates += 1
                        if signal["action"] in {"opened", "reopened"}:
                            existing_signal = (
                                await db.execute(
                                    select(ProactiveEvent).where(
                                        ProactiveEvent.organization_id == organization_id,
                                        ProactiveEvent.project_id == project_id,
                                        ProactiveEvent.delivery_id == normalized_delivery_id,
                                    )
                                )
                            ).scalar_one_or_none()
                            if existing_signal is not None:
                                events.append((tenant_id, existing_signal))
                        continue

                    identity_filter = (
                        GitHubWorkItemState.organization_id == organization_id,
                        GitHubWorkItemState.project_id == project_id,
                        GitHubWorkItemState.event_type == signal["item_event_type"],
                        GitHubWorkItemState.repository_key == signal["repository_key"],
                        GitHubWorkItemState.item_number == signal["item_number"],
                    )
                    state = (
                        await db.execute(
                            select(GitHubWorkItemState).where(*identity_filter).with_for_update()
                        )
                    ).scalar_one_or_none()
                    applied_to_current = state is None
                    if state is None:
                        state = GitHubWorkItemState(
                            organization_id=organization_id,
                            project_id=project_id,
                            event_type=signal["item_event_type"],
                            repository_key=signal["repository_key"],
                            repository_name=signal["repository_name"],
                            item_number=signal["item_number"],
                            title=signal["workflow_name"],
                            lifecycle_state=signal["lifecycle_state"],
                            branch=signal["branch"],
                            details_url=signal["details_url"],
                            provider_updated_at=provider_updated_at,
                            last_delivery_id=normalized_delivery_id,
                        )
                        db.add(state)
                    elif provider_updated_at > state.provider_updated_at:
                        state.repository_name = signal["repository_name"]
                        state.title = signal["workflow_name"]
                        state.lifecycle_state = signal["lifecycle_state"]
                        state.branch = signal["branch"]
                        state.details_url = signal["details_url"]
                        state.provider_updated_at = provider_updated_at
                        state.last_delivery_id = normalized_delivery_id
                        applied_to_current = True

                    db.add(
                        ProactiveEventTransition(
                            organization_id=organization_id,
                            project_id=project_id,
                            delivery_id=normalized_delivery_id,
                            event_type=signal["item_event_type"],
                            action=signal["action"],
                            repository_key=signal["repository_key"],
                            repository_name=signal["repository_name"],
                            item_number=signal["item_number"],
                            title=signal["workflow_name"],
                            lifecycle_state=signal["lifecycle_state"],
                            branch=signal["branch"],
                            details_url=signal["details_url"],
                            provider_updated_at=provider_updated_at,
                            applied_to_current=applied_to_current,
                        )
                    )
                    processed_transitions += 1
                    if signal["action"] not in {"opened", "reopened"} or not applied_to_current:
                        continue

                existing = (
                    await db.execute(
                        select(ProactiveEvent).where(
                            ProactiveEvent.organization_id == organization_id,
                            ProactiveEvent.project_id == project_id,
                            (ProactiveEvent.delivery_id == normalized_delivery_id)
                            | (ProactiveEvent.source_event_id == signal["source_event_id"]),
                        )
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        event_type == "dependabot_alert"
                        and signal.get("action") == "reopened"
                        and existing.status == "acknowledged"
                        and existing.delivery_id != normalized_delivery_id
                    ):
                        # Reuse the stable alert row, but reopen human triage
                        # once per distinct GitHub delivery. Recording that
                        # delivery makes a replay idempotent even after a user
                        # acknowledges the reopened alert.
                        existing.delivery_id = normalized_delivery_id
                        existing.event_type = event_type
                        existing.action = "reopened"
                        existing.repository_name = signal["repository_name"]
                        existing.workflow_name = signal["workflow_name"]
                        existing.conclusion = signal["conclusion"]
                        existing.branch = signal["branch"]
                        existing.details_url = signal["details_url"]
                        existing.status = "pending"
                    duplicates += 1
                    events.append((tenant_id, existing))
                    continue
                event = ProactiveEvent(
                    organization_id=organization_id,
                    project_id=project_id,
                    delivery_id=normalized_delivery_id,
                    source_event_id=signal["source_event_id"],
                    event_type=signal.get("event_type", event_type),
                    action=signal.get("action", "completed"),
                    repository_name=signal["repository_name"],
                    workflow_name=signal["workflow_name"],
                    conclusion=signal["conclusion"],
                    branch=signal["branch"],
                    details_url=signal["details_url"],
                    status="pending",
                )
                db.add(event)
                events.append((tenant_id, event))
            # Flush each tenant's inserts while its transaction-local RLS scope is active.
            await db.flush()
        return events, duplicates, processed_transitions

    try:
        events, duplicate_count, processed_count = await _record_events()
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Rebuild the fan-out after a unique-key race. The failed transaction
        # may have rolled back other projects' rows, so retry every missing one.
        for attempt in range(3):
            try:
                events, duplicate_count, processed_count = await _record_events()
                await db.commit()
                break
            except IntegrityError:
                await db.rollback()
        else:
            raise HTTPException(status_code=503, detail="GitHub delivery could not be recorded; retry the webhook")
    for event in events:
        await bind_tenant_scope(db, event[0])
        await db.refresh(event[1])
    return {
        "accepted": True,
        "ignored": False,
        "created_count": max(0, len(events) - duplicate_count),
        "duplicate_count": duplicate_count,
        "processed_transition_count": processed_count,
        "event_ids": [str(event.id) for _, event in events],
    }


@router.get("/organizations/{organization_id}/proactive-events", summary="List verified proactive events")
async def list_proactive_events(
    organization_id: uuid.UUID,
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
) -> dict[str, object]:
    membership = await require_org_membership(db, organization_id, roles=ANY_ROLE)
    if not 1 <= limit <= 100:
        raise HTTPException(status_code=422, detail="Limit must be between 1 and 100")
    result = await db.execute(
        select(ProactiveEvent)
        .where(ProactiveEvent.organization_id == organization_id)
        .order_by(ProactiveEvent.created_at.desc())
        .limit(limit)
    )
    events = list(result.scalars().all())
    states = await _work_item_state_for_events(db, organization_id, events)
    items = [_event_dict_with_state(event, states) for event in events]
    return {
        "events": items,
        "count": len(items),
        "content_trust": "untrusted_github_event",
        "can_manage": membership.role in OWNER_ADMIN,
    }


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
    states = await _work_item_state_for_events(db, organization_id, [event])
    return _event_dict_with_state(event, states)
