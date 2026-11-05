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
        self.interrupt_before_completion = False

    async def execute(self, statement):
        sql = str(statement)
        if "UPDATE verification_runs" in sql and self.interrupt_before_completion:
            self.records[1].status = "interrupted"
            return _Result(None)
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
        if getattr(record, "id", None) is None:
            record.id = uuid.uuid4()
        self.records.append(record)

    async def commit(self) -> None:
        return None

    async def refresh(self, _record) -> None:
        return None

    async def rollback(self) -> None:
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


@pytest.mark.asyncio
async def test_verification_tool_uses_fixed_profile_and_persists_evidence(tmp_path: Path, monkeypatch) -> None:
    from solvyn.agents import workspace_tools
    from solvyn.sandbox.runtime import SandboxResult

    runtime, session, _manager, workspace = _runtime(tmp_path, {"EXECUTE_COMMANDS"})

    class _Runner:
        async def run(self, received_workspace, command):
            assert received_workspace.id == workspace.id
            assert tuple(command) == ("pytest", "-q")
            return SandboxResult(
                run_id=uuid.uuid4(),
                command=("pytest", "-q"),
                exit_code=0,
                timed_out=False,
                stdout="2 passed",
                stderr="",
                output_truncated=False,
                duration_seconds=0.5,
            )

    monkeypatch.setattr(workspace_tools, "create_sandbox_runner", lambda _manager: _Runner())

    result = await runtime._invoke(
        {
            "id": "call-5",
            "function": {
                "name": "workspace_run_verification",
                "arguments": '{"kind":"test"}',
            },
        }
    )

    assert result["kind"] == "test"
    assert result["status"] == "passed"
    assert result["stdout"] == "2 passed"
    assert session.records[0].status == "succeeded"
    verification = session.records[1]
    assert verification.status == "passed"
    assert verification.command == '["pytest", "-q"]'
    assert verification.source_digest is not None


@pytest.mark.asyncio
async def test_verification_tool_requires_execute_commands_grant(tmp_path: Path) -> None:
    runtime, session, _manager, _workspace = _runtime(tmp_path, {"READ_ONLY"})

    result = await runtime._invoke(
        {
            "id": "call-6",
            "function": {
                "name": "workspace_run_verification",
                "arguments": '{"kind":"test"}',
            },
        }
    )

    assert result == {"error": "permission_denied"}
    assert len(session.records) == 1
    assert session.records[0].status == "denied"
    available = {tool["function"]["name"] for tool in runtime._tool_definitions({"READ_ONLY"})}
    assert "workspace_run_verification" not in available


@pytest.mark.asyncio
async def test_late_verification_result_does_not_overwrite_interrupted_run(tmp_path: Path, monkeypatch) -> None:
    from solvyn.agents import workspace_tools
    from solvyn.sandbox.runtime import SandboxResult

    runtime, session, _manager, workspace = _runtime(tmp_path, {"EXECUTE_COMMANDS"})
    session.interrupt_before_completion = True

    class _Runner:
        async def run(self, _received_workspace, command):
            return SandboxResult(
                run_id=uuid.uuid4(),
                command=tuple(command),
                exit_code=0,
                timed_out=False,
                stdout="late result",
                stderr="",
                output_truncated=False,
                duration_seconds=1.0,
            )

    monkeypatch.setattr(workspace_tools, "create_sandbox_runner", lambda _manager: _Runner())

    result = await runtime._invoke(
        {
            "id": "call-7",
            "function": {
                "name": "workspace_run_verification",
                "arguments": '{"kind":"test"}',
            },
        }
    )

    assert result["status"] == "interrupted"
    assert result["error_code"] == "verification_run_already_terminal"
    assert session.records[1].status == "interrupted"
