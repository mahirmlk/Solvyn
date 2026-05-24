from typing import Any

import httpx

from solvyn.config import settings
from solvyn.llm.providers.base import LLMProvider, LLMResponse


class DeepSeekProvider(LLMProvider):
    BASE_URL = "https://api.deepseek.com/v1"

    def __init__(self) -> None:
        self.api_key = settings.deepseek_api_key
        self.http_client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def generate(self, prompt: str, system_prompt: str | None = None, model: str | None = None, **kwargs: Any) -> LLMResponse:
        model = model or "deepseek-chat"
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
            provider="deepseek",
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
            "deepseek-chat": (0.27, 1.10),
            "deepseek-reasoner": (0.55, 2.19),
        }
        return costs.get(model, (0.50, 1.00))
