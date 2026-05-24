from typing import Any

import httpx

from solvyn.config import settings
from solvyn.llm.providers.base import LLMProvider, LLMResponse


class OpenRouterProvider(LLMProvider):
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self) -> None:
        self.api_key = settings.openrouter_api_key
        self.http_client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def generate(self, prompt: str, system_prompt: str | None = None, model: str | None = None, **kwargs: Any) -> LLMResponse:
        model = model or settings.llm_default_model
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response = await self.http_client.post(
            "/chat/completions",
            json={"model": model, "messages": messages, **kwargs},
        )
        if response.status_code >= 400:
            error_body = response.text
            raise httpx.HTTPStatusError(
                f"API error: {error_body}",
                request=response.request,
                response=response,
            )
        data = response.json()

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)
        prompt_cost, completion_cost = self.get_cost_per_token(model)
        cost = (prompt_tokens * prompt_cost + completion_tokens * completion_cost) / 1_000_000

        return LLMResponse(
            content=data["choices"][0]["message"]["content"],
            provider="openrouter",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost=cost,
        )

    async def close(self) -> None:
        await self.http_client.aclose()

    def get_cost_per_token(self, model: str) -> tuple[float, float]:
        costs = {
            "openai/gpt-4o": (2.50, 10.00),
            "openai/gpt-4o-mini": (0.15, 0.60),
            "anthropic/claude-3.5-sonnet": (3.00, 15.00),
            "google/gemini-2.0-flash-001": (0.10, 0.40),
        }
        return costs.get(model, (1.00, 3.00))
