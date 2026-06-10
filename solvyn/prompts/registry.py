from __future__ import annotations

import logging
from typing import Any

from jinja2 import BaseLoader, Environment

logger = logging.getLogger(__name__)

_jinja_env = Environment(loader=BaseLoader(), autoescape=False)


class PromptTemplate:
    """A versioned, Jinja2-based prompt template."""

    def __init__(
        self,
        name: str,
        template: str,
        version: str = "1.0.0",
        description: str = "",
        variables: list[str] | None = None,
    ) -> None:
        self.name = name
        self.template = template
        self.version = version
        self.description = description
        self.variables = variables or []
        self._compiled = _jinja_env.from_string(template)

    def render(self, **kwargs: Any) -> str:
        return self._compiled.render(**kwargs)

    def get_variable_names(self) -> set[str]:
        return self._compiled.module.__dict__.keys() if hasattr(self._compiled, "module") else set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "template": self.template,
            "version": self.version,
            "description": self.description,
            "variables": self.variables,
        }


class PromptRegistry:
    """Versioned prompt registry with Jinja2 templating."""

    def __init__(self) -> None:
        self._prompts: dict[str, list[PromptTemplate]] = {}
        self._active: dict[str, str] = {}

    def register(self, name: str, template: str, version: str = "1.0.0", description: str = "", variables: list[str] | None = None) -> PromptTemplate:
        prompt = PromptTemplate(name, template, version, description, variables)
        if name not in self._prompts:
            self._prompts[name] = []
        self._prompts[name].append(prompt)
        self._active[name] = version
        logger.debug(f"Registered prompt '{name}' v{version}")
        return prompt

    def get(self, name: str, version: str | None = None) -> PromptTemplate:
        prompts = self._prompts.get(name, [])
        if not prompts:
            raise KeyError(f"Prompt '{name}' not found in registry")
        target_version = version or self._active.get(name, "")
        for p in prompts:
            if p.version == target_version:
                return p
        return prompts[-1]

    def render(self, name: str, version: str | None = None, **kwargs: Any) -> str:
        prompt = self.get(name, version)
        return prompt.render(**kwargs)

    def list_prompts(self) -> list[dict[str, Any]]:
        result = []
        for name, versions in self._prompts.items():
            active = self._active.get(name, "")
            result.append(
                {
                    "name": name,
                    "active_version": active,
                    "versions": [v.version for v in versions],
                }
            )
        return result

    def set_active_version(self, name: str, version: str) -> None:
        prompts = self._prompts.get(name, [])
        versions = {p.version for p in prompts}
        if version not in versions:
            raise ValueError(f"Version '{version}' not found for prompt '{name}'. Available: {versions}")
        self._active[name] = version

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompts": self.list_prompts(),
            "total_templates": sum(len(v) for v in self._prompts.values()),
        }


prompt_registry = PromptRegistry()
