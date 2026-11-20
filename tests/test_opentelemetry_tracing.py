from __future__ import annotations

from contextlib import contextmanager
from typing import Any

import pytest

from solvyn.observability import opentelemetry


class _RecordingSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _RecordingTracer:
    def __init__(self) -> None:
        self.span = _RecordingSpan()
        self.name: str | None = None
        self.options: dict[str, Any] = {}

    @contextmanager
    def start_as_current_span(self, name: str, **options: Any):
        self.name = name
        self.options = options
        yield self.span


@pytest.mark.asyncio
async def test_disabled_tracing_passes_request_without_creating_span(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer = _RecordingTracer()
    monkeypatch.setattr(opentelemetry, "get_tracer", lambda _name: tracer)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})

    middleware = opentelemetry.PrivacySafeTracingMiddleware(app, enabled=False)
    await middleware({"type": "http", "method": "GET", "headers": [(b"authorization", b"secret")]}, None, send)

    assert tracer.name is None
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_request_span_records_only_method_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer = _RecordingTracer()
    monkeypatch.setattr(opentelemetry, "get_tracer", lambda _name: tracer)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 202, "headers": []})

    middleware = opentelemetry.PrivacySafeTracingMiddleware(app, enabled=True)
    await middleware(
        {
            "type": "http",
            "method": "POST",
            "path": "/private/repository",
            "query_string": b"token=secret",
            "headers": [(b"authorization", b"secret")],
        },
        None,
        send,
    )

    assert tracer.name == "HTTP request"
    assert tracer.options == {"record_exception": False, "set_status_on_exception": False}
    assert tracer.span.attributes == {
        "http.request.method": "POST",
        "http.response.status_code": 202,
    }
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_unhandled_exception_is_not_recorded_on_request_span(monkeypatch: pytest.MonkeyPatch) -> None:
    tracer = _RecordingTracer()
    monkeypatch.setattr(opentelemetry, "get_tracer", lambda _name: tracer)

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        raise RuntimeError("sensitive request detail")

    middleware = opentelemetry.PrivacySafeTracingMiddleware(app, enabled=True)
    with pytest.raises(RuntimeError, match="sensitive request detail"):
        await middleware({"type": "http", "method": "POST"}, None, lambda _message: None)

    assert tracer.options == {"record_exception": False, "set_status_on_exception": False}
    assert tracer.span.attributes == {
        "http.request.method": "POST",
        "http.response.status_code": 500,
    }


def test_shutdown_flushes_and_shuts_down_provider_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class Provider:
        flush_calls = 0
        shutdown_calls = 0

        def force_flush(self, *, timeout_millis: int) -> bool:
            assert timeout_millis == 5000
            self.flush_calls += 1
            return True

        def shutdown(self) -> None:
            self.shutdown_calls += 1

    provider = Provider()
    monkeypatch.setattr(opentelemetry, "_tracer_provider", provider)
    monkeypatch.setattr(opentelemetry, "_otel_available", True)

    opentelemetry.shutdown_opentelemetry()
    opentelemetry.shutdown_opentelemetry()

    assert provider.flush_calls == 1
    assert provider.shutdown_calls == 1
    assert opentelemetry._tracer_provider is None
    assert opentelemetry._otel_available is False


def test_shutdown_provider_errors_are_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingProvider:
        def force_flush(self, *, timeout_millis: int) -> bool:
            raise RuntimeError("flush failed")

        def shutdown(self) -> None:
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(opentelemetry, "_tracer_provider", FailingProvider())
    opentelemetry.shutdown_opentelemetry()

    assert opentelemetry._tracer_provider is None
