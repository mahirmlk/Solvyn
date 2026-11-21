from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from solvyn.api.v1.webhooks import _parse_github_work_item, receive_github_webhook
from solvyn.db.models import GitHubWorkItemState, ProactiveEvent, ProactiveEventTransition

_SECRET = "dependabot-webhook-test-secret"


def _signature(body: bytes) -> str:
    digest = hmac.new(_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _request(body: bytes, *, delivery_id: uuid.UUID) -> Request:
    headers = [
        (b"content-type", b"application/json"),
        (b"x-github-event", b"pull_request"),
        (b"x-github-delivery", str(delivery_id).encode()),
        (b"x-hub-signature-256", _signature(body).encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/api/v1/webhooks/github",
        "raw_path": b"/api/v1/webhooks/github",
        "query_string": b"",
        "headers": headers,
        "server": ("example.test", 443),
        "client": ("127.0.0.1", 1234),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _payload(*, login: str = "dependabot[bot]") -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"id": 987654, "full_name": "Octo/Repo"},
        "installation": {
            "id": 812,
            "account": {"id": 456, "login": "Octo", "type": "Organization"},
        },
        "pull_request": {
            "number": 123,
            "state": "open",
            "updated_at": "2026-11-20T10:00:00Z",
            "title": "Bump fastapi from 0.114 to 0.115",
            "body": "This body is untrusted and must never be stored.",
            "html_url": "https://github.com/Octo/Repo/pull/123",
            "head": {"ref": "dependabot/pip/fastapi-0.115"},
            "user": {"login": login},
        },
    }


def test_dependabot_pull_request_is_classified_from_bounded_metadata_only() -> None:
    parsed = _parse_github_work_item("pull_request", _payload())

    assert parsed is not None
    assert parsed["repository_name"] == "Octo/Repo"
    assert parsed["source_event_id"].startswith("dependabot_update:octo/repo:123:opened:")
    assert parsed["repository_key"] == "octo/repo"
    assert parsed["item_number"] == 123
    assert parsed["item_event_type"] == "pull_request"
    assert parsed["lifecycle_state"] == "open"
    assert parsed["workflow_name"] == "Bump fastapi from 0.114 to 0.115"
    assert parsed["conclusion"] == "open"
    assert parsed["branch"] == "dependabot/pip/fastapi-0.115"
    assert parsed["details_url"] == "https://github.com/Octo/Repo/pull/123"
    assert parsed["action"] == "opened"
    assert parsed["event_type"] == "dependabot_update"
    assert all("untrusted" not in value for value in parsed.values() if isinstance(value, str))


@pytest.mark.parametrize("login", ["dependabot", "dependabot[bot]evil", "octocat"])
def test_non_dependabot_pull_requests_keep_the_existing_event_type(login: str) -> None:
    parsed = _parse_github_work_item("pull_request", _payload(login=login))

    assert parsed is not None
    assert parsed["source_event_id"].startswith("pull_request:octo/repo:123:opened:")
    assert "event_type" not in parsed


@pytest.mark.asyncio
async def test_signed_dependabot_update_signal_is_persisted_and_replay_deduplicated() -> None:
    body = json.dumps(_payload()).encode()
    tenant_id, project_id, organization_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.repository_match_query: Any | None = None
            self.added: list[Any] =
            self.persisted_event: ProactiveEvent | None = None
            self.persisted_state: GitHubWorkItemState | None = None

        async def execute(self, _query: object) -> Any:
            self.execute_count += 1
            if self.execute_count == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [tenant_id]))
            if self.execute_count == 2:
                self.repository_match_query = _query
                return SimpleNamespace(all=lambda: [(project_id, organization_id, tenant_id)])
            if self.execute_count == 4:
                return SimpleNamespace(scalar_one_or_none=lambda: self.persisted_state)
            if self.execute_count == 5:
                return SimpleNamespace(scalar_one_or_none=lambda: self.persisted_event)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, event: Any) -> None:
            if event.id is None:
                event.id = uuid.uuid4()
            self.added.append(event)
            if isinstance(event, ProactiveEvent):
                self.persisted_event = event
            elif isinstance(event, GitHubWorkItemState):
                self.persisted_state = event

        async def commit(self) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(self, _event: ProactiveEvent) -> None:
            return None

    db = StubDB()
    bind_tenant = AsyncMock()
    with (
        patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", _SECRET),
        patch("solvyn.api.v1.webhooks.bind_tenant_scope", bind_tenant),
    ):
        first = await receive_github_webhook(_request(body, delivery_id=uuid.uuid4()), db=cast(AsyncSession, db))
        db.execute_count = 0
        replay = await receive_github_webhook(_request(body, delivery_id=uuid.uuid4()), db=cast(AsyncSession, db))

    assert first["created_count"] == 1
    assert replay["created_count"] == 0
    assert replay["duplicate_count"] == 0
    assert replay["processed_transition_count"] == 1
    event = next(item for item in db.added if isinstance(item, ProactiveEvent))
    assert len([item for item in db.added if isinstance(item, ProactiveEvent)]) == 1
    transitions = [item for item in db.added if isinstance(item, ProactiveEventTransition)]
    assert len(transitions) == 2
    assert all(item.event_type == "pull_request" for item in transitions)
    assert event.event_type == "dependabot_update"
    assert event.source_event_id.startswith("dependabot_update:octo/repo:123:opened:")
    assert event.workflow_name == "Bump fastapi from 0.114 to 0.115"
    assert event.branch == "dependabot/pip/fastapi-0.115"
    assert event.details_url == "https://github.com/Octo/Repo/pull/123"
    assert event.status == "pending"
    assert "untrusted and must never be stored" not in repr(event.__dict__)
    assert bind_tenant.await_count >= 2
    assert db.repository_match_query is not None
    match_sql = str(db.repository_match_query)
    match_params = db.repository_match_query.compile().params.values()
    assert "integrations.organization_id = projects.organization_id" in match_sql
    assert {812, 456, 987654}.issubset(set(match_params))
