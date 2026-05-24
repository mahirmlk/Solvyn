
import pytest

from solvyn.agents.schema import AgentCapability


def test_agent_registry_list() -> None:
    from solvyn.agents.registry import agent_registry
    agents = agent_registry.list_agents()
    assert len(agents) >= 9
    assert "product-manager" in agents
    assert "system-designer" in agents
    assert "backend-engineer" in agents
    assert "frontend-engineer" in agents
    assert "qa-engineer" in agents
    assert "code-reviewer" in agents
    assert "security-auditor" in agents
    assert "devops-engineer" in agents
    assert "documentation-writer" in agents


def test_agent_registry_find_by_capability() -> None:
    from solvyn.agents.registry import agent_registry
    designers = agent_registry.find_by_capability(AgentCapability.SYSTEM_DESIGN)
    assert len(designers) >= 1
    assert any(m.agent_type == "system-designer" for m in designers)


def test_agent_registry_create() -> None:
    from solvyn.agents.registry import agent_registry
    agent = agent_registry.create("product-manager", session_id="s1", tenant_id="t1")
    assert agent is not None
    assert agent.metadata.agent_type == "product-manager"


def test_agent_registry_create_unknown() -> None:
    from solvyn.agents.registry import agent_registry
    with pytest.raises(ValueError, match="Unknown agent type"):
        agent_registry.create("nonexistent", session_id="s1", tenant_id="t1")
