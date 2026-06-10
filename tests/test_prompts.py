from __future__ import annotations

import pytest

from solvyn.prompts.registry import PromptRegistry, PromptTemplate, prompt_registry


class TestPromptTemplate:
    def test_render(self) -> None:
        tpl = PromptTemplate("test", "Hello {{ name }}!")
        result = tpl.render(name="World")
        assert result == "Hello World!"

    def test_render_with_defaults(self) -> None:
        tpl = PromptTemplate("test", "Count: {{ count | default(0) }}")
        result = tpl.render()
        assert result == "Count: 0"

    def test_to_dict(self) -> None:
        tpl = PromptTemplate("t", "body", version="2.0", description="desc", variables=["x"])
        d = tpl.to_dict()
        assert d["version"] == "2.0"
        assert d["variables"] == ["x"]


class TestPromptRegistry:
    def test_register_and_get(self) -> None:
        reg = PromptRegistry()
        reg.register("test_prompt", "Hello {{ name }}!", version="1.0")
        tpl = reg.get("test_prompt")
        assert tpl.render(name="AI") == "Hello AI!"

    def test_get_not_found(self) -> None:
        reg = PromptRegistry()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_render(self) -> None:
        reg = PromptRegistry()
        reg.register("greet", "Hi {{ who }}!")
        result = reg.render("greet", who="there")
        assert result == "Hi there!"

    def test_list_prompts(self) -> None:
        reg = PromptRegistry()
        reg.register("a", "x")
        reg.register("b", "y")
        prompts = reg.list_prompts()
        assert len(prompts) == 2

    def test_set_active_version(self) -> None:
        reg = PromptRegistry()
        reg.register("p", "v1", version="1.0")
        reg.register("p", "v2", version="2.0")
        reg.set_active_version("p", "1.0")
        tpl = reg.get("p")
        assert tpl.version == "1.0"

    def test_set_active_version_not_found(self) -> None:
        reg = PromptRegistry()
        reg.register("p", "v1", version="1.0")
        with pytest.raises(ValueError):
            reg.set_active_version("p", "9.0")

    def test_to_dict(self) -> None:
        reg = PromptRegistry()
        reg.register("x", "y")
        d = reg.to_dict()
        assert d["total_templates"] == 1


class TestGlobalRegistry:
    def test_global_registry_exists(self) -> None:
        assert prompt_registry is not None
