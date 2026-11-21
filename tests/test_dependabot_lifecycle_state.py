from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from solvyn.db.models import GitHubDependabotAlertState, ProactiveEvent
from solvyn.proactive_dependabot import dependabot_alert_signal
from solvyn.proactive_dependabot_state import polling_delivery_id, record_dependabot_alert


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object | None:
        return self.value


class _AlertStore:
    """Small async-session double for the lifecycle transition helper."""

    def __init__(self) -> None:
        self.state: GitHubDependabotAlertState | None = None
        self.event: ProactiveEvent | None = None
        self.added: list[object] = []

    async def execute(self, query: object) -> _Result:
        entity = query.column_descriptions[0]["entity"]  # type: ignore[attr-defined]
        return _Result(self.state if entity is GitHubDependabotAlertState else self.event)

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, GitHubDependabotAlertState):
            self.state = value
        elif isinstance(value, ProactiveEvent):
            self.event = value


def _alert(updated_at: str, *, state: str = "open") -> dict[str, Any]:
    return {
        "number": 73,
        "state": state,
        "updated_at": updated_at,
        "security_advisory": {"summary": "Prototype pollution", "severity": "high"},
        "html_url": "https://github.com/octo/repo/security/dependabot/73",
    }


async def _record(
    store: _AlertStore,
    alert: dict[str, Any],
    *,
    source: str = "poll",
    action: str = "created",
    delivery_id: str | None = None,
) -> tuple[ProactiveEvent | None, bool]:
    signal = dependabot_alert_signal("octo/repo", alert, action=action)
    assert signal is not None
    return await record_dependabot_alert(
        store,  # type: ignore[arg-type]
        organization_id=uuid.UUID(int=1),
        project_id=uuid.UUID(int=2),
        repository_id=uuid.UUID(int=3),
        signal=signal,
        delivery_id=delivery_id or polling_delivery_id(signal["source_event_id"], signal["provider_updated_at"]),
        source=source,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_close_and_reopen_between_polls_rearms_acknowledged_alert_once() -> None:
    store = _AlertStore()
    first, created = await _record(store, _alert("2026-11-20T10:00:00Z"))
    assert created and first is not None and first.status == "pending"
    first.status = "acknowledged"

    # Even when the endpoint includes explicit closed states, a close and
    # reopen between snapshots may be represented only by a later open marker.
    reopened, created_again = await _record(store, _alert("2026-11-22T10:00:00Z"))
    assert not created_again and reopened is first
    assert first.status == "pending"
    assert first.action == "reopened"
    assert store.state is not None
    assert store.state.provider_updated_at == datetime(2026, 11, 22, 10, tzinfo=UTC)
    assert store.state.last_reopen_marker == datetime(2026, 11, 22, 10, tzinfo=UTC)

    first.status = "acknowledged"
    repeated, created_repeated = await _record(store, _alert("2026-11-22T10:00:00Z"))
    assert not created_repeated and repeated is first
    assert first.status == "acknowledged"


@pytest.mark.asyncio
async def test_poll_recovers_missed_reopen_webhook_and_late_webhook_does_not_rearm_twice() -> None:
    store = _AlertStore()
    event, created = await _record(store, _alert("2026-11-20T10:00:00Z"))
    assert created and event is not None
    event.status = "acknowledged"

    # No webhook arrived. A polling snapshot with a newer marker recovers it.
    _, _ = await _record(store, _alert("2026-11-22T10:00:00Z"))
    assert event.status == "pending"
    event.status = "acknowledged"

    # A delayed webhook for that same reopen must not re-arm the row again,
    # even though GitHub assigns it a different delivery UUID.
    _, _ = await _record(
        store,
        _alert("2026-11-22T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert event.status == "acknowledged"


@pytest.mark.asyncio
async def test_distinct_webhook_reopen_marker_rearms_once_across_poll_and_delivery_replay() -> None:
    store = _AlertStore()
    event, created = await _record(store, _alert("2026-11-20T10:00:00Z"))
    assert created and event is not None
    event.status = "acknowledged"

    reopened, _ = await _record(
        store,
        _alert("2026-11-22T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert reopened is event and event.status == "pending"
    event.status = "acknowledged"

    # Poll and a webhook replay for the same updated_at are both duplicates.
    await _record(store, _alert("2026-11-22T10:00:00Z"))
    await _record(
        store,
        _alert("2026-11-22T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert event.status == "acknowledged"

    # An older webhook arriving after a newer poll snapshot cannot rewind or
    # re-arm the current alert lifecycle.
    await _record(
        store,
        _alert("2026-11-21T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert event.status == "acknowledged"
    assert store.state is not None
    assert store.state.provider_updated_at == datetime(2026, 11, 22, 10, tzinfo=UTC)

    later, _ = await _record(
        store,
        _alert("2026-11-23T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert later is event and event.status == "pending"


@pytest.mark.asyncio
async def test_closed_webhook_persists_state_without_creating_a_triage_signal() -> None:
    store = _AlertStore()
    closed, created = await _record(
        store,
        _alert("2026-11-20T10:00:00Z", state="dismissed"),
        source="webhook",
        action="dismissed",
        delivery_id=str(uuid.uuid4()),
    )
    assert closed is None and not created
    assert store.state is not None and store.state.lifecycle_state == "dismissed"
    assert store.event is None

    reopened, created = await _record(
        store,
        _alert("2026-11-22T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert reopened is not None and created
    assert reopened.status == "pending"
    assert store.state is not None
    assert store.state.last_reopen_marker == datetime(2026, 11, 22, 10, tzinfo=UTC)


@pytest.mark.asyncio
async def test_explicit_close_resolves_existing_signal_and_new_open_revision_rearms_it() -> None:
    store = _AlertStore()
    event, created = await _record(store, _alert("2026-11-20T10:00:00Z"))
    assert created and event is not None
    event.status = "acknowledged"

    await _record(
        store,
        _alert("2026-11-21T10:00:00Z", state="dismissed"),
        source="poll",
        action="created",
    )
    assert event.status == "resolved"
    assert event.action == "created"
    assert store.state is not None and store.state.lifecycle_state == "dismissed"

    reopened, created_again = await _record(store, _alert("2026-11-22T10:00:00Z"))
    assert not created_again and reopened is event
    assert event.status == "pending"
    assert event.action == "reopened"


@pytest.mark.asyncio
async def test_stale_or_tied_poll_cannot_resurrect_closed_alert_without_reopen_webhook() -> None:
    store = _AlertStore()
    await _record(
        store,
        _alert("2026-11-22T10:00:00Z", state="fixed"),
        source="webhook",
        action="fixed",
        delivery_id=str(uuid.uuid4()),
    )

    stale, _ = await _record(store, _alert("2026-11-21T10:00:00Z"))
    tied_poll, _ = await _record(store, _alert("2026-11-22T10:00:00Z"))
    assert stale is None and tied_poll is None
    assert store.event is None

    tied_reopen, created = await _record(
        store,
        _alert("2026-11-22T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert created and tied_reopen is not None
    assert tied_reopen.status == "pending"
    assert tied_reopen.action == "reopened"
    assert store.state is not None and store.state.lifecycle_state == "open"


@pytest.mark.asyncio
async def test_poll_reopen_with_no_existing_event_deduplicates_late_same_marker_webhook() -> None:
    store = _AlertStore()
    await _record(
        store,
        _alert("2026-11-21T10:00:00Z", state="dismissed"),
        source="webhook",
        action="dismissed",
        delivery_id=str(uuid.uuid4()),
    )

    event, created = await _record(store, _alert("2026-11-22T10:00:00Z"))
    assert created and event is not None
    assert store.state is not None
    assert store.state.last_reopen_marker == datetime(2026, 11, 22, 10, tzinfo=UTC)
    event.status = "acknowledged"

    await _record(
        store,
        _alert("2026-11-22T10:00:00Z"),
        source="webhook",
        action="reopened",
        delivery_id=str(uuid.uuid4()),
    )
    assert event.status == "acknowledged"


def test_alert_signal_requires_and_normalizes_provider_update_marker() -> None:
    signal = dependabot_alert_signal("Octo/Repo", _alert("2026-11-22T10:00:00-05:00"))
    assert signal is not None
    assert signal["repository_name"] == "Octo/Repo"
    assert signal["provider_updated_at"] == datetime(2026, 11, 22, 15, tzinfo=UTC)
    assert signal["lifecycle_state"] == "open"
    assert signal["notify"] is True
    assert dependabot_alert_signal("octo/repo", {**_alert("2026-11-22T10:00:00Z"), "updated_at": "invalid"}) is None

    auto_dismissed = dependabot_alert_signal(
        "octo/repo",
        _alert("2026-11-22T10:00:00Z", state="auto_dismissed"),
    )
    assert auto_dismissed is not None
    assert auto_dismissed["lifecycle_state"] == "dismissed"
    assert auto_dismissed["notify"] is False


def test_resolved_alert_webhook_updates_lifecycle_without_a_new_triage_signal() -> None:
    signal = dependabot_alert_signal(
        "octo/repo",
        _alert("2026-11-22T10:00:00Z", state="dismissed"),
        action="dismissed",
    )
    assert signal is not None
    assert signal["lifecycle_state"] == "dismissed"
    assert signal["notify"] is False

