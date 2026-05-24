import logging
from typing import Any

import httpx

from solvyn.config import settings
from solvyn.llm.providers.base import LLMProvider, LLMResponse
from solvyn.llm.providers.deepseek import DeepSeekProvider
from solvyn.llm.providers.groq import GroqProvider
from solvyn.llm.providers.openrouter import OpenRouterProvider

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self) -> None:
        self.providers: dict[str, LLMProvider] = {
            "openrouter": OpenRouterProvider(),
            "groq": GroqProvider(),
            "deepseek": DeepSeekProvider(),
        }

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
        **kwargs: Any,
    ) -> LLMResponse:
        provider_name = provider or settings.llm_default_provider
        llm = self.providers.get(provider_name)

        if llm is None:
            raise ValueError(f"Unknown provider: {provider_name}. Available: {list(self.providers.keys())}")

        try:
            return await llm.generate(prompt=prompt, system_prompt=system_prompt, model=model, **kwargs)
        except (httpx.HTTPError, ConnectionError, TimeoutError) as e:
            logger.error(f"Provider {provider_name} failed: {e}")
            if not fallback:
                raise

            fallback_order = [p for p in ["openrouter", "groq", "deepseek"] if p != provider_name and p in self.providers]
            for fallback_name in fallback_order:
                try:
                    logger.info(f"Falling back to {fallback_name}")
                    fb_llm = self.providers[fallback_name]
                    return await fb_llm.generate(prompt=prompt, system_prompt=system_prompt, model=None, **kwargs)
                except (httpx.HTTPError, ConnectionError, TimeoutError) as fb_e:
                    logger.error(f"Fallback {fallback_name} also failed: {fb_e}")
                    continue

            raise RuntimeError("All LLM providers failed")


llm_router = LLMRouter()
