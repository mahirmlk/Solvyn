from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from solvyn.config import settings
from solvyn.llm.providers.base import LLMProvider, LLMResponse
from solvyn.llm.providers.deepseek import DeepSeekProvider
from solvyn.llm.providers.groq import GroqProvider
from solvyn.llm.providers.openrouter import OpenRouterProvider
from solvyn.observability.langfuse import trace_llm_call

logger = logging.getLogger(__name__)


@dataclass
class ModelInfo:
    """Metadata for a registered LLM model."""

    provider: str
    model: str
    cost_per_1m_prompt: float
    cost_per_1m_completion: float
    context_window: int = 128000
    supports_streaming: bool = True
    supports_function_calling: bool = False
    latency_class: str = "medium"
    quality_class: str = "high"


class ModelRegistry:
    """Registry of all known models with metadata for dynamic selection."""

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}

    def register(self, info: ModelInfo) -> None:
        key = f"{info.provider}/{info.model}"
        self._models[key] = info

    def get(self, provider: str, model: str) -> ModelInfo | None:
        return self._models.get(f"{provider}/{model}")

    def list_models(self) -> list[ModelInfo]:
        return list(self._models.values())

    def find_cheapest(self, provider: str | None = None) -> ModelInfo | None:
        candidates = [m for m in self._models.values() if provider is None or m.provider == provider]
        if not candidates:
            return None
        return min(candidates, key=lambda m: m.cost_per_1m_prompt + m.cost_per_1m_completion)

    def find_fastest(self) -> ModelInfo | None:
        speed_order = {"fast": 0, "medium": 1, "slow": 2}
        candidates = [m for m in self._models.values()]
        if not candidates:
            return None
        return min(candidates, key=lambda m: speed_order.get(m.latency_class, 1))

    def find_by_quality(self, min_quality: str = "high") -> list[ModelInfo]:
        quality_order = {"low": 0, "medium": 1, "high": 2, "ultra": 3}
        min_val = quality_order.get(min_quality, 1)
        return [m for m in self._models.values() if quality_order.get(m.quality_class, 0) >= min_val]


class LLMResponseCache:
    """Redis-backed cache for LLM responses keyed by (prompt_hash, model)."""

    def __init__(self, default_ttl: int = 3600) -> None:
        self.default_ttl = default_ttl
        self._local_cache: dict[str, tuple[LLMResponse, float]] = {}
        self._hits = 0
        self._misses = 0
        self._max_local_size = 10000

    def _make_key(self, prompt: str, model: str, system_prompt: str | None = None) -> str:
        content = f"{prompt}:{model}:{system_prompt or ''}"
        return hashlib.sha256(content.encode()).hexdigest()

    async def get(self, prompt: str, model: str, system_prompt: str | None = None) -> LLMResponse | None:
        import time as _time

        key = self._make_key(prompt, model, system_prompt)
        entry = self._local_cache.get(key)
        if entry is not None:
            response, cached_at = entry
            if _time.time() - cached_at < self.default_ttl:
                self._hits += 1
                return response
            del self._local_cache[key]
        self._misses += 1
        try:
            from solvyn.message_bus.redis import message_bus

            if message_bus.redis is not None:
                raw = await message_bus.redis.get(f"llm_cache:{key}")
                if raw:
                    import json

                    data = json.loads(raw)
                    self._hits += 1
                    return LLMResponse(
                        content=data["content"],
                        provider=data["provider"],
                        model=data["model"],
                        prompt_tokens=data.get("prompt_tokens", 0),
                        completion_tokens=data.get("completion_tokens", 0),
                        total_tokens=data.get("total_tokens", 0),
                        cost=data.get("cost", 0.0),
                    )
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning("LLM cache read failed (corrupted entry): %s", e)
        return None

    async def set(self, prompt: str, model: str, response: LLMResponse, system_prompt: str | None = None) -> None:
        key = self._make_key(prompt, model, system_prompt)
        import time as _time

        self._local_cache[key] = (response, _time.time())
        if len(self._local_cache) > self._max_local_size:
            sorted_keys = sorted(self._local_cache, key=lambda k: self._local_cache[k][1])
            for k in sorted_keys[: len(self._local_cache) // 2]:
                del self._local_cache[k]
        try:
            import json

            from solvyn.message_bus.redis import message_bus

            if message_bus.redis is not None:
                data = {
                    "content": response.content,
                    "provider": response.provider,
                    "model": response.model,
                    "prompt_tokens": response.prompt_tokens,
                    "completion_tokens": response.completion_tokens,
                    "total_tokens": response.total_tokens,
                    "cost": response.cost,
                }
                await message_bus.redis.setex(f"llm_cache:{key}", self.default_ttl, json.dumps(data))
        except (json.JSONDecodeError, TypeError, OSError) as e:
            logger.warning("LLM cache write failed: %s", e)

    def get_stats(self) -> dict[str, Any]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": self._hits / total if total > 0 else 0.0,
            "local_cache_size": len(self._local_cache),
        }


class LLMRouter:
    """Multi-provider LLM router with caching, dynamic model selection, and fallback."""

    def __init__(self) -> None:
        self.providers: dict[str, LLMProvider] = {
            "openrouter": OpenRouterProvider(),
            "groq": GroqProvider(),
            "deepseek": DeepSeekProvider(),
        }
        self.registry = ModelRegistry()
        self.cache = LLMResponseCache()
        self._init_model_registry()

    def _init_model_registry(self) -> None:
        models = [
            ("openrouter", "openai/gpt-4o", 2.50, 10.00, 128000, "medium", "ultra"),
            ("openrouter", "openai/gpt-4o-mini", 0.15, 0.60, 128000, "fast", "high"),
            ("openrouter", "anthropic/claude-3.5-sonnet", 3.00, 15.00, 200000, "medium", "ultra"),
            ("openrouter", "google/gemini-2.0-flash-001", 0.10, 0.40, 1000000, "fast", "high"),
            ("groq", "llama-3.3-70b-versatile", 0.59, 0.79, 128000, "fast", "high"),
            ("groq", "llama-3.1-8b-instant", 0.05, 0.08, 128000, "fast", "medium"),
            ("groq", "mixtral-8x7b-32768", 0.24, 0.24, 32768, "fast", "medium"),
            ("deepseek", "deepseek-chat", 0.27, 1.10, 64000, "medium", "high"),
            ("deepseek", "deepseek-reasoner", 0.55, 2.19, 64000, "slow", "ultra"),
        ]
        for provider, model, prompt_cost, compl_cost, ctx, latency, quality in models:
            self.registry.register(
                ModelInfo(
                    provider=provider,
                    model=model,
                    cost_per_1m_prompt=prompt_cost,
                    cost_per_1m_completion=compl_cost,
                    context_window=ctx,
                    latency_class=latency,
                    quality_class=quality,
                )
            )

    async def close(self) -> None:
        for provider in self.providers.values():
            await provider.close()

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        fallback: bool = True,
        use_cache: bool = True,
        tenant_id: str | None = None,
        session_id: str | None = None,
        agent_type: str | None = None,
        task_id: str | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        effective_model = model or settings.llm_default_model
        effective_provider = provider or settings.llm_default_provider
        resolved_tenant = self._resolve_tenant_id(tenant_id)
        resolved_session = self._resolve_session_id(session_id)
        trace_name = agent_type or "llm_call"

        if use_cache:
            cached = await self.cache.get(prompt, effective_model, system_prompt)
            if cached is not None:
                logger.debug(f"Cache hit for model={effective_model}")
                return cached

        llm = self.providers.get(effective_provider)
        if llm is None:
            raise ValueError(f"Unknown provider: {effective_provider}. Available: {list(self.providers.keys())}")

        with trace_llm_call(
            name=trace_name,
            model=effective_model,
            provider=effective_provider,
            input_data={"prompt": prompt, "system_prompt": system_prompt},
            tenant_id=resolved_tenant,
            session_id=resolved_session,
        ) as trace:
            try:
                response = await llm.generate(
                    prompt=prompt, system_prompt=system_prompt, model=model, **kwargs
                )
            except (ConnectionError, TimeoutError, httpx.TimeoutException, httpx.ConnectError, httpx.HTTPStatusError) as e:
                logger.error(f"Provider {effective_provider} failed: {e}")
                if not fallback:
                    raise

                fallback_order = [
                    p for p in ["openrouter", "groq", "deepseek"] if p != effective_provider and p in self.providers
                ]
                for fallback_name in fallback_order:
                    fb_llm = self.providers[fallback_name]
                    with trace_llm_call(
                        name=f"{trace_name}_fallback_{fallback_name}",
                        model=effective_model,
                        provider=fallback_name,
                        input_data={"prompt": prompt, "system_prompt": system_prompt, "fallback_for": effective_provider},
                        tenant_id=resolved_tenant,
                        session_id=resolved_session,
                    ) as fb_trace:
                        try:
                            response = await fb_llm.generate(
                                prompt=prompt, system_prompt=system_prompt, model=None, **kwargs
                            )
                        except (
                            ConnectionError,
                            TimeoutError,
                            httpx.TimeoutException,
                            httpx.ConnectError,
                            httpx.HTTPStatusError,
                        ) as fb_e:
                            logger.error(f"Fallback {fallback_name} also failed: {fb_e}")
                            continue
                        _populate_trace_record(fb_trace, response)
                        await self._record_task_usage(
                            tenant_id=resolved_tenant,
                            session_id=resolved_session,
                            task_id=task_id,
                            agent_type=agent_type or "unknown",
                            response=response,
                            fallback_provider=fallback_name,
                            fallback_model=effective_model,
                        )
                        if use_cache:
                            await self.cache.set(prompt, effective_model, response, system_prompt)
                        return response

                raise RuntimeError("All LLM providers failed")

            _populate_trace_record(trace, response)
            await self._record_task_usage(
                tenant_id=resolved_tenant,
                session_id=resolved_session,
                task_id=task_id,
                agent_type=agent_type or "unknown",
                response=response,
                fallback_provider=effective_provider,
                fallback_model=effective_model,
            )
            if use_cache:
                await self.cache.set(prompt, effective_model, response, system_prompt)
            return response

    @staticmethod
    async def _record_task_usage(
        *,
        tenant_id: str,
        session_id: str | None,
        task_id: str | None,
        agent_type: str,
        response: LLMResponse,
        fallback_provider: str,
        fallback_model: str,
    ) -> None:
        if task_id is None:
            return
        try:
            from solvyn.agents.cost_tracker import cost_tracker

            await cost_tracker.record(
                tenant_id=tenant_id,
                session_id=session_id,
                task_id=task_id,
                agent_type=agent_type,
                provider=response.provider or fallback_provider,
                model=response.model or fallback_model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                cost=response.cost,
            )
        except Exception:  # noqa: BLE001 — usage persistence must not fail engineering work
            logger.exception("Could not persist task-scoped LLM usage")

    @staticmethod
    def _resolve_tenant_id(override: str | None) -> str:
        if override:
            return override
        try:
            from solvyn.multi_tenant.middleware import tenant_context

            slug = tenant_context.tenant_slug
            if slug:
                return slug
        except Exception:  # noqa: BLE001 — never break on context access
            pass
        return settings.default_tenant_id

    @staticmethod
    def _resolve_session_id(override: str | None) -> str | None:
        if override:
            return override
        return None

    def select_model(self, strategy: str = "default", **kwargs: Any) -> tuple[str, str]:
        if strategy == "cheapest":
            info = self.registry.find_cheapest()
        elif strategy == "fastest":
            info = self.registry.find_fastest()
        elif strategy == "quality":
            quality = kwargs.get("min_quality", "high")
            models = self.registry.find_by_quality(quality)
            info = models[0] if models else None
        else:
            return (settings.llm_default_provider, settings.llm_default_model)

        if info is None:
            return (settings.llm_default_provider, settings.llm_default_model)
        return (info.provider, info.model)

    def get_cache_stats(self) -> dict[str, Any]:
        return self.cache.get_stats()

    def get_model_registry(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": m.provider,
                "model": m.model,
                "cost_per_1m_prompt": m.cost_per_1m_prompt,
                "cost_per_1m_completion": m.cost_per_1m_completion,
                "context_window": m.context_window,
                "latency_class": m.latency_class,
                "quality_class": m.quality_class,
            }
            for m in self.registry.list_models()
        ]


llm_router = LLMRouter()


def _populate_trace_record(record: dict[str, Any], response: LLMResponse) -> None:
    """Populate a ``trace_llm_call`` record from an LLM response.

    Best-effort: the LLM call itself must not be affected by errors here.
    """
    try:
        record["output"] = response.content
        record["prompt_tokens"] = response.prompt_tokens
        record["completion_tokens"] = response.completion_tokens
        record["total_tokens"] = response.total_tokens
        record["cost"] = response.cost
        if not record.get("model") and response.model:
            record["model"] = response.model
        if not record.get("provider") and response.provider:
            record["provider"] = response.provider
    except Exception as exc:  # noqa: BLE001 — never break on record population
        logger.debug("Failed to populate trace record: %s", exc)
