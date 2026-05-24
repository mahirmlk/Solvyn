from solvyn.agents.schema import (
    AgentCapability,
    AgentMessage,
    AgentMetadata,
    AgentStatus,
    Checkpoint,
    ExecutionEdge,
    ExecutionGraph,
    ExecutionNode,
)


def test_agent_status_values() -> None:
    assert AgentStatus.IDLE == "idle"
    assert AgentStatus.RUNNING == "running"
    assert AgentStatus.COMPLETED == "completed"
    assert AgentStatus.FAILED == "failed"
    assert AgentStatus.PAUSED == "paused"


def test_agent_capability_values() -> None:
    assert AgentCapability.CODE_GENERATION == "code_generation"
    assert AgentCapability.CODE_REVIEW == "code_review"
    assert AgentCapability.SYSTEM_DESIGN == "system_design"
    assert AgentCapability.PRODUCT_MANAGEMENT == "product_management"
    assert AgentCapability.TESTING == "testing"
    assert AgentCapability.SECURITY == "security"
    assert AgentCapability.DEVOPS == "devops"
    assert AgentCapability.DOCUMENTATION == "documentation"


def test_agent_metadata() -> None:
    m = AgentMetadata(
        agent_type="test-agent",
        name="Test Agent",
        description="A test agent",
        capabilities=[AgentCapability.TESTING, AgentCapability.CODE_REVIEW],
        version="1.0.0",
    )
    assert m.agent_type == "test-agent"
    assert m.name == "Test Agent"
    assert len(m.capabilities) == 2


def test_agent_message() -> None:
    msg = AgentMessage(
        id="m1", from_agent="a", to_agent="b", session_id="s1",
        tenant_id="t1", type="request", content={"text": "hello"},
    )
    assert msg.id == "m1"
    assert msg.content["text"] == "hello"
    assert msg.correlation_id is None


def test_checkpoint(sample_checkpoint: Checkpoint) -> None:
    assert sample_checkpoint.id == "cp-1"
    assert sample_checkpoint.state["step"] == "done"
    assert sample_checkpoint.status == "completed"


def test_execution_node() -> None:
    node = ExecutionNode(id="n1", agent_type="test-agent", status="pending", input={"task": "x"})
    assert node.id == "n1"
    assert node.status == "pending"
    assert node.input == {"task": "x"}
    assert node.error is None


def test_execution_edge() -> None:
    edge = ExecutionEdge(source="n1", target="n2", condition="completed")
    assert edge.source == "n1"
    assert edge.target == "n2"
    assert edge.condition == "completed"


def test_execution_edge_defaults() -> None:
    edge = ExecutionEdge(source="a", target="b")
    assert edge.condition is None


def test_execution_graph() -> None:
    node = ExecutionNode(id="n1", agent_type="a", status="pending")
    edge = ExecutionEdge(source="n1", target="n2")
    graph = ExecutionGraph(id="g1", session_id="s1", nodes=[node], edges=[edge], status="running")
    assert graph.id == "g1"
    assert len(graph.nodes) == 1
    assert len(graph.edges) == 1
    assert graph.status == "running"
    assert graph.created_at is not None
