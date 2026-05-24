import json
import logging
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from solvyn.logging_config import JsonFormatter, setup_logging


# ─── Auth ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_auth_middleware_missing_key() -> None:
    from solvyn.multi_tenant.auth import auth_middleware
    request = AsyncMock(spec=Request)
    request.url.path = "/api/v1/sessions"
    request.headers = {}

    async def ok(req):
        return JSONResponse({"ok": True})

    response = await auth_middleware(request, ok)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_auth_middleware_valid_key() -> None:
    from solvyn.multi_tenant.auth import auth_middleware
    from solvyn.config import settings

    original_secret = settings.api_key_secret
    settings.api_key_secret = "test-secret-key"
    try:
        request = AsyncMock(spec=Request)
        request.url.path = "/api/v1/sessions"
        request.headers = {"X-API-Key": "test-secret-key"}

        async def ok(req):
            return JSONResponse({"ok": True})

        response = await auth_middleware(request, ok)
        assert response.status_code == 200
    finally:
        settings.api_key_secret = original_secret


@pytest.mark.asyncio
async def test_auth_middleware_invalid_key() -> None:
    from solvyn.multi_tenant.auth import auth_middleware
    from solvyn.config import settings

    original_secret = settings.api_key_secret
    settings.api_key_secret = "test-secret-key"
    try:
        request = AsyncMock(spec=Request)
        request.url.path = "/api/v1/sessions"
        request.headers = {"X-API-Key": "wrong-secret-key"}

        async def ok(req):
            return JSONResponse({"ok": True})

        response = await auth_middleware(request, ok)
        assert response.status_code == 401
    finally:
        settings.api_key_secret = original_secret


@pytest.mark.asyncio
async def test_auth_middleware_public_path_health() -> None:
    from solvyn.multi_tenant.auth import auth_middleware
    request = AsyncMock(spec=Request)
    request.url.path = "/api/v1/health"
    request.headers = {}

    async def ok(req):
        return JSONResponse({"status": "ok"})

    response = await auth_middleware(request, ok)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_auth_middleware_public_path_metrics() -> None:
    from solvyn.multi_tenant.auth import auth_middleware
    request = AsyncMock(spec=Request)
    request.url.path = "/metrics"
    request.headers = {}

    async def ok(req):
        return JSONResponse({"status": "ok"})

    response = await auth_middleware(request, ok)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_rate_limit_allows_normal() -> None:
    from solvyn.multi_tenant.auth import RateLimitMiddleware
    mw = RateLimitMiddleware()
    request = AsyncMock(spec=Request)
    request.url.path = "/api/v1/workflows"
    request.client.host = "1.2.3.4"

    async def ok(req):
        return JSONResponse({"ok": True})

    response = await mw(request, ok)
    assert response.status_code == 200


# ─── Logging ─────────────────────────────────────────────────
def test_json_formatter() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO,
        pathname=__file__, lineno=1, msg="hello world",
        args=(), exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["level"] == "INFO"
    assert parsed["name"] == "test"
    assert parsed["message"] == "hello world"
    assert "timestamp" in parsed


def test_setup_logging_json() -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    try:
        setup_logging("DEBUG", json_format=True)
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)


def test_setup_logging_text() -> None:
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    root.handlers.clear()
    try:
        setup_logging("INFO", json_format=False)
        assert len(root.handlers) == 1
    finally:
        root.handlers.clear()
        root.handlers.extend(old_handlers)
