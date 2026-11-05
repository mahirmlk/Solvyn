"""Tests for parallel execution in AgentGraph."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_agent_graph_runs_independent_nodes_in_parallel() -> None:
    """Nodes at the same topological level should execute concurrently."""
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    # Three independent start nodes (no edges between them)
    graph.add_node("agent-a", {"task": "a"})
    graph.add_node("agent-b", {"task": "b"})
    graph.add_node("agent-c", {"task": "c"})

    execution_times: list[float] = []

    async def slow_agent_run(input_data: dict) -> dict:
        start = time.monotonic()
        await asyncio.sleep(0.05)  # Simulate work
        execution_times.append(time.monotonic() - start)
        return {"result": "done"}

    mock_agent = AsyncMock()
    mock_agent.run = slow_agent_run

    with patch("solvyn.agents.graph.agent_registry.create", return_value=mock_agent):
        result = await graph.execute("s1", "t1")

    assert result.status == "completed"
    assert len(result.nodes) == 3

    # All three should have completed
    for node in result.nodes:
        assert node.status == "completed"

    # If running in parallel, total time should be ~0.05s, not ~0.15s
    # Allow some tolerance
    assert len(execution_times) == 3


@pytest.mark.asyncio
async def test_agent_graph_runs_levels_sequentially() -> None:
    """Nodes in level 1 should wait for level 0 to complete."""
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    n1 = graph.add_node("agent-a", {"task": "a"})
    n2 = graph.add_node("agent-b", {"task": "b"})
    graph.add_edge(n1, n2)  # n2 depends on n1

    call_order: list[str] = []

    async def tracking_run(input_data: dict) -> dict:
        agent_type = input_data.get("task", "unknown")
        call_order.append(agent_type)
        return {"result": agent_type}

    mock_agent = AsyncMock()
    mock_agent.run = tracking_run

    with patch("solvyn.agents.graph.agent_registry.create", return_value=mock_agent):
        result = await graph.execute("s1", "t1")

    assert result.status == "completed"
    # agent-a should run before agent-b
    assert call_order == ["a", "b"]


@pytest.mark.asyncio
async def test_agent_graph_persists_node_lifecycle_checkpoints() -> None:
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    graph.add_node("agent-a", {"task": "a"})
    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value={"result": "done"})
    snapshots = []

    async def save(snapshot) -> None:
        snapshots.append(snapshot)

    with patch("solvyn.agents.graph.agent_registry.create", return_value=mock_agent):
        result = await graph.execute("s1", "t1", on_checkpoint=save, graph_id="run-1")

    assert result.id == "run-1"
    assert result.status == "completed"
    assert any(snapshot.status == "running" and snapshot.nodes[0].status == "running" for snapshot in snapshots)
    assert snapshots[-1].status == "completed"
    assert snapshots[-1].nodes[0].status == "completed"
    assert any(snapshot.nodes[0].status == "running" for snapshot in snapshots)


@pytest.mark.asyncio
async def test_agent_graph_resume_skips_completed_nodes() -> None:
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    completed = graph.add_node("agent-a", {"task": "already done"})
    pending = graph.add_node("agent-b", {"source": "agent-a.output"})
    graph.nodes[completed].status = "completed"
    graph.nodes[completed].output = {"result": "saved"}
    graph.add_edge(completed, pending)

    mock_agent = AsyncMock()
    mock_agent.run = AsyncMock(return_value={"result": "resumed"})
    with patch("solvyn.agents.graph.agent_registry.create", return_value=mock_agent):
        result = await graph.execute("s1", "t1", graph_id="run-1", execution_status="resuming")

    assert result.status == "completed"
    assert graph.nodes[completed].output == {"result": "saved"}
    assert mock_agent.run.await_count == 1
    assert mock_agent.run.await_args.args[0]["source"] == {"result": "saved"}


@pytest.mark.asyncio
async def test_agent_graph_parallel_failure_skips_downstream() -> None:
    """If one node in a parallel level fails, its downstream should be skipped."""
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    n1 = graph.add_node("agent-a", {"task": "a"})
    n2 = graph.add_node("agent-b", {"task": "b"})
    n3 = graph.add_node("agent-c", {"task": "c"})  # depends on n2

    graph.add_edge(n1, n3)  # n3 depends on n1
    graph.add_edge(n2, n3)  # n3 also depends on n2

    call_count = 0

    async def sometimes_fail(input_data: dict) -> dict:
        nonlocal call_count
        call_count += 1
        task = input_data.get("task", "")
        if task == "b":
            raise RuntimeError("agent-b failed")
        return {"result": task}

    mock_agent = AsyncMock()
    mock_agent.run = sometimes_fail

    with patch("solvyn.agents.graph.agent_registry.create", return_value=mock_agent):
        result = await graph.execute("s1", "t1")

    assert result.status == "failed"
    # n3 should be skipped because n2 failed
    n3_id = [n.id for n in result.nodes if n.agent_type == "agent-c"][0]
    assert graph.nodes[n3_id].status == "skipped"
    assert "dependency failed" in (graph.nodes[n3_id].error or "").lower()


@pytest.mark.asyncio
async def test_agent_graph_compute_levels() -> None:
    """Verify topological level computation."""
    from solvyn.agents.graph import AgentGraph

    graph = AgentGraph()
    # Level 0: a, b
    # Level 1: c (depends on a, b)
    # Level 2: d (depends on c)
    a = graph.add_node("a")
    b = graph.add_node("b")
    c = graph.add_node("c")
    d = graph.add_node("d")
    graph.add_edge(a, c)
    graph.add_edge(b, c)
    graph.add_edge(c, d)

    levels = graph._compute_levels()
    assert len(levels) == 3
    assert set(levels[0]) == {a, b}  # level 0: a and b
    assert levels[1] == [c]  # level 1: c
    assert levels[2] == [d]  # level 2: d
