from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_otel_available = False
_tracer_provider: Any = None


def init_opentelemetry(
    service_name: str = "solvyn",
    otlp_endpoint: str | None = None,
    enabled: bool = False,
) -> None:
    global _otel_available, _tracer_provider
    if not enabled:
        logger.info("OpenTelemetry disabled (set OTEL_ENABLED=true to enable)")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)

        if otlp_endpoint:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

            # Collector endpoint uses OTLP/HTTP. Accept either its base URL or
            # an explicit /v1/traces URL for local and external deployments.
            endpoint = otlp_endpoint.rstrip("/")
            if not endpoint.endswith("/v1/traces"):
                endpoint = f"{endpoint}/v1/traces"
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info(f"OpenTelemetry OTLP exporter configured: {otlp_endpoint}")
        else:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
            logger.info("OpenTelemetry console exporter configured")

        trace.set_tracer_provider(provider)
        _tracer_provider = provider
        _otel_available = True
        logger.info(f"OpenTelemetry initialized for service: {service_name}")
    except ImportError:
        logger.warning("OpenTelemetry packages not installed. Install with: pip install opentelemetry-sdk opentelemetry-exporter-otlp")
    except Exception as e:
        logger.error(f"Failed to initialize OpenTelemetry: {e}")


def get_tracer(name: str = "solvyn") -> Any:
    if not _otel_available:
        return _NoOpTracer()
    from opentelemetry import trace

    return trace.get_tracer(name)


def shutdown_opentelemetry() -> None:
    """Flush and shut down the provider once without interrupting process shutdown."""
    global _otel_available, _tracer_provider
    provider = _tracer_provider
    if provider is None:
        return

    # Clear the reference first so repeated or reentrant calls are harmless.
    _tracer_provider = None
    _otel_available = False
    try:
        if not provider.force_flush(timeout_millis=5000):
            logger.warning("OpenTelemetry flush timed out during shutdown")
    except Exception:
        logger.warning("OpenTelemetry flush failed during shutdown", exc_info=True)
    try:
        provider.shutdown()
    except Exception:
        logger.warning("OpenTelemetry provider shutdown failed", exc_info=True)


class PrivacySafeTracingMiddleware:
    """Record request method and status only; never inspect headers or bodies."""

    def __init__(self, app: Any, *, enabled: bool) -> None:
        self.app = app
        self.tracer = get_tracer("solvyn.http") if enabled else None

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self.tracer is None:
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "UNKNOWN")
        status_code = 500

        async def capture_status(message: dict[str, Any]) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        with self.tracer.start_as_current_span(
            "HTTP request",
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("http.request.method", method)
            try:
                await self.app(scope, receive, capture_status)
            finally:
                span.set_attribute("http.response.status_code", status_code)


class _NoOpTracer:
    def start_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


class _NoOpSpan:
    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any) -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass

    def end(self) -> None:
        pass
