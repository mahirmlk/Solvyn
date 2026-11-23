"""Persist and reconcile the latest Dependabot lifecycle marker."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.db.models import GitHubDependabotAlertState, ProactiveEvent


async def record_dependabot_alert(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    project_id: uuid.UUID,
    repository_id: uuid.UUID,
    signal: dict[str, Any],
    delivery_id: str,
    source: Literal["webhook", "poll"],
) -> tuple[ProactiveEvent | None, bool]:
    """Persist an alert snapshot and re-arm an acknowledged signal once per revision.

    Polling includes explicit open and provider-closed states. A newer open
    ``updated_at`` after a known closed state recovers a close/reopen sequence
    whose reopen webhook was lost. Missing alerts and incomplete scans never
    imply closure. Webhook and poll observations with the same provider marker
    converge on one alert row and one reopen.
    """
    alert_number = int(signal["alert_number"])
    repository_key = str(signal["repository_name"]).lower()
    marker = signal["provider_updated_at"]
    lifecycle_state = str(signal["lifecycle_state"])
    action = str(signal["action"])
    if not isinstance(marker, datetime) or marker.tzinfo is None or marker.utcoffset() is None:
        raise ValueError("Dependabot alert update marker must be timezone-aware")

    state = (
        await db.execute(
            select(GitHubDependabotAlertState)
            .where(
                GitHubDependabotAlertState.organization_id == organization_id,
                GitHubDependabotAlertState.project_id == project_id,
                GitHubDependabotAlertState.repository_id == repository_id,
                GitHubDependabotAlertState.alert_number == alert_number,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    previous_lifecycle_state = state.lifecycle_state if state is not None else None
    event = (
        await db.execute(
            select(ProactiveEvent)
            .where(
                ProactiveEvent.organization_id == organization_id,
                ProactiveEvent.project_id == project_id,
                ProactiveEvent.source_event_id == signal["source_event_id"],
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    if state is None:
        state = GitHubDependabotAlertState(
            organization_id=organization_id,
            project_id=project_id,
            repository_id=repository_id,
            repository_key=repository_key,
            repository_name=signal["repository_name"],
            alert_number=alert_number,
            title=signal["workflow_name"],
            severity=signal["conclusion"],
            branch=signal["branch"],
            details_url=signal["details_url"],
            lifecycle_state=lifecycle_state,
            provider_updated_at=marker,
            last_reopen_marker=None,
            last_delivery_id=delivery_id,
        )
        db.add(state)
        newer_marker = False
    else:
        newer_marker = marker > state.provider_updated_at
        tied_explicit_reopen = (
            marker == state.provider_updated_at
            and lifecycle_state == "open"
            and action == "reopened"
        )
        if newer_marker or tied_explicit_reopen:
            state.repository_key = repository_key
            state.repository_name = signal["repository_name"]
            state.title = signal["workflow_name"]
            state.severity = signal["conclusion"]
            state.branch = signal["branch"]
            state.details_url = signal["details_url"]
            state.lifecycle_state = lifecycle_state
            state.provider_updated_at = marker
            state.last_delivery_id = delivery_id

    if lifecycle_state != "open":
        # A provider closure is authoritative for the existing triage card.
        # Older closure webhooks must not resolve a newer open revision.
        if (
            event is not None
            and state.provider_updated_at == marker
            and state.lifecycle_state == lifecycle_state
            and event.event_type == "dependabot_alert"
        ):
            event.status = "resolved"
            event.action = action
            event.conclusion = lifecycle_state
        return None, False

    if state is not None and state.lifecycle_state != "open":
        stale_marker = marker < state.provider_updated_at
        tied_without_reopen = marker == state.provider_updated_at and not (
            source == "webhook" and action == "reopened"
        )
        if stale_marker or tied_without_reopen:
            # A delayed open delivery must not resurrect a revision already
            # known to be closed. At equal provider precision, only an
            # explicit reopen webhook is sufficient evidence.
            return event, False

    if event is None:
        if action == "reopened" or (
            source == "poll"
            and newer_marker
            and previous_lifecycle_state is not None
            and previous_lifecycle_state != "open"
        ):
            state.last_reopen_marker = marker
        event = ProactiveEvent(
            organization_id=organization_id,
            project_id=project_id,
            delivery_id=delivery_id[:128],
            source_event_id=signal["source_event_id"],
            event_type="dependabot_alert",
            action=action,
            repository_name=signal["repository_name"],
            workflow_name=signal["workflow_name"],
            conclusion=signal["conclusion"],
            branch=signal["branch"],
            details_url=signal["details_url"],
            status="pending",
        )
        db.add(event)
        return event, True

    last_reopen_marker = state.last_reopen_marker
    explicit_reopen = (
        action == "reopened"
        and marker >= state.provider_updated_at
        and (last_reopen_marker is None or marker > last_reopen_marker)
    )
    recovered_reopen = (
        source == "poll"
        and newer_marker
        and previous_lifecycle_state is not None
        and previous_lifecycle_state != "open"
        and event.status in {"acknowledged", "resolved"}
    )
    distinct_reopen = explicit_reopen or recovered_reopen
    if distinct_reopen:
        state.last_reopen_marker = marker
        if event.status in {"acknowledged", "resolved"}:
            event.delivery_id = delivery_id[:128]
            event.action = "reopened"
            event.repository_name = signal["repository_name"]
            event.workflow_name = signal["workflow_name"]
            event.conclusion = signal["conclusion"]
            event.branch = signal["branch"]
            event.details_url = signal["details_url"]
            event.status = "pending"
        return event, False

    # Keep the inbox row's details fresh without changing its acknowledgement.
    if newer_marker:
        event.repository_name = signal["repository_name"]
        event.workflow_name = signal["workflow_name"]
        event.conclusion = signal["conclusion"]
        event.branch = signal["branch"]
        event.details_url = signal["details_url"]
    return event, False


def polling_delivery_id(source_event_id: str, marker: datetime) -> str:
    """Return a bounded deterministic delivery key for one provider revision."""
    revision = marker.isoformat()
    digest = hashlib.sha256(f"{source_event_id}:{revision}".encode()).hexdigest()
    return f"dependabot-poll:{digest}"
