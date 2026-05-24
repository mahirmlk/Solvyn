import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def test_agent_graph_add_node() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    nid = g.add_node("agent-a", {"task": "test"})
    assert nid is not None
    assert len(g.nodes) == 1
    node = g.nodes[nid]
    assert node.agent_type == "agent-a"
    assert node.input == {"task": "test"}


def test_agent_graph_add_node_default_input() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    nid = g.add_node("agent-a")
    assert g.nodes[nid].input == {}


def test_agent_graph_add_edge() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    n1 = g.add_node("a")
    n2 = g.add_node("b")
    g.add_edge(n1, n2, "completed")
    assert len(g.edges) == 1
    assert g.edges[0].source == n1
    assert g.edges[0].target == n2
    assert g.edges[0].condition == "completed"


def test_agent_graph_get_start_nodes() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    n1 = g.add_node("a")
    n2 = g.add_node("b")
    g.add_edge(n1, n2)
    starts = g.get_start_nodes()
    assert n1 in starts
    assert n2 not in starts


def test_agent_graph_get_next_nodes() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    n1 = g.add_node("a")
    n2 = g.add_node("b")
    n3 = g.add_node("c")
    g.add_edge(n1, n2)
    g.add_edge(n1, n3)
    next_nodes = g.get_next_nodes(n1)
    assert len(next_nodes) == 2
    assert n2 in next_nodes
    assert n3 in next_nodes


def test_agent_graph_get_prev_nodes() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    n1 = g.add_node("a")
    n2 = g.add_node("b")
    g.add_edge(n1, n2)
    prev = g.get_prev_nodes(n2)
    assert prev == [n1]


def test_agent_graph_to_dict() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    g.add_node("a")
    d = g.to_dict()
    assert "nodes" in d
    assert "edges" in d
    assert "graph_id" in d


def test_agent_graph_to_json() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    g.add_node("a")
    j = g.to_json()
    assert isinstance(j, str)
    assert "nodes" in j


@pytest.mark.asyncio
async def test_agent_graph_execute() -> None:
    from solvyn.agents.graph import AgentGraph
    g = AgentGraph()
    g.add_node("product-manager", {"text": "build a feature"})
    g.add_node("system-designer")
    g.add_edge(list(g.nodes.keys())[0], list(g.nodes.keys())[1])

    mock_agent = __import__("unittest").mock.AsyncMock()
    mock_agent.run.return_value = {"content": "done", "status": "completed"}
    mock_agent.metadata.agent_type = "mock"

    with patch("solvyn.agents.graph.agent_registry.create", return_value=mock_agent):
        result = await g.execute("s1", "t1")
        assert result.status == "completed"
        assert len(result.nodes) == 2


@pytest.mark.asyncio
async def test_agent_graph_resolves_predecessor_output_before_running_dependent_node() -> None:
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    source = graph.add_node("product-manager", {"text": "build a feature"})
    target = graph.add_node("system-designer", {"requirements": {"$node_output": source}})
    graph.add_edge(source, target)

    source_agent = AsyncMock()
    source_agent.run.return_value = {"content": "approved requirements"}
    target_agent = AsyncMock()
    target_agent.run.return_value = {"content": "design"}

    with patch("solvyn.agents.graph.agent_registry.create", side_effect=[source_agent, target_agent]):
        result = await graph.execute("s1", "t1")

    target_agent.run.assert_awaited_once_with({"requirements": {"content": "approved requirements"}})
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_agent_graph_marks_dependent_node_skipped_when_predecessor_fails() -> None:
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    source = graph.add_node("product-manager")
    target = graph.add_node("system-designer")
    graph.add_edge(source, target)

    source_agent = AsyncMock()
    source_agent.run.side_effect = RuntimeError("generation failed")

    with patch("solvyn.agents.graph.agent_registry.create", return_value=source_agent):
        result = await asyncio.wait_for(graph.execute("s1", "t1"), timeout=0.2)

    assert result.status == "failed"
    assert graph.nodes[target].status == "skipped"
    assert "dependency failed" in (graph.nodes[target].error or "")
