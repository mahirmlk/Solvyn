from typing import Any

import httpx

from solvyn.config import settings
from solvyn.llm.providers.base import LLMProvider, LLMResponse


class GroqProvider(LLMProvider):
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self) -> None:
        self.api_key = settings.groq_api_key
        self.http_client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def generate(self, prompt: str, system_prompt: str | None = None, model: str | None = None, **kwargs: Any) -> LLMResponse:
        model = model or "llama-3.3-70b-versatile"
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

        choices = data.get("choices", [])
        content = choices[0].get("message", {}).get("content", "") if choices else ""
        return LLMResponse(
            content=content,
            provider="groq",
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
            "llama-3.3-70b-versatile": (0.59, 0.79),
            "llama-3.1-8b-instant": (0.05, 0.08),
            "mixtral-8x7b-32768": (0.24, 0.24),
        }
        return costs.get(model, (0.50, 0.50))
