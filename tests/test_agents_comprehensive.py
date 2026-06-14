"""Comprehensive unit tests for every Solvyn agent.

Each test verifies that:

* the agent is registered in the ``agent_registry``;
* the agent can be instantiated with session/tenant identifiers;
* the agent's ``execute`` returns the expected output schema (with the LLM
  router mocked);
* the agent transitions through its lifecycle states correctly;
* on failure the agent captures the error in its ``execution_node`` so the
  surrounding ``RetryHandler`` can react to it.

The tests are tagged with the ``unit`` marker (see ``pyproject.toml``) and
are picked up by ``pytest -m unit``.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from solvyn.agents.schema import AgentCapability, AgentStatus

pytestmark = pytest.mark.unit


# ─── Agent definitions for parametrization ──────────────────────────────

# Each entry: (agent_type, expected_capability, expected_output_keys)
AGENT_SPECS: list[tuple[str, AgentCapability, set[str]]] = [
    ("product-manager", AgentCapability.PRODUCT_MANAGEMENT, {"requirement", "analysis", "provider", "model", "cost"}),
    ("system-designer", AgentCapability.SYSTEM_DESIGN, {"requirements", "design", "provider", "model", "cost"}),
    ("backend-engineer", AgentCapability.CODE_GENERATION, {"specification", "code", "provider", "model", "cost"}),
    ("frontend-engineer", AgentCapability.CODE_GENERATION, {"design_spec", "code", "provider", "model", "cost"}),
    ("qa-engineer", AgentCapability.TESTING, {"code_tested", "tests", "provider", "model", "cost"}),
    ("code-reviewer", AgentCapability.CODE_REVIEW, {"code_reviewed", "review", "provider", "model", "cost"}),
    ("security-auditor", AgentCapability.SECURITY, {"code_audited", "audit", "provider", "model", "cost"}),
    ("devops-engineer", AgentCapability.DEVOPS, {"project", "config", "provider", "model", "cost"}),
    ("documentation-writer", AgentCapability.DOCUMENTATION, {"source", "documentation", "provider", "model", "cost"}),
]

AGENT_IDS = [spec[0] for spec in AGENT_SPECS]


# ─── Module-level LLM mock ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _mock_llm_router() -> Any:
    """Replace ``llm_router.generate`` with a deterministic stub.

    The stub returns a valid :class:`LLMResponse` whose ``content`` echoes
    the prompt prefix. Agents only care about the dataclass shape, so this
    is enough to exercise their full code path.
    """
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router

    llm_router.cache._local_cache.clear()

    async def _mock_generate(
        prompt: str,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        fallback: bool = True,
        use_cache: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(
            content=f"mock-output :: {prompt[:60]}",
            provider=provider or "openrouter",
            model=model or "openai/gpt-4o",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
            cost=0.0001,
        )

    original = llm_router.generate
    llm_router.generate = _mock_generate  # type: ignore[method-assign]
    try:
        yield
    finally:
        llm_router.generate = original  # type: ignore[method-assign]


# ─── Registration / instantiation ───────────────────────────────────────


@pytest.mark.parametrize("agent_type", AGENT_IDS)
def test_agent_is_registered(agent_type: str) -> None:
    """Every specialized agent must be present in the global registry."""
    from solvyn.agents.registry import agent_registry

    assert agent_type in agent_registry.list_agents()


@pytest.mark.parametrize("agent_type, capability, _", AGENT_SPECS)
def test_agent_has_expected_capability(agent_type: str, capability: AgentCapability, _: set[str]) -> None:
    """Each agent must declare the capability it was designed for."""
    from solvyn.agents.registry import agent_registry

    metadata = agent_registry.get_metadata(agent_type)
    assert metadata is not None
    assert capability in metadata.capabilities


@pytest.mark.parametrize("agent_type", AGENT_IDS)
def test_agent_can_be_instantiated_via_registry(agent_type: str) -> None:
    """The registry factory must produce a working agent instance."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create(agent_type, session_id="s1", tenant_id="t1")
    assert agent is not None
    assert agent.metadata.agent_type == agent_type
    assert agent.session_id == "s1"
    assert agent.tenant_id == "t1"
    assert agent.status == AgentStatus.IDLE
    assert agent.agent_id  # auto-generated


@pytest.mark.parametrize("agent_type", AGENT_IDS)
def test_agent_metadata_shape(agent_type: str) -> None:
    """Agent metadata must have a name, description, and at least one capability."""
    from solvyn.agents.registry import agent_registry

    metadata = agent_registry.get_metadata(agent_type)
    assert metadata is not None
    assert metadata.name
    assert metadata.description
    assert len(metadata.capabilities) >= 1
    assert metadata.version


# ─── Output schema (with mocked LLM) ───────────────────────────────────


@pytest.mark.parametrize("agent_type, _, expected_keys", AGENT_SPECS)
@pytest.mark.asyncio
async def test_agent_returns_expected_output_schema(
    agent_type: str, _: AgentCapability, expected_keys: set[str]
) -> None:
    """Each agent's execute() must return a dict containing the expected keys."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create(agent_type, session_id="s1", tenant_id="t1")
    # Every agent accepts at least a "text"/"code"/"specification" fallback.
    output = await agent.run({"text": "test input for " + agent_type})

    assert isinstance(output, dict)
    missing = expected_keys - set(output.keys())
    assert not missing, f"{agent_type} output missing keys: {missing}"
    # LLM-derived fields are populated from the mock
    assert output["provider"] == "openrouter"
    assert output["model"] == "openai/gpt-4o"
    assert output["cost"] >= 0


@pytest.mark.asyncio
async def test_product_manager_execute_uses_requirement_field() -> None:
    """Product Manager should surface the requirement it analysed."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create("product-manager", session_id="s1", tenant_id="t1")
    output = await agent.run({"requirement": "Build a chat app"})
    assert output["requirement"] == "Build a chat app"
    assert "analysis" in output


@pytest.mark.asyncio
async def test_backend_engineer_execute_uses_specification_field() -> None:
    """Backend Engineer should surface the spec and language choice."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create("backend-engineer", session_id="s1", tenant_id="t1")
    output = await agent.run({"specification": "REST API for orders", "language": "python"})
    assert output["specification"] == "REST API for orders"


@pytest.mark.asyncio
async def test_security_auditor_execute_uses_code_field() -> None:
    """Security Auditor should accept code via the ``code`` input key."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create("security-auditor", session_id="s1", tenant_id="t1")
    output = await agent.run({"code": "def f(x):\n    return eval(x)\n"})
    assert "audit" in output
    assert output["code_audited"].startswith("def f")


# ─── Lifecycle / state transitions ──────────────────────────────────────


@pytest.mark.parametrize("agent_type", AGENT_IDS)
@pytest.mark.asyncio
async def test_agent_transitions_idle_to_completed(agent_type: str) -> None:
    """A successful run transitions IDLE → RUNNING → COMPLETED."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create(agent_type, session_id="s1", tenant_id="t1")
    assert agent.status == AgentStatus.IDLE

    await agent.run({"text": "trigger"})

    assert agent.status == AgentStatus.COMPLETED
    assert agent.execution_node is not None
    assert agent.execution_node.status == "completed"
    assert agent.execution_node.completed_at is not None
    assert agent.execution_node.started_at is not None
    assert agent.execution_node.error is None


@pytest.mark.parametrize("agent_type", AGENT_IDS)
@pytest.mark.asyncio
async def test_agent_records_execution_node_metrics(agent_type: str) -> None:
    """Each run attaches an ``ExecutionNode`` carrying the agent's metadata.

    This is the in-process "metric" for an agent run: status, timestamps,
    and the produced output.  Tests verify the structure, not the HTTP
    middleware counters.
    """
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create(agent_type, session_id="s1", tenant_id="t1")
    await agent.run({"text": "metrics"})

    node = agent.execution_node
    assert node is not None
    assert node.agent_type == agent_type
    assert node.id  # auto-generated
    assert node.status == "completed"
    assert isinstance(node.output, dict)
    # ``state`` should retain both the input and the output for replay
    assert "input" in agent.state
    assert "output" in agent.state


# ─── Failure / retry semantics ──────────────────────────────────────────


@pytest.mark.parametrize("agent_type", AGENT_IDS)
@pytest.mark.asyncio
async def test_agent_captures_failure_for_retry_handler(agent_type: str) -> None:
    """When ``execute`` raises, ``run`` must mark the agent FAILED and record the error.

    The :class:`RetryHandler` operates *outside* the agent, so the contract
    the agent must honour on failure is: state = FAILED, error captured,
    exception re-raised.  This test pins down that contract.
    """
    from solvyn.agents.registry import agent_registry

    boom = RuntimeError(f"simulated failure for {agent_type}")

    with patch.object(
        agent_registry._agents[agent_type],
        "execute",
        AsyncMock(side_effect=boom),
    ):
        agent = agent_registry.create(agent_type, session_id="s1", tenant_id="t1")
        with pytest.raises(RuntimeError, match=f"simulated failure for {agent_type}"):
            await agent.run({"text": "should fail"})

    assert agent.status == AgentStatus.FAILED
    assert agent.execution_node is not None
    assert agent.execution_node.status == "failed"
    assert agent.execution_node.error == str(boom)
    assert agent.execution_node.completed_at is not None
    # The output was never produced
    assert agent.execution_node.output is None


@pytest.mark.asyncio
async def test_failed_agent_can_be_reset_for_retry() -> None:
    """After failure, ``reset`` returns the agent to IDLE so a retry can run."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create("product-manager", session_id="s1", tenant_id="t1")

    with patch.object(agent_registry._agents["product-manager"], "execute", AsyncMock(side_effect=ValueError("nope"))):
        with pytest.raises(ValueError):
            await agent.run({"text": "boom"})

    assert agent.status == AgentStatus.FAILED

    agent.reset()
    assert agent.status == AgentStatus.IDLE
    assert agent.state == {}
    assert agent.execution_node is None

    # After reset, a retry against the real (mocked) LLM should succeed.
    output = await agent.run({"text": "retry payload"})
    assert agent.status == AgentStatus.COMPLETED
    assert "analysis" in output


# ─── Checkpoint / approval flow ─────────────────────────────────────────


@pytest.mark.parametrize("agent_type", AGENT_IDS)
@pytest.mark.asyncio
async def test_agent_can_request_approval_checkpoint(agent_type: str) -> None:
    """``request_approval`` should pause the agent and stash a checkpoint id."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create(agent_type, session_id="s1", tenant_id="t1")
    checkpoint = await agent.request_approval(summary="please review", state_snapshot={"k": "v"})

    assert checkpoint.id
    assert checkpoint.agent_type == agent_type
    assert checkpoint.session_id == "s1"
    assert checkpoint.state == {"k": "v"}
    assert agent.status == AgentStatus.WAITING_APPROVAL
    assert agent.state["checkpoint"]["summary"] == "please review"


@pytest.mark.asyncio
async def test_agent_resume_from_approved_checkpoint() -> None:
    """Approving a checkpoint transitions the agent back to RUNNING."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create("system-designer", session_id="s1", tenant_id="t1")
    await agent.request_approval(summary="looks good?")

    agent.resume_from_checkpoint(resolution="approved", approved=True)
    assert agent.status == AgentStatus.RUNNING
    assert "checkpoint" not in agent.state


@pytest.mark.asyncio
async def test_agent_resume_from_rejected_checkpoint() -> None:
    """Rejecting a checkpoint leaves the agent paused and records the reason."""
    from solvyn.agents.registry import agent_registry

    agent = agent_registry.create("qa-engineer", session_id="s1", tenant_id="t1")
    await agent.request_approval(summary="ship?")

    agent.resume_from_checkpoint(resolution="need more tests", approved=False)
    assert agent.status == AgentStatus.PAUSED
    assert agent.state["rejected_reason"] == "need more tests"


# ─── Capability-based lookup ───────────────────────────────────────────


@pytest.mark.parametrize("agent_type, capability, _", AGENT_SPECS)
def test_agent_findable_by_capability(agent_type: str, capability: AgentCapability, _: set[str]) -> None:
    """find_by_capability should be able to locate each agent."""
    from solvyn.agents.registry import agent_registry

    matches = agent_registry.find_by_capability(capability)
    types = {m.agent_type for m in matches}
    assert agent_type in types


# ─── Cross-agent sanity checks ──────────────────────────────────────────


def test_all_nine_agents_present() -> None:
    """The registry must contain exactly nine specialized agents."""
    from solvyn.agents.registry import agent_registry

    agents = agent_registry.list_agents()
    assert len(agents) == 9
    expected = {
        "product-manager",
        "system-designer",
        "backend-engineer",
        "frontend-engineer",
        "qa-engineer",
        "code-reviewer",
        "security-auditor",
        "devops-engineer",
        "documentation-writer",
    }
    assert set(agents.keys()) == expected
