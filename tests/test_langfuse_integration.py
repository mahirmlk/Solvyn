"""Unit tests for the LangFuse observability integration.

These tests mock the LangFuse client and verify that:

* the module degrades gracefully when LangFuse is not configured
* ``trace_llm_call`` emits a generation with the expected metadata
* ``flush`` / ``shutdown`` are safe to call in any state
* ``TracingManager`` mirrors traces to LangFuse when toggled on
* the LLM router wraps every call in a ``trace_llm_call`` context
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FakeGeneration:
    """Stand-in for ``langfuse._client.span.LangfuseGeneration``.

    Captures the start kwargs, ``update`` kwargs, and ``end`` calls so tests
    can assert on them.
    """

    instances: list[FakeGeneration] = []

    def __init__(self, start_kwargs: dict[str, Any] | None = None) -> None:
        self.update_kwargs: list[dict[str, Any]] = []
        self.ended: bool = False
        self.start_kwargs: dict[str, Any] | None = start_kwargs
        FakeGeneration.instances.append(self)

    def update(self, **kwargs: Any) -> None:
        self.update_kwargs.append(kwargs)

    def end(self) -> None:
        self.ended = True


@contextmanager
def _patched_langfuse_client(enabled: bool = True) -> Any:
    """Patch ``_client`` on the langfuse module and yield the fake factory."""
    from solvyn.observability import langfuse as langfuse_mod

    FakeGeneration.instances.clear()
    langfuse_mod.reset_for_tests()

    factory_calls: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> Any:
        factory_calls.append(kwargs)
        return FakeGeneration(start_kwargs=kwargs)

    client = MagicMock()
    client.start_observation = MagicMock(side_effect=factory)
    client.flush = MagicMock()

    langfuse_mod.set_client_for_tests(client, enabled=enabled)
    try:
        yield client, factory, factory_calls
    finally:
        langfuse_mod.reset_for_tests()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_init_no_op_without_credentials() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    langfuse_mod.reset_for_tests()
    from solvyn.config import settings as solvyn_settings

    with patch.object(solvyn_settings, "langfuse_public_key", ""), \
         patch.object(solvyn_settings, "langfuse_secret_key", ""):
        assert langfuse_mod.init_langfuse() is False
    assert langfuse_mod.is_enabled() is False
    assert langfuse_mod.get_client() is None


def test_init_succeeds_with_explicit_keys() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    langfuse_mod.reset_for_tests()
    fake_factory = MagicMock(return_value=MagicMock())
    with patch.object(langfuse_mod, "_LangfuseClient", fake_factory, create=True):
        result = langfuse_mod.init_langfuse(
            public_key="pk-test",
            secret_key="sk-test",
            host="https://example.com",
        )
    assert result is True
    assert langfuse_mod.is_enabled() is True
    fake_factory.assert_called_once_with(
        public_key="pk-test",
        secret_key="sk-test",
        host="https://example.com",
    )
    langfuse_mod.reset_for_tests()


def test_init_handles_client_exception() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    langfuse_mod.reset_for_tests()
    broken_factory = MagicMock(side_effect=RuntimeError("boom"))
    with patch.object(langfuse_mod, "_LangfuseClient", broken_factory, create=True):
        assert langfuse_mod.init_langfuse(public_key="pk", secret_key="sk") is False
    assert langfuse_mod.is_enabled() is False
    langfuse_mod.reset_for_tests()


# ---------------------------------------------------------------------------
# trace_llm_call
# ---------------------------------------------------------------------------
def test_trace_llm_call_noop_when_disabled() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    langfuse_mod.reset_for_tests()
    with langfuse_mod.trace_llm_call(
        name="n",
        model="m",
        provider="p",
        input_data={"prompt": "hi"},
        tenant_id="t1",
        session_id="s1",
    ) as record:
        record["output"] = "hello"
        record["total_tokens"] = 5
    assert record["output"] == "hello"
    assert record["duration_ms"] >= 0
    assert FakeGeneration.instances == []


def test_trace_llm_call_records_observation() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    with _patched_langfuse_client() as (client, factory, _calls):
        with langfuse_mod.trace_llm_call(
            name="agent.run",
            model="gpt-4o",
            provider="openrouter",
            input_data={"prompt": "hi", "system_prompt": "be brief"},
            tenant_id="tenant-a",
            session_id="sess-a",
        ) as record:
            record["output"] = "hello there"
            record["prompt_tokens"] = 3
            record["completion_tokens"] = 5
            record["total_tokens"] = 8
            record["cost"] = 0.0001

        assert len(FakeGeneration.instances) == 1
        gen = FakeGeneration.instances[0]
        assert gen.ended is True
        assert gen.start_kwargs is not None
        assert gen.start_kwargs["as_type"] == "generation"
        assert gen.start_kwargs["name"] == "agent.run"
        assert gen.start_kwargs["model"] == "gpt-4o"
        assert gen.start_kwargs["metadata"]["provider"] == "openrouter"
        assert gen.start_kwargs["metadata"]["tenant_id"] == "tenant-a"
        assert gen.start_kwargs["metadata"]["session_id"] == "sess-a"

        assert gen.update_kwargs, "update() was never called"
        update = gen.update_kwargs[-1]
        assert update["output"] == "hello there"
        assert update["usage_details"] == {"input": 3, "output": 5, "total": 8}
        assert update["cost_details"] == {"total": 0.0001}
        assert "level" not in update
        client.start_observation.assert_called_once()


def test_trace_llm_call_captures_exception() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    with _patched_langfuse_client() as (_client, _factory, _calls):
        with pytest.raises(RuntimeError, match="provider exploded"):
            with langfuse_mod.trace_llm_call(
                name="agent.run",
                model="m",
                provider="p",
            ) as record:
                raise RuntimeError("provider exploded")
        gen = FakeGeneration.instances[0]
        assert gen.ended is True
        update = gen.update_kwargs[-1]
        assert update["level"] == "ERROR"
        assert update["status_message"] == "provider exploded"
        assert record["error"] == "provider exploded"
        assert record["duration_ms"] >= 0


def test_trace_llm_call_tolerates_start_observation_failure() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    langfuse_mod.reset_for_tests()
    FakeGeneration.instances.clear()
    client = MagicMock()
    client.start_observation = MagicMock(side_effect=RuntimeError("network down"))
    client.flush = MagicMock()
    langfuse_mod.set_client_for_tests(client, enabled=True)
    try:
        with langfuse_mod.trace_llm_call(name="x", model="m") as record:
            record["output"] = "ok"
        # No fake generations recorded because start_observation failed
        assert FakeGeneration.instances == []
        assert record["output"] == "ok"
    finally:
        langfuse_mod.reset_for_tests()


# ---------------------------------------------------------------------------
# flush / shutdown
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_flush_safe_when_disabled() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    langfuse_mod.reset_for_tests()
    # Should not raise.
    await langfuse_mod.flush()
    await langfuse_mod.shutdown()


@pytest.mark.asyncio
async def test_flush_calls_client() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    with _patched_langfuse_client() as (client, _factory, _calls):
        await langfuse_mod.flush()
        client.flush.assert_called_once()


@pytest.mark.asyncio
async def test_shutdown_flushes_and_resets() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    with _patched_langfuse_client() as (client, _factory, _calls):
        await langfuse_mod.shutdown()
        client.flush.assert_called_once()
    # Module state is reset.
    assert langfuse_mod.get_client() is None
    assert langfuse_mod.is_enabled() is False


# ---------------------------------------------------------------------------
# TracingManager mirror
# ---------------------------------------------------------------------------
def test_tracing_manager_mirror_off_by_default() -> None:
    from solvyn.observability.tracing import tracing_manager

    tracing_manager.set_langfuse_mirror(False)
    assert tracing_manager.is_langfuse_mirroring() is False


def test_tracing_manager_mirror_calls_record_event() -> None:
    from solvyn.observability import langfuse as langfuse_mod
    from solvyn.observability.tracing import tracing_manager

    record_calls: list[dict[str, Any]] = []

    def fake_record_event(*args: Any, **kwargs: Any) -> None:
        record_calls.append(kwargs)

    with patch.object(langfuse_mod, "record_event", side_effect=fake_record_event):
        tracing_manager.set_langfuse_mirror(True)
        try:
            trace_id = tracing_manager.create_trace(
                name="agent.run",
                agent_type="test-agent",
                session_id="s1",
                tenant_id="t1",
                metadata={"foo": "bar"},
            )
            tracing_manager.add_span(
                trace_id,
                name="llm_call",
                span_type="llm",
                input_data={"prompt": "hi"},
                output_data={"output": "hello"},
                duration_ms=12.5,
                token_count=42,
                model="gpt-4o",
                provider="openrouter",
            )
        finally:
            tracing_manager.set_langfuse_mirror(False)

    # One call for create_trace, one for add_span
    assert len(record_calls) == 2
    span_kwargs = record_calls[1]
    assert span_kwargs["as_type"] == "generation"
    assert span_kwargs["name"] == "llm_call"
    assert span_kwargs["input_data"] == {"prompt": "hi"}
    assert span_kwargs["output_data"] == {"output": "hello"}
    assert span_kwargs["model"] == "gpt-4o"
    assert span_kwargs["usage_details"] == {"total": 42}
    assert span_kwargs["tenant_id"] == "t1"
    assert span_kwargs["session_id"] == "s1"


def test_tracing_manager_mirror_handles_record_event_failure() -> None:
    from solvyn.observability import langfuse as langfuse_mod
    from solvyn.observability.tracing import tracing_manager

    def exploding(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("boom")

    with patch.object(langfuse_mod, "record_event", side_effect=exploding):
        tracing_manager.set_langfuse_mirror(True)
        try:
            # Must not raise — in-memory tracing must keep working.
            trace_id = tracing_manager.create_trace(
                name="agent.run",
                agent_type="test-agent",
                session_id="s1",
                tenant_id="t1",
            )
            tracing_manager.add_span(
                trace_id,
                name="span",
                span_type="llm",
            )
        finally:
            tracing_manager.set_langfuse_mirror(False)

    trace = tracing_manager.get_trace(trace_id)
    assert trace is not None
    assert len(trace["spans"]) == 1


# ---------------------------------------------------------------------------
# LLM router integration
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_llm_router_generates_langfuse_observation() -> None:
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router

    with _patched_langfuse_client():
        mock_provider = AsyncMock()
        mock_provider.generate.return_value = LLMResponse(
            content="ok",
            provider="openrouter",
            model="openai/gpt-4o",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cost=0.0001,
        )
        # Inject our mock in place of the real provider.
        original = llm_router.providers.get("openrouter")
        llm_router.providers["openrouter"] = mock_provider
        try:
            response = await llm_router.generate(
                prompt="hi",
                provider="openrouter",
                model="openai/gpt-4o",
                use_cache=False,
                fallback=False,
                tenant_id="tenant-a",
                session_id="sess-a",
                agent_type="backend_engineer",
            )
        finally:
            if original is not None:
                llm_router.providers["openrouter"] = original
            else:
                llm_router.providers.pop("openrouter", None)

    assert response.content == "ok"
    assert len(FakeGeneration.instances) == 1
    gen = FakeGeneration.instances[0]
    assert gen.start_kwargs is not None
    assert gen.start_kwargs["name"] == "backend_engineer"
    assert gen.start_kwargs["model"] == "openai/gpt-4o"
    assert gen.start_kwargs["metadata"]["tenant_id"] == "tenant-a"
    assert gen.start_kwargs["metadata"]["session_id"] == "sess-a"
    update = gen.update_kwargs[-1]
    assert update["output"] == "ok"
    assert update["usage_details"] == {"input": 10, "output": 20, "total": 30}
    assert update["cost_details"] == {"total": 0.0001}


@pytest.mark.asyncio
async def test_llm_router_works_when_langfuse_disabled() -> None:
    """The router must function normally when LangFuse is not configured."""
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router
    from solvyn.observability import langfuse as langfuse_mod

    FakeGeneration.instances.clear()
    langfuse_mod.reset_for_tests()
    assert langfuse_mod.is_enabled() is False

    mock_provider = AsyncMock()
    mock_provider.generate.return_value = LLMResponse(
        content="still works",
        provider="openrouter",
        model="openai/gpt-4o",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
        cost=0.0,
    )
    original = llm_router.providers.get("openrouter")
    llm_router.providers["openrouter"] = mock_provider
    try:
        response = await llm_router.generate(
            prompt="hi",
            provider="openrouter",
            use_cache=False,
            fallback=False,
        )
    finally:
        if original is not None:
            llm_router.providers["openrouter"] = original
        else:
            llm_router.providers.pop("openrouter", None)

    assert response.content == "still works"
    assert FakeGeneration.instances == []


@pytest.mark.asyncio
async def test_llm_router_records_error_to_langfuse() -> None:
    import httpx

    from solvyn.llm.router import llm_router

    with _patched_langfuse_client():
        mock_provider = AsyncMock()
        mock_request = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "boom"
        mock_provider.generate.side_effect = httpx.HTTPStatusError(
            "boom", request=mock_request, response=mock_response
        )
        # Use fallback=True with all providers patched to fail, so the
        # router exhausts fallbacks and raises ``RuntimeError``.
        original_openrouter = llm_router.providers.get("openrouter")
        original_groq = llm_router.providers.get("groq")
        original_deepseek = llm_router.providers.get("deepseek")
        llm_router.providers["openrouter"] = mock_provider
        llm_router.providers["groq"] = mock_provider
        llm_router.providers["deepseek"] = mock_provider
        try:
            with pytest.raises(RuntimeError, match="All LLM providers failed"):
                await llm_router.generate(
                    prompt="hi",
                    provider="openrouter",
                    use_cache=False,
                    fallback=True,
                )
        finally:
            if original_openrouter is not None:
                llm_router.providers["openrouter"] = original_openrouter
            else:
                llm_router.providers.pop("openrouter", None)
            if original_groq is not None:
                llm_router.providers["groq"] = original_groq
            else:
                llm_router.providers.pop("groq", None)
            if original_deepseek is not None:
                llm_router.providers["deepseek"] = original_deepseek
            else:
                llm_router.providers.pop("deepseek", None)

    # The initial call should have been recorded as a failed observation.
    # The router wraps fallback failures in a RuntimeError, so the outer
    # trace's status_message reflects that. The error level is what we
    # really care about.
    initial = FakeGeneration.instances[0]
    update = initial.update_kwargs[-1]
    assert update["level"] == "ERROR"
    assert update["status_message"]


# ---------------------------------------------------------------------------
# LangFuse is_available
# ---------------------------------------------------------------------------
def test_is_available_reflects_package_presence() -> None:
    from solvyn.observability import langfuse as langfuse_mod

    # langfuse is installed in the test env (pyproject requires it).
    assert langfuse_mod.is_available() is True
