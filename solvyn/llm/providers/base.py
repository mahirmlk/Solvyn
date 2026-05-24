from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMResponse:
    content: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost: float

    def __post_init__(self) -> None:
        if self.content is None:
            self.content = ""


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, system_prompt: str | None = None, model: str | None = None, **kwargs: Any) -> LLMResponse:
        ...

    @abstractmethod
    def get_cost_per_token(self, model: str) -> tuple[float, float]:
        ...
