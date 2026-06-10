from __future__ import annotations

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
