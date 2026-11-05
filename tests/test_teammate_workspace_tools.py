from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalars(self):
        return SimpleNamespace(all=lambda: self.value)


class _Session:
    def __init__(self, granted: set[str]) -> None:
        self.granted = granted
        self.records = []

    async def execute(self, statement):
        sql = str(statement)
        if "teammate_permissions" in sql:
            parameters = statement.compile().params
            values = [
                item
                for value in parameters.values()
                for item in (value if isinstance(value, (list, tuple)) else [value])
                if isinstance(item, str)
            ]
            if " IN " in sql and values:
                return _Result(list(self.granted))
            required = next((value for value in values if value in {"READ_ONLY", "MODIFY_FILES"}), None)
            return _Result(uuid.uuid4() if required in self.granted else None)
        return _Result(uuid.uuid4())

    def add(self, record) -> None:
        self.records.append(record)

    async def commit(self) -> None:
        return None

    async def refresh(self, _record) -> None:
        return None


def _runtime(tmp_path: Path, granted: set[str]):
    from solvyn.agents.workspace_tools import TeammateWorkspaceToolRuntime
    from solvyn.sandbox.workspaces import WorkspaceManager

    session = _Session(granted)
    manager = WorkspaceManager(tmp_path / "workspaces")
    workspace = manager.create()
    runtime = TeammateWorkspaceToolRuntime(
        session,
        manager,
        tenant_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        workspace_id=workspace.id,
        teammate_id=uuid.uuid4(),
        handoff_lease_id=uuid.uuid4(),
    )
    return runtime, session, manager, workspace


@pytest.mark.asyncio
async def test_workspace_write_is_scoped_and_audited_without_file_contents(tmp_path: Path) -> None:
    runtime, session, manager, workspace = _runtime(tmp_path, {"READ_ONLY", "MODIFY_FILES"})

    result = await runtime._invoke(
        {
            "id": "call-1",
            "function": {
                "name": "workspace_write_file",
                "arguments": '{"path":"src/main.py","content":"print(1)"}',
            },
        }
    )

    assert result == {"path": "src/main.py", "bytes_written": 8}
    assert manager.read_file(workspace, "src/main.py") == b"print(1)"
    record = session.records[0]
    assert record.status == "succeeded"
    assert record.path == "src/main.py"
    assert record.input_bytes == 8
    assert not hasattr(record, "content")


@pytest.mark.asyncio
async def test_workspace_write_requires_modify_permission(tmp_path: Path) -> None:
    runtime, session, manager, workspace = _runtime(tmp_path, {"READ_ONLY"})

    result = await runtime._invoke(
        {
            "id": "call-2",
            "function": {
                "name": "workspace_write_file",
                "arguments": '{"path":"main.py","content":"changed"}',
            },
        }
    )

    assert result == {"error": "permission_denied"}
    assert not (workspace.path / "main.py").exists()
    assert session.records[0].status == "denied"
    assert manager.list_files(workspace) == []


@pytest.mark.asyncio
async def test_workspace_path_traversal_is_rejected_and_audited(tmp_path: Path) -> None:
    runtime, session, _manager, _workspace = _runtime(tmp_path, {"READ_ONLY"})

    result = await runtime._invoke(
        {
            "id": "call-3",
            "function": {
                "name": "workspace_read_file",
                "arguments": '{"path":"../secret.txt"}',
            },
        }
    )

    assert result == {"error": "unsafe_or_unavailable_path"}
    assert session.records[0].status == "failed"


@pytest.mark.asyncio
async def test_workspace_list_filters_git_files_before_applying_limit(tmp_path: Path) -> None:
    runtime, _session, manager, workspace = _runtime(tmp_path, {"READ_ONLY"})
    manager.write_file(workspace, ".git/config", b"internal")
    manager.write_file(workspace, "a.py", b"a")
    manager.write_file(workspace, "b.py", b"b")

    result = await runtime._dispatch("workspace_list_files", {"limit": 1})

    assert result == {"files": ["a.py"], "truncated": True}


@pytest.mark.asyncio
async def test_tool_loop_returns_model_result_after_audited_file_write(tmp_path: Path) -> None:
    from solvyn.llm.providers.base import LLMResponse

    runtime, session, manager, workspace = _runtime(tmp_path, {"READ_ONLY", "MODIFY_FILES"})
    responses = [
        LLMResponse(
            "", "test", "test", 10, 4, 14, 0,
            tool_calls=[
                {
                    "id": "call-4",
                    "type": "function",
                    "function": {
                        "name": "workspace_write_file",
                        "arguments": '{"path":"result.txt","content":"done"}',
                    },
                }
            ],
        ),
        LLMResponse("The file is ready.", "test", "test", 12, 5, 17, 0),
    ]
    requests = []

    async def generate_once(prompt, system_prompt, **kwargs):
        requests.append((prompt, system_prompt, kwargs))
        return responses.pop(0)

    result = await runtime.run(generate_once, "Create result.txt", "System", {})

    assert result.content == "The file is ready."
    assert result.prompt_tokens == 22
    assert result.completion_tokens == 9
    assert result.tool_calls == []
    assert manager.read_file(workspace, "result.txt") == b"done"
    assert session.records[0].status == "succeeded"
    assert requests[0][2]["use_cache"] is False
    assert requests[1][2]["messages"][-1]["role"] == "tool"
