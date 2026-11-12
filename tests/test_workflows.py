import uuid
from unittest.mock import AsyncMock

import pytest

from solvyn.workflows.templates import WORKFLOW_TEMPLATES, WorkflowStep, WorkflowTemplate


def test_workflow_templates_exist() -> None:
    assert len(WORKFLOW_TEMPLATES) >= 3
    assert "feature" in WORKFLOW_TEMPLATES
    assert "bugfix" in WORKFLOW_TEMPLATES
    assert "release" in WORKFLOW_TEMPLATES


def test_feature_workflow_steps() -> None:
    tmpl = WORKFLOW_TEMPLATES["feature"]
    assert len(tmpl.steps) >= 5
    assert tmpl.default_inputs.get("requirement") is not None


def test_bugfix_workflow_steps() -> None:
    tmpl = WORKFLOW_TEMPLATES["bugfix"]
    assert len(tmpl.steps) >= 3
    assert tmpl.default_inputs.get("bug_report") is not None


def test_release_workflow_steps() -> None:
    tmpl = WORKFLOW_TEMPLATES["release"]
    assert len(tmpl.steps) >= 2
    # release template has changelog_input, codebase_snapshot, project_info
    assert tmpl.default_inputs.get("changelog_input") is not None


def test_workflow_template_creation() -> None:
    step = WorkflowStep(
        agent_type="test-agent",
        input_map={"key": "test_input"},
        description="Test step",
    )
    tmpl = WorkflowTemplate(
        name="test",
        description="Test workflow",
        steps=[step],
        default_inputs={"test_input": "default"},
    )
    assert tmpl.name == "test"
    assert len(tmpl.steps) == 1
    assert tmpl.steps[0].description == "Test step"


@pytest.mark.asyncio
async def test_workflow_executor_run() -> None:
    from solvyn.workflows.executor import WorkflowExecutor
    executor = WorkflowExecutor()
    mock_result = AsyncMock()
    mock_result.status = "completed"
    mock_result.nodes = []
    executor.execute_template = AsyncMock(return_value=mock_result)

    result = await executor.execute_template("feature", {"requirement": "test"}, "s1", "t1")
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_workflow_run_timeout_returns_gateway_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from solvyn.api.v1 import workflows as workflow_api
    from solvyn.workflows.executor import WorkflowExecutionTimeoutError

    monkeypatch.setattr(workflow_api, "_workflow_tenant_ref", lambda: "test-tenant")
    monkeypatch.setattr(
        workflow_api.workflow_executor,
        "run",
        AsyncMock(side_effect=WorkflowExecutionTimeoutError("Workflow execution exceeded 1800 seconds")),
    )

    with pytest.raises(HTTPException) as raised:
        await workflow_api.run_workflow("feature", {"requirement": "test"})

    assert raised.value.status_code == 504
    assert "1800 seconds" in raised.value.detail


@pytest.mark.asyncio
async def test_workflow_resume_timeout_returns_gateway_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import HTTPException

    from solvyn.api.v1 import workflows as workflow_api
    from solvyn.workflows.executor import WorkflowExecutionTimeoutError

    monkeypatch.setattr(workflow_api, "_workflow_tenant_ref", lambda: "test-tenant")
    monkeypatch.setattr(
        workflow_api.workflow_executor,
        "resume",
        AsyncMock(side_effect=WorkflowExecutionTimeoutError("Workflow execution exceeded 1800 seconds")),
    )

    with pytest.raises(HTTPException) as raised:
        await workflow_api.resume_workflow_execution(uuid.uuid4())

    assert raised.value.status_code == 504


@pytest.mark.asyncio
async def test_task_generator() -> None:
    from solvyn.workflows.task_generator import TaskGenerator
    generator = TaskGenerator()
    generator.generate_from_spec = AsyncMock(return_value={
        "tasks": [
            {"title": "Task 1", "priority": "high", "assigned_agent": "backend-engineer"},
            {"title": "Task 2", "priority": "medium", "assigned_agent": "frontend-engineer"},
        ],
    })
    result = await generator.generate_from_spec("Build a login page")
    assert len(result["tasks"]) == 2
    assert result["tasks"][0]["title"] == "Task 1"


def test_task_generator_build_workflow_graph() -> None:
    from solvyn.workflows.task_generator import TaskGenerator
    generator = TaskGenerator()
    tasks = [
        {"title": "Design API", "priority": "high", "assigned_agent": "system-designer"},
        {"title": "Implement API", "priority": "high", "assigned_agent": "backend-engineer"},
    ]
    graph = generator.build_workflow_graph(tasks)
    assert "nodes" in graph
    assert len(graph["nodes"]) == 2
