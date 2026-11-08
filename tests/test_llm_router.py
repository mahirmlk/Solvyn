from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest


class TestLLMRouterCache:
    def test_router_has_providers(self) -> None:
        from solvyn.llm.router import llm_router
        assert "openrouter" in llm_router.providers
        assert "groq" in llm_router.providers
        assert "deepseek" in llm_router.providers

    def test_router_has_registry(self) -> None:
        from solvyn.llm.router import llm_router
        models = llm_router.registry.list_models()
        assert len(models) >= 8

    def test_select_model_default(self) -> None:
        from solvyn.llm.router import llm_router
        provider, model = llm_router.select_model("default")
        assert provider == "openrouter"
        assert model == "openai/gpt-4o"

    def test_select_model_cheapest(self) -> None:
        from solvyn.llm.router import llm_router
        provider, model = llm_router.select_model("cheapest")
        assert provider != ""
        assert model != ""

    def test_select_model_fastest(self) -> None:
        from solvyn.llm.router import llm_router
        provider, model = llm_router.select_model("fastest")
        assert provider != ""

    def test_get_model_registry(self) -> None:
        from solvyn.llm.router import llm_router
        registry = llm_router.get_model_registry()
        assert len(registry) >= 8
        assert "provider" in registry[0]
        assert "cost_per_1m_prompt" in registry[0]

    def test_cache_stats(self) -> None:
        from solvyn.llm.router import llm_router
        stats = llm_router.cache.get_stats()
        assert "hits" in stats
        assert "misses" in stats
        assert "hit_rate" in stats


class _UsageProvider:
    def __init__(self, response):
        self.response = response

    async def generate(self, **_kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _UsageCache:
    def __init__(self, cached=None):
        self.cached = cached

    async def get(self, *_args):
        return self.cached

    async def set(self, *_args):
        return None


@pytest.mark.asyncio
async def test_router_records_task_usage_after_successful_provider_response(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.agents.cost_tracker import cost_tracker
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import LLMRouter

    response = LLMResponse(
        content="result", provider="groq", model="model-a", prompt_tokens=12,
        completion_tokens=8, total_tokens=20, cost=0.0001,
    )
    record = AsyncMock()
    monkeypatch.setattr(cost_tracker, "record", record)
    router = object.__new__(LLMRouter)
    router.providers = {"groq": _UsageProvider(response)}
    router.cache = _UsageCache()
    tenant_id, session_id, task_id = (str(uuid.uuid4()) for _ in range(3))

    await router.generate(
        prompt="private prompt", provider="groq", model="model-a", use_cache=False,
        tenant_id=tenant_id, session_id=session_id, agent_type="qa-engineer", task_id=task_id,
    )

    record.assert_awaited_once_with(
        tenant_id=tenant_id, session_id=session_id, task_id=task_id,
        agent_type="qa-engineer", provider="groq", model="model-a",
        prompt_tokens=12, completion_tokens=8, total_tokens=20, cost=0.0001,
    )
    assert "private prompt" not in str(record.await_args)


@pytest.mark.asyncio
async def test_router_does_not_record_cached_task_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.agents.cost_tracker import cost_tracker
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import LLMRouter

    record = AsyncMock()
    monkeypatch.setattr(cost_tracker, "record", record)
    router = object.__new__(LLMRouter)
    router.providers = {}
    router.cache = _UsageCache(
        LLMResponse(
            content="cached", provider="groq", model="model-a", prompt_tokens=12,
            completion_tokens=8, total_tokens=20, cost=0.0001,
        )
    )

    await router.generate(
        prompt="private prompt", provider="groq", model="model-a", tenant_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
    )

    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_attributes_usage_to_successful_fallback_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from solvyn.agents.cost_tracker import cost_tracker
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import LLMRouter

    response = LLMResponse(
        content="fallback", provider="deepseek", model="actual-fallback-model", prompt_tokens=9,
        completion_tokens=4, total_tokens=13, cost=0.00002,
    )
    record = AsyncMock()
    monkeypatch.setattr(cost_tracker, "record", record)
    router = object.__new__(LLMRouter)
    router.providers = {
        "openrouter": _UsageProvider(ConnectionError("primary unavailable")),
        "deepseek": _UsageProvider(response),
    }
    router.cache = _UsageCache()
    task_id = str(uuid.uuid4())

    await router.generate(
        prompt="work", provider="openrouter", model="requested-model", use_cache=False,
        tenant_id=str(uuid.uuid4()), agent_type="engineer", task_id=task_id,
    )

    assert record.await_count == 1
    assert record.await_args.kwargs["provider"] == "deepseek"
    assert record.await_args.kwargs["model"] == "actual-fallback-model"
    assert record.await_args.kwargs["task_id"] == task_id
