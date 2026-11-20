from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from solvyn.workflows import scheduler


@pytest.fixture
def span_exporter(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(scheduler, "get_tracer", provider.get_tracer)
    yield exporter
    provider.shutdown()


@pytest.mark.asyncio
async def test_scheduled_workflow_emits_privacy_safe_completed_span(
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
) -> None:
    execution_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    resume = AsyncMock()
    monkeypatch.setattr(scheduler.workflow_executor, "resume", resume)

    await scheduler._resume_scheduled_workflow(execution_id, tenant_id)

    resume.assert_awaited_once_with(execution_id, str(tenant_id))
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "solvyn.scheduler.workflow.resume"
    assert span.status.status_code == StatusCode.UNSET
    assert span.events == ()
    assert span.attributes == {
        "solvyn.execution.id": str(execution_id),
        "solvyn.execution.kind": "workflow",
        "solvyn.execution.outcome": "completed",
    }
    assert str(tenant_id) not in str(span.attributes)


@pytest.mark.asyncio
async def test_scheduled_workflow_race_records_not_claimed_without_exception_event(
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
) -> None:
    execution_id = uuid.uuid4()
    resume = AsyncMock(side_effect=RuntimeError("private task details"))
    monkeypatch.setattr(scheduler.workflow_executor, "resume", resume)

    await scheduler._resume_scheduled_workflow(execution_id, uuid.uuid4())

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["solvyn.execution.outcome"] == "not_claimed"
    assert span.status.status_code == StatusCode.UNSET
    assert span.events == ()
    assert "private task details" not in str(span.attributes)


@pytest.mark.asyncio
async def test_scheduled_workflow_failure_records_only_error_status(
    monkeypatch: pytest.MonkeyPatch,
    span_exporter: InMemorySpanExporter,
) -> None:
    execution_id = uuid.uuid4()
    resume = AsyncMock(side_effect=ValueError("prompt and credentials must not leak"))
    monkeypatch.setattr(scheduler.workflow_executor, "resume", resume)

    await scheduler._resume_scheduled_workflow(execution_id, uuid.uuid4())

    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.attributes["solvyn.execution.outcome"] == "failed"
    assert span.status.status_code == StatusCode.ERROR
    assert span.events == ()
    assert "prompt and credentials" not in str(span.attributes)
