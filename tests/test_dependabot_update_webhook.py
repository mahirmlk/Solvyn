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
from solvyn.db.models import ProactiveEvent

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
        "repository": {"full_name": "Octo/Repo"},
        "pull_request": {
            "number": 123,
            "state": "open",
            "title": "Bump fastapi from 0.114 to 0.115",
            "body": "This body is untrusted and must never be stored.",
            "html_url": "https://github.com/Octo/Repo/pull/123",
            "head": {"ref": "dependabot/pip/fastapi-0.115"},
            "user": {"login": login},
        },
    }


def test_dependabot_pull_request_is_classified_from_bounded_metadata_only() -> None:
    parsed = _parse_github_work_item("pull_request", _payload())

    assert parsed == {
        "repository_name": "Octo/Repo",
        "source_event_id": "dependabot_update:octo/repo:123:opened",
        "workflow_name": "Bump fastapi from 0.114 to 0.115",
        "conclusion": "open",
        "branch": "dependabot/pip/fastapi-0.115",
        "details_url": "https://github.com/Octo/Repo/pull/123",
        "action": "opened",
        "event_type": "dependabot_update",
    }
    assert all("untrusted" not in value for value in parsed.values())


@pytest.mark.parametrize("login", ["dependabot", "dependabot[bot]evil", "octocat"])
def test_non_dependabot_pull_requests_keep_the_existing_event_type(login: str) -> None:
    parsed = _parse_github_work_item("pull_request", _payload(login=login))

    assert parsed is not None
    assert parsed["source_event_id"] == "pull_request:octo/repo:123:opened"
    assert "event_type" not in parsed


@pytest.mark.asyncio
async def test_signed_dependabot_update_signal_is_persisted_and_replay_deduplicated() -> None:
    body = json.dumps(_payload()).encode()
    tenant_id, project_id, organization_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.added: list[ProactiveEvent] = []
            self.persisted: ProactiveEvent | None = None

        async def execute(self, _query: object) -> Any:
            self.execute_count += 1
            if self.execute_count == 1:
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [tenant_id]))
            if self.execute_count == 2:
                return SimpleNamespace(all=lambda: [(project_id, organization_id, tenant_id)])
            return SimpleNamespace(scalar_one_or_none=lambda: self.persisted)

        def add(self, event: ProactiveEvent) -> None:
            event.id = uuid.uuid4()
            self.added.append(event)

        async def commit(self) -> None:
            if self.added:
                self.persisted = self.added[-1]

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
    assert replay["duplicate_count"] == 1
    assert len(db.added) == 1
    event = db.added[0]
    assert event.event_type == "dependabot_update"
    assert event.source_event_id == "dependabot_update:octo/repo:123:opened"
    assert event.workflow_name == "Bump fastapi from 0.114 to 0.115"
    assert event.branch == "dependabot/pip/fastapi-0.115"
    assert event.details_url == "https://github.com/Octo/Repo/pull/123"
    assert event.status == "pending"
    assert "untrusted and must never be stored" not in repr(event.__dict__)
    assert bind_tenant.await_count >= 2
