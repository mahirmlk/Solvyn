"""Tests for A2A integration in communication.py."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from solvyn.a2a.protocol import A2AEnvelope, A2AMessageType, TaskRequest, TaskResponse, TaskStatus
from solvyn.agents.schema import AgentMetadata, AgentCapability


# ---------------------------------------------------------------------------
# AgentCommunication.register_agent_instance
# ---------------------------------------------------------------------------

def test_register_agent_instance() -> None:
    """Registering an agent should make it available for A2A routing."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()
    mock_agent = MagicMock()
    mock_agent.agent_id = "agent-1"
    mock_agent.metadata.agent_type = "code-writer"

    comm.register_agent_instance(mock_agent)

    assert "agent-1" in comm._active_agents
    assert "code-writer" in comm._a2a_handlers


def test_unregister_agent_instance() -> None:
    """Unregistering should remove the agent."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()
    mock_agent = MagicMock()
    mock_agent.agent_id = "agent-1"
    mock_agent.metadata.agent_type = "code-writer"

    comm.register_agent_instance(mock_agent)
    comm.unregister_agent_instance("agent-1")

    assert "agent-1" not in comm._active_agents


# ---------------------------------------------------------------------------
# _find_agent_by_type
# ---------------------------------------------------------------------------

def test_find_agent_by_type_found() -> None:
    """Should find an agent by its type."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()
    mock_agent = MagicMock()
    mock_agent.agent_id = "agent-1"
    mock_agent.metadata.agent_type = "code-writer"

    comm.register_agent_instance(mock_agent)
    found = comm._find_agent_by_type("code-writer")

    assert found is mock_agent


def test_find_agent_by_type_not_found() -> None:
    """Should return None when no agent of that type exists."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()
    found = comm._find_agent_by_type("nonexistent")
    assert found is None


# ---------------------------------------------------------------------------
# A2A handler execution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a2a_handler_executes_agent() -> None:
    """The A2A handler should call agent.run() with the task input data."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()
    mock_agent = AsyncMock()
    mock_agent.agent_id = "agent-1"
    mock_agent.metadata.agent_type = "code-writer"
    mock_agent.run.return_value = {"output": "generated code"}

    comm.register_agent_instance(mock_agent)

    handler = comm._a2a_handlers["code-writer"]
    task = TaskRequest(task_type="code_gen", input_data={"prompt": "write hello world"})

    result = await handler(task)

    mock_agent.run.assert_awaited_once_with({"prompt": "write hello world"})
    assert result == {"output": "generated code"}


@pytest.mark.asyncio
async def test_a2a_handler_raises_when_no_agent() -> None:
    """Handler should raise ValueError when no agent instance is available."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()
    # Manually create a handler without registering an agent
    comm._a2a_handlers["ghost"] = comm._create_a2a_handler("ghost")

    handler = comm._a2a_handlers["ghost"]
    task = TaskRequest(input_data={})

    with pytest.raises(ValueError, match="No agent instance"):
        await handler(task)


# ---------------------------------------------------------------------------
# send_a2a_task convenience method
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_a2a_task() -> None:
    """send_a2a_task should create envelope + task and send via broker."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()

    with patch("solvyn.a2a.broker.A2ABroker") as MockBroker:
        mock_broker = MockBroker.return_value
        mock_broker.send_task = AsyncMock(return_value="pub-id-123")
        mock_broker.create_envelope.return_value = A2AEnvelope(
            message_id="env-1",
            from_agent="planner",
            to_agent="coder",
            session_id="s1",
            tenant_id="t1",
        )
        envelope = await comm.send_a2a_task(
            from_agent="planner",
            to_agent="coder",
            input_data={"task": "build API"},
            session_id="s1",
            tenant_id="t1",
        )

    assert envelope.from_agent == "planner"
    assert envelope.to_agent == "coder"
    mock_broker.send_task.assert_awaited_once()


# ---------------------------------------------------------------------------
# Legacy message routing still works
# ---------------------------------------------------------------------------

def test_register_handler_legacy() -> None:
    """Legacy register_handler should populate both _handlers and _a2a_handlers."""
    from solvyn.agents.communication import AgentCommunication

    comm = AgentCommunication()

    mock_cls = MagicMock()
    mock_cls.return_value.metadata.agent_type = "test-agent"

    comm.register_handler("test-agent", mock_cls)

    assert "test-agent" in comm._handlers
    assert "test-agent" in comm._a2a_handlers
