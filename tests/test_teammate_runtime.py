from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_initial_teammate_roles_map_to_shared_registered_agents() -> None:
    from solvyn.agents.teammate_runtime import ROLE_AGENT_TYPES, validate_role_agent_type

    assert set(ROLE_AGENT_TYPES) == {"Tech Lead", "Software Engineer", "QA Engineer", "Security Engineer"}
    for role, agent_type in ROLE_AGENT_TYPES.items():
        validate_role_agent_type(role, agent_type)


def test_role_cannot_be_bound_to_an_unrelated_agent() -> None:
    from solvyn.agents.teammate_runtime import validate_role_agent_type

    with pytest.raises(ValueError, match="requires agent type"):
        validate_role_agent_type("QA Engineer", "backend-engineer")
    with pytest.raises(ValueError, match="Unsupported teammate role"):
        validate_role_agent_type("Researcher", "backend-engineer")


def test_project_context_includes_only_given_project_and_is_bounded() -> None:
    from solvyn.agents.teammate_runtime import _MAX_CONTEXT_CHARS, build_teammate_system_context

    profile = SimpleNamespace(
        role="Software Engineer",
        responsibilities="Implement assigned work",
        system_instructions="Use the project conventions",
    )
    project = SimpleNamespace(
        name="Billing",
        slug="billing",
        description="Payment service",
        instructions="Keep migrations backward compatible",
    )
    knowledge = [SimpleNamespace(category="convention", key="language", value="Python")]

    context = build_teammate_system_context(profile, project, knowledge, ["READ_ONLY"])
    assert "Billing (billing)" in context
    assert "Keep migrations backward compatible" in context
    assert "[convention] language: Python" in context
    assert "READ_ONLY" in context
    assert len(context) <= _MAX_CONTEXT_CHARS


@pytest.mark.asyncio
async def test_agent_applies_teammate_context_and_token_limit(monkeypatch) -> None:
    from solvyn.agents.backend_engineer import BackendEngineerAgent
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router

    generate = AsyncMock(
        side_effect=[
            LLMResponse("response", "test", "test", 20, 96, 116, 0),
            LLMResponse("response", "test", "test", 20, 32, 52, 0),
        ]
    )
    monkeypatch.setattr(llm_router, "generate", generate)
    agent = BackendEngineerAgent()
    agent._teammate_system_context = "Project: Billing"
    agent._max_tokens_per_run = 128

    await agent.llm_generate("prompt", system_prompt="Role prompt", max_tokens=512)
    await agent.llm_generate("second prompt", system_prompt="Role prompt", max_tokens=512)

    first_kwargs, second_kwargs = [call.kwargs for call in generate.await_args_list]
    kwargs = first_kwargs
    assert "Role prompt" in kwargs["system_prompt"]
    assert "Project: Billing" in kwargs["system_prompt"]
    assert kwargs["max_tokens"] == 128
    assert second_kwargs["max_tokens"] == 32
