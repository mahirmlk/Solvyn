"""Tests for signed, idempotent GitHub CI failure event ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from solvyn.api.v1.webhooks import _parse_security_alert, _parse_workflow_failure, _valid_signature, receive_github_webhook
from solvyn.db.models import ProactiveEvent
from solvyn.multi_tenant.auth import RateLimitMiddleware


def _request(body: bytes, *, event: str = "workflow_run", delivery: str | None = None, signature: str = "") -> Request:
    delivery_id = delivery or str(uuid.uuid4())
    headers = [
        (b"content-length", str(len(body)).encode()),
        (b"x-github-event", event.encode()),
        (b"x-github-delivery", delivery_id.encode()),
        (b"x-hub-signature-256", signature.encode()),
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


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_github_webhook_signature_uses_sha256_hmac() -> None:
    body = b'{"action":"completed"}'
    signature = _signature("webhook-secret", body)
    assert _valid_signature("webhook-secret", body, signature)
    assert not _valid_signature("", body, signature)
    assert not _valid_signature("webhook-secret", body + b"x", signature)
    assert not _valid_signature("webhook-secret", body, "sha1=bad")


def test_workflow_failure_parser_keeps_only_bounded_safe_fields() -> None:
    parsed = _parse_workflow_failure(
        {
            "action": "completed",
            "repository": {"full_name": "Octo/Repo"},
            "workflow_run": {
                "id": 123,
                "run_attempt": 2,
                "conclusion": "failure",
                "name": "CI",
                "head_branch": "main",
                "html_url": "https://github.com/Octo/Repo/actions/runs/123",
            },
        }
    )
    assert parsed == {
        "repository_name": "Octo/Repo",
        "source_event_id": "octo/repo:123:2",
        "workflow_name": "CI",
        "conclusion": "failure",
        "branch": "main",
        "details_url": "https://github.com/Octo/Repo/actions/runs/123",
    }
    assert _parse_workflow_failure({"action": "completed", "workflow_run": {"conclusion": "success"}}) is None


def test_security_alert_parser_keeps_bounded_metadata_and_never_secret_values() -> None:
    parsed = _parse_security_alert(
        "secret_scanning_alert",
        {
            "action": "created",
            "repository": {"full_name": "Octo/Repo"},
            "alert": {
                "number": 42,
                "state": "open",
                "secret_type_display_name": "Example token",
                "secret": "must-not-be-stored",
                "html_url": "https://github.com/Octo/Repo/security/secret-scanning/42",
            },
        },
    )
    assert parsed == {
        "repository_name": "Octo/Repo",
        "source_event_id": "secret_scanning_alert:octo/repo:42:created",
        "workflow_name": "Example token",
        "conclusion": "unknown",
        "branch": "",
        "details_url": "https://github.com/Octo/Repo/security/secret-scanning/42",
        "action": "created",
    }
    assert all("must-not-be-stored" not in value for value in parsed.values())


def test_security_alert_parser_ignores_resolved_alerts_and_spoofed_urls() -> None:
    assert _parse_security_alert(
        "dependabot_alert",
        {
            "action": "fixed",
            "repository": {"full_name": "octo/repo"},
            "alert": {"number": 7, "state": "fixed"},
        },
    ) is None
    parsed = _parse_security_alert(
        "code_scanning_alert",
        {
            "action": "appeared_in_branch",
            "repository": {"full_name": "octo/repo"},
            "alert": {
                "number": 8,
                "state": "open",
                "rule": {"description": "Unsafe call", "security_severity_level": "high"},
                "html_url": "https://github.com.attacker.example/alert/8",
            },
        },
    )
    assert parsed is not None
    assert parsed["workflow_name"] == "Unsafe call"
    assert parsed["conclusion"] == "high"
    assert parsed["details_url"] == ""


@pytest.mark.asyncio
async def test_webhook_rejects_bad_signature_before_database_access() -> None:
    body = b"{}"
    db = AsyncMock()
    with (
        patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", "webhook-secret"),
        pytest.raises(HTTPException) as exc,
    ):
        await receive_github_webhook(_request(body, signature="sha256=bad"), db=db)
    assert exc.value.status_code == 401
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_rejects_oversized_payload_before_signature_or_database_work() -> None:
    body = b"x" * 256_001
    db = AsyncMock()
    with pytest.raises(HTTPException) as exc:
        await receive_github_webhook(_request(body), db=db)
    assert exc.value.status_code == 413
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_persists_one_triage_event_per_connected_project() -> None:
    body = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "octo/repo"},
            "workflow_run": {"id": 456, "run_attempt": 1, "conclusion": "failure", "name": "CI", "head_branch": "main"},
        }
    ).encode()

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.added: list[ProactiveEvent] = []

        async def execute(self, _query: object) -> SimpleNamespace:
            self.execute_count += 1
            if self.execute_count == 1:
                tenant_a, tenant_b = uuid.uuid4(), uuid.uuid4()
                self.tenant_ids = (tenant_a, tenant_b)
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.tenant_ids)))
            if self.execute_count in {2, 3}:
                tenant_id = self.tenant_ids[self.execute_count - 2]
                matches = [(uuid.uuid4(), uuid.uuid4(), tenant_id)]
                return SimpleNamespace(all=lambda: matches)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, event: ProactiveEvent) -> None:
            if event.id is None:
                event.id = uuid.uuid4()
            self.added.append(event)

        async def commit(self) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(self, _event: ProactiveEvent) -> None:
            return None

    stub_db = StubDB()
    db = cast(AsyncSession, stub_db)
    delivery_id = str(uuid.uuid4())
    with (
        patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", "webhook-secret"),
        patch("solvyn.api.v1.webhooks.bind_tenant_scope", new_callable=AsyncMock) as bind_scope,
    ):
        response = await receive_github_webhook(
            _request(body, delivery=delivery_id, signature=_signature("webhook-secret", body)),
            db=db,
        )

    assert response["created_count"] == 2
    assert response["duplicate_count"] == 0
    assert len(stub_db.added) == 2
    scoped_tenants = [call.args[1] for call in bind_scope.await_args_list]
    assert scoped_tenants[:2] == list(stub_db.tenant_ids)
    assert scoped_tenants[2:] == list(stub_db.tenant_ids)
    assert stub_db.added[0].delivery_id == delivery_id
    assert stub_db.added[0].source_event_id == "octo/repo:456:1"
    assert stub_db.added[0].status == "pending"


@pytest.mark.asyncio
async def test_signed_security_alert_is_persisted_without_secret_material() -> None:
    body = json.dumps(
        {
            "action": "created",
            "repository": {"full_name": "octo/repo"},
            "alert": {
                "number": 73,
                "state": "open",
                "secret_type_display_name": "Example token",
                "secret": "must-never-reach-the-database",
                "html_url": "https://github.com/octo/repo/security/secret-scanning/73",
            },
        }
    ).encode()

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.added: list[ProactiveEvent] = []

        async def execute(self, _query: object) -> SimpleNamespace:
            self.execute_count += 1
            if self.execute_count == 1:
                self.tenant_id = uuid.uuid4()
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.tenant_id]))
            if self.execute_count == 2:
                return SimpleNamespace(all=lambda: [(uuid.uuid4(), uuid.uuid4(), self.tenant_id)])
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, event: ProactiveEvent) -> None:
            if event.id is None:
                event.id = uuid.uuid4()
            self.added.append(event)

        async def commit(self) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(self, _event: ProactiveEvent) -> None:
            return None

    stub_db = StubDB()
    db = cast(AsyncSession, stub_db)
    with (
        patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", "webhook-secret"),
        patch("solvyn.api.v1.webhooks.bind_tenant_scope", new_callable=AsyncMock),
    ):
        result = await receive_github_webhook(
            _request(body, event="secret_scanning_alert", signature=_signature("webhook-secret", body)),
            db=db,
        )

    assert result["created_count"] == 1
    assert len(stub_db.added) == 1
    event = stub_db.added[0]
    assert event.event_type == "secret_scanning_alert"
    assert event.action == "created"
    assert event.workflow_name == "Example token"
    assert event.status == "pending"
    assert "must-never-reach-the-database" not in repr(event.__dict__)


@pytest.mark.asyncio
async def test_replayed_workflow_run_with_new_delivery_id_is_deduplicated() -> None:
    body = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "octo/repo"},
            "workflow_run": {"id": 456, "run_attempt": 1, "conclusion": "failure", "name": "CI", "head_branch": "main"},
        }
    ).encode()

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.added = False
            self.existing = SimpleNamespace(
                id=uuid.uuid4(),
                source_event_id="octo/repo:456:1",
                delivery_id=str(uuid.uuid4()),
                created_at=None,
            )

        async def execute(self, _query: object) -> SimpleNamespace:
            self.execute_count += 1
            if self.execute_count == 1:
                self.tenant_id = uuid.uuid4()
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.tenant_id]))
            if self.execute_count == 2:
                return SimpleNamespace(all=lambda: [(uuid.uuid4(), uuid.uuid4(), self.tenant_id)])
            return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

        def add(self, _event: ProactiveEvent) -> None:
            self.added = True

        async def commit(self) -> None:
            return None

        async def flush(self) -> None:
            return None

        async def refresh(self, _event: object) -> None:
            return None

    stub_db = StubDB()
    db = cast(AsyncSession, stub_db)
    with (
        patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", "webhook-secret"),
        patch("solvyn.api.v1.webhooks.bind_tenant_scope", new_callable=AsyncMock),
    ):
        result = await receive_github_webhook(
            _request(body, delivery=str(uuid.uuid4()), signature=_signature("webhook-secret", body)),
            db=db,
        )
    assert result["created_count"] == 0
    assert result["duplicate_count"] == 1
    assert not stub_db.added


@pytest.mark.asyncio
async def test_unique_race_retries_missing_project_event_after_rollback() -> None:
    body = json.dumps(
        {
            "action": "completed",
            "repository": {"full_name": "octo/repo"},
            "workflow_run": {"id": 789, "run_attempt": 1, "conclusion": "failure", "name": "CI"},
        }
    ).encode()
    project_a, project_b = uuid.uuid4(), uuid.uuid4()
    organization_a, organization_b = uuid.uuid4(), uuid.uuid4()
    existing = SimpleNamespace(id=uuid.uuid4(), created_at=None)

    class StubDB:
        def __init__(self) -> None:
            self.execute_count = 0
            self.commit_count = 0
            self.added: list[ProactiveEvent] = []
            self.rolled_back = False

        async def execute(self, _query: object) -> SimpleNamespace:
            self.execute_count += 1
            if self.execute_count == 1:
                self.tenant_id = uuid.uuid4()
                return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [self.tenant_id]))
            if self.execute_count == 2:
                return SimpleNamespace(all=lambda: [(project_a, organization_a, self.tenant_id), (project_b, organization_b, self.tenant_id)])
            if self.execute_count in {3, 4, 5}:
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            if self.execute_count == 6:
                return SimpleNamespace(scalar_one_or_none=lambda: existing)
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, event: ProactiveEvent) -> None:
            if event.id is None:
                event.id = uuid.uuid4()
            self.added.append(event)

        async def commit(self) -> None:
            self.commit_count += 1
            if self.commit_count == 1:
                raise IntegrityError("insert", {}, RuntimeError("unique collision"))

        async def flush(self) -> None:
            return None

        async def rollback(self) -> None:
            self.rolled_back = True

        async def refresh(self, _event: object) -> None:
            return None

    stub_db = StubDB()
    db = cast(AsyncSession, stub_db)
    with (
        patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", "webhook-secret"),
        patch("solvyn.api.v1.webhooks.bind_tenant_scope", new_callable=AsyncMock),
    ):
        result = await receive_github_webhook(
            _request(body, signature=_signature("webhook-secret", body)),
            db=db,
        )

    assert stub_db.rolled_back
    assert stub_db.commit_count == 2
    assert len(stub_db.added) == 3
    assert result["created_count"] == 1
    assert result["duplicate_count"] == 1


@pytest.mark.asyncio
async def test_unsupported_signed_event_is_ignored_without_database_access() -> None:
    body = b"{}"
    db = AsyncMock()
    with patch("solvyn.api.v1.webhooks.settings.github_webhook_secret", "webhook-secret"):
        result = await receive_github_webhook(
            _request(body, event="issues", signature=_signature("webhook-secret", body)),
            db=db,
        )
    assert result == {"accepted": True, "ignored": True, "reason": "unsupported_event"}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_github_webhook_keeps_ip_rate_limit_without_tenant_limit() -> None:
    request = _request(b"{}")
    middleware = RateLimitMiddleware()
    call_next = AsyncMock(return_value=Response(status_code=202))
    with (
        patch("solvyn.multi_tenant.auth.redis_rate_limiter.check_ip", new_callable=AsyncMock, return_value=(True, 0)),
        patch("solvyn.multi_tenant.auth.redis_rate_limiter.check_tenant", new_callable=AsyncMock) as tenant_limit,
    ):
        response = await middleware(request, call_next)
    assert response.status_code == 202
    tenant_limit.assert_not_called()


@pytest.mark.asyncio
async def test_github_webhook_rate_limit_rejects_excess_ip_requests() -> None:
    request = _request(b"{}")
    middleware = RateLimitMiddleware()
    call_next = AsyncMock(return_value=Response(status_code=202))
    with patch(
        "solvyn.multi_tenant.auth.redis_rate_limiter.check_ip",
        new_callable=AsyncMock,
        return_value=(False, 30),
    ) as ip_limit:
        response = await middleware(request, call_next)
    assert response.status_code == 429
    ip_limit.assert_awaited_once()
    call_next.assert_not_called()
