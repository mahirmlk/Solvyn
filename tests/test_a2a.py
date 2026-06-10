from __future__ import annotations

import pytest

from solvyn.a2a.protocol import (
    A2AEnvelope,
    A2AMessageType,
    A2AResponse,
    ErrorReport,
    StatusUpdate,
    TaskDelegation,
    TaskRequest,
    TaskResponse,
    TaskStatus,
)


class TestA2AProtocol:
    def test_envelope_creation(self) -> None:
        env = A2AEnvelope(from_agent="pm", to_agent="dev", session_id="s1")
        assert env.from_agent == "pm"
        assert env.to_agent == "dev"
        assert env.session_id == "s1"
        assert env.version == "1.0.0"

    def test_envelope_to_dict(self) -> None:
        env = A2AEnvelope(from_agent="a", to_agent="b")
        d = env.to_dict()
        assert "message_id" in d
        assert d["from_agent"] == "a"
        assert d["message_type"] == A2AMessageType.TASK_REQUEST

    def test_envelope_from_dict(self) -> None:
        data = {"from_agent": "x", "to_agent": "y", "session_id": "s2", "version": "2.0.0"}
        env = A2AEnvelope.from_dict(data)
        assert env.from_agent == "x"
        assert env.version == "2.0.0"

    def test_task_request(self) -> None:
        req = TaskRequest(task_type="code_gen", input_data={"spec": "build api"})
        d = req.to_dict()
        assert d["task_type"] == "code_gen"
        assert d["input_data"]["spec"] == "build api"

    def test_task_request_from_dict(self) -> None:
        data = {"task_type": "review", "priority": 5}
        req = TaskRequest.from_dict(data)
        assert req.task_type == "review"
        assert req.priority == 5

    def test_task_response(self) -> None:
        resp = TaskResponse(task_id="t1", status=TaskStatus.COMPLETED, output_data={"result": "done"})
        d = resp.to_dict()
        assert d["status"] == TaskStatus.COMPLETED
        assert d["output_data"]["result"] == "done"

    def test_task_response_from_dict(self) -> None:
        data = {"task_id": "t2", "status": "failed", "error": "timeout"}
        resp = TaskResponse.from_dict(data)
        assert resp.status == TaskStatus.FAILED
        assert resp.error == "timeout"

    def test_status_update(self) -> None:
        update = StatusUpdate(task_id="t1", progress=0.5, message="halfway")
        d = update.to_dict()
        assert d["progress"] == 0.5
        assert d["message"] == "halfway"

    def test_error_report(self) -> None:
        report = ErrorReport(task_id="t1", error_type="timeout", message="took too long", retryable=True)
        d = report.to_dict()
        assert d["retryable"] is True
        assert d["error_type"] == "timeout"

    def test_task_delegation(self) -> None:
        delegation = TaskDelegation(parent_task_id="p1", delegated_to="dev", delegated_from="pm")
        d = delegation.to_dict()
        assert d["delegated_to"] == "dev"
        assert d["delegated_from"] == "pm"

    def test_a2a_response(self) -> None:
        resp = A2AResponse(success=True, envelope=A2AEnvelope(), payload={"result": "ok"})
        d = resp.to_dict()
        assert d["success"] is True
        assert "envelope" in d

    def test_task_status_enum(self) -> None:
        assert TaskStatus.PENDING == "pending"
        assert TaskStatus.RUNNING == "running"
        assert TaskStatus.COMPLETED == "completed"
        assert TaskStatus.FAILED == "failed"
        assert TaskStatus.CANCELLED == "cancelled"
