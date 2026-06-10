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
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
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
