SetValueInvocationException: 
Line |
   3 |  $RawUI.CursorPosition = @{X=0;Y=0}
     |  ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
     | Exception setting "CursorPosition": "The handle is invalid."
"""Credential-free acceptance of repository-backed teammate delivery.

The test keeps the provider boundary deterministic, but exercises the real
conversation, handoff, workspace tool loop, Docker verifier, completion gate,
and draft-PR orchestration against the CI PostgreSQL database.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_repository_task_runs_approved_tool_loop_in_docker_and_delivers_draft_pr(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Prove the complete repository task path with no real provider credentials."""
    from sqlalchemy import select

    from solvyn.agents import teammate_runtime
    from solvyn.api.v1 import teammates as teammates_api
    from solvyn.api.v1 import verification as verification_api
    from solvyn.config import settings
    from solvyn.db.models import (
        Approval,
        Organization,
        OrganizationMembership,
        PermissionLevel,
        Project,
        Repository,
        TeammatePermission,
        TeammateProfile,
        TeammateProjectAssignment,
        Tenant,
        User,
        VerificationRun,
    )
    from solvyn.db.session import async_session_factory, bind_tenant_scope
    from solvyn.identity.security import create_access_token
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router
    from solvyn.sandbox.runtime import DockerSandboxRunner
    from solvyn.sandbox.workspaces import WorkspaceManager
    from solvyn.workflows import scheduler, task_completion

    workspace_root = tmp_path / "repository-workspaces"
    monkeypatch.setattr(settings, "sandbox_workspace_root", str(workspace_root))
    monkeypatch.setattr(teammates_api.settings, "sandbox_workspace_root", str(workspace_root))
    monkeypatch.setattr(verification_api.settings, "sandbox_workspace_root", str(workspace_root))
    monkeypatch.setattr(task_completion.settings, "sandbox_workspace_root", str(workspace_root))

    runner = DockerSandboxRunner(WorkspaceManager(workspace_root), image=settings.sandbox_image)
    ready = await runner.check_ready()
    if not ready:
        message = "Docker engine or solvyn-sandbox:local image is unavailable"
        if os.getenv("SOLVYN_REQUIRE_SANDBOX_IMAGE") == "1":
            pytest.fail(message)
        pytest.skip(message)

    tenant_id, org_id, project_id, lead_id, engineer_id, repository_id, user_id = (
        uuid.uuid4() for _ in range(7)
    )
    source_sha = "a" * 40
    tree_sha = "b" * 40
    original = "def health():\n    return {'status': 'ok'}\n"
    test_source = "from src.health import health\n\n\ndef test_health_is_ready():\n    assert health() == {'status': 'ready'}\n"
    repository_files = {"src/health.py": original, "tests/test_health.py": test_source}

    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        tenant_id = tenant.id
        await bind_tenant_scope(db, tenant.id)
        db.add_all(
            [
                Organization(
                    id=org_id,
                    tenant_id=tenant.id,
                    name="Workflow acceptance",
                    slug=f"workflow-acceptance-{uuid.uuid4().hex[:10]}",
                ),
                Project(
                    id=project_id,
                    tenant_id=tenant.id,
                    organization_id=org_id,
                    name="Health API",
                    slug=f"health-api-{uuid.uuid4().hex[:10]}",
                ),
                TeammateProfile(
                    id=lead_id,
                    tenant_id=tenant.id,
                    organization_id=org_id,
                    name="Tech Lead",
                    role="Tech Lead",
                    agent_type="system-designer",
                ),
                TeammateProfile(
                    id=engineer_id,
                    tenant_id=tenant.id,
                    organization_id=org_id,
                    name="Software Engineer",
                    role="Software Engineer",
                    agent_type="backend-engineer",
                ),
            ]
        )
        await db.flush()
        await db.commit()
        # The repository insert RLS policy checks for its project in a fresh
        # tenant-bound transaction.
        await bind_tenant_scope(db, tenant.id)
        db.add_all(
            [
                Repository(
                    id=repository_id,
                    project_id=project_id,
                    provider="github",
                    full_name="solvyn/workflow-acceptance",
                    default_branch="main",
                    url="https://github.com/solvyn/workflow-acceptance",
                ),
                TeammateProjectAssignment(teammate_id=lead_id, project_id=project_id),
                TeammateProjectAssignment(teammate_id=engineer_id, project_id=project_id),
                TeammatePermission(teammate_id=engineer_id, permission=PermissionLevel.READ_ONLY.value, granted=True),
                TeammatePermission(teammate_id=engineer_id, permission=PermissionLevel.MODIFY_FILES.value, granted=True),
                TeammatePermission(teammate_id=engineer_id, permission=PermissionLevel.EXECUTE_COMMANDS.value, granted=True),
                User(id=user_id, email=f"workflow-owner-{uuid.uuid4().hex[:10]}@example.com", password_hash="test-hash"),
                OrganizationMembership(user_id=user_id, organization_id=org_id, role="owner"),
            ]
        )
        await db.commit()

    client.headers["Authorization"] = f"Bearer {create_access_token(subject=str(user_id))}"
    client.headers["X-Tenant-Slug"] = "test-tenant"

    class FakeRepositoryProvider:
        async def __aenter__(self) -> FakeRepositoryProvider:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def inspect(self, _full_name: str, revision: str | None = None) -> dict[str, Any]:
            assert revision in (None, source_sha)
            return {
                "revision": revision or source_sha,
                "default_branch": "main",
                "private": False,
                "repository": {"description": "Acceptance fixture"},
                "manifests": {},
                "top_level_entries": ["src/", "tests/"],
                "languages": {"Python": 100},
            }

        @staticmethod
        def parse_dependencies(_manifests: dict[str, str]) -> dict[str, list[str]]:
            return {}

        @staticmethod
        def detect_frameworks(_dependencies: dict[str, list[str]]) -> list[str]:
            return []

        async def repository_metadata(self, _full_name: str) -> dict[str, Any]:
            return {"default_branch": "main", "private": False}

        async def branch_commit(self, _full_name: str, _branch: str) -> str:
            pytest.fail("workspace provisioning must use the confirmed commit pin")
            return source_sha

        async def commit_tree_sha(self, _full_name: str, revision: str) -> str:
            assert revision == source_sha
            return tree_sha

        async def repository_tree(self, _full_name: str, actual_tree: str) -> tuple[str, list[dict[str, Any]]]:
            assert actual_tree == tree_sha
            return tree_sha, [
                {"path": path, "type": "blob", "mode": "100644", "size": len(content.encode())}
                for path, content in repository_files.items()
            ]

        async def text_file(
            self, _full_name: str, _path: str, sha: str, *, max_bytes: int | None = None
        ) -> str:
            assert sha == source_sha
            assert max_bytes is None or max_bytes > 0
            return repository_files[_path]

    provider = FakeRepositoryProvider()
    for module in (teammates_api, teammate_runtime, verification_api):
        monkeypatch.setattr(
            module,
            "get_authorized_repository_provider",
            AsyncMock(return_value=provider),
        )

    plan_payload = {
        "title": "Return ready from health",
        "description": "Update the repository health handler to report ready.",
        "acceptance_criteria": ["The health handler reports ready."],
        "implementation_steps": ["Update the health status response."],
        "relevant_files": ["src/health.py"],
        "verification_strategy": ["Run Ruff lint in the sandbox."],
        "risks": [],
    }
    tool_calls = [
        ("workspace_list_files", {}),
        ("workspace_read_file", {"path": "src/health.py"}),
        (
            "workspace_write_file",
            {"path": "src/health.py", "content": "def health():\n    return {'status': 'ready'}\n"},
        ),
        ("workspace_run_verification", {"kind": "test"}),
    ]
    tool_cursor = 0
    generated: list[dict[str, Any]] = []

    async def fake_generate(prompt: str = "", **kwargs: Any) -> LLMResponse:
        nonlocal tool_cursor
        generated.append(kwargs)
        has_tool_runtime = isinstance(kwargs.get("tools"), list) and bool(kwargs["tools"])
        if has_tool_runtime:
            if tool_cursor >= len(tool_calls):
                return LLMResponse(
                    content="Updated the health endpoint and requested lint verification.",
                    provider="fake",
                    model="acceptance",
                    prompt_tokens=2,
                    completion_tokens=3,
                    total_tokens=5,
                    cost=0,
                )
            name, arguments = tool_calls[tool_cursor]
            tool_cursor += 1
            call = {
                "id": f"acceptance-tool-{tool_cursor}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            return LLMResponse(
                content="",
                provider="fake",
                model="acceptance",
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
                cost=0,
                tool_calls=[call],
            )
        if '"mode": "task_plan"' in prompt or "Draft an implementation plan" in prompt:
            content = json.dumps(plan_payload)
        else:
            content = "I will update the health response and check it in the isolated workspace."
        return LLMResponse(
            content=content,
            provider="fake",
            model="acceptance",
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
            cost=0,
        )

    monkeypatch.setattr(llm_router, "generate", fake_generate)

    conversation = await client.post(
        f"/api/v1/teammates/{lead_id}/conversations", json={"project_id": str(project_id)}
    )
    assert conversation.status_code == 201, conversation.text
    conversation_id = conversation.json()["id"]
    message = await client.post(
        f"/api/v1/teammates/{lead_id}/conversations/{conversation_id}/messages",
        json={"content": "Change the health status to ready and verify the repository."},
    )
    assert message.status_code == 200, message.text
    plan = await client.post(
        f"/api/v1/teammates/{lead_id}/conversations/{conversation_id}/plan",
        json={"repository_id": str(repository_id), "repository_paths": list(repository_files)},
    )
    assert plan.status_code == 200, plan.text
    assert plan.json()["repository_revision"] == source_sha

    confirmed = await client.post(
        f"/api/v1/teammates/{lead_id}/conversations/{conversation_id}/task",
        json={
            "confirmed": True,
            "title": plan_payload["title"],
            "description": plan_payload["description"],
            "acceptance_criteria": plan_payload["acceptance_criteria"],
            "target_teammate_id": str(engineer_id),
            "repository_id": str(repository_id),
            "repository_revision": plan.json()["repository_revision"],
            "repository_paths": list(repository_files),
            "plan": plan.json()["plan"],
        },
    )
    assert confirmed.status_code == 201, confirmed.text
    task_state = confirmed.json()["task"]
    execution_id = uuid.UUID(task_state["execution_id"])
    root_task_id = uuid.UUID(task_state["root_task_id"])
    child_task_id = uuid.UUID(task_state["task_id"])
    handoff_id = uuid.UUID(task_state["handoff_id"])
    assert task_state["handoff_status"] == "waiting_approval"
    approval_id = task_state["approval_id"]
    approval = await client.post(
        f"/api/v1/approvals/{approval_id}/approve", json={"note": "Approved the confirmed repository task."}
    )
    assert approval.status_code == 200, approval.text

    workspace = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{child_task_id}/workspace",
        json={
            "repository_id": str(repository_id),
            "repository_paths": list(repository_files),
            "source_commit_sha": source_sha,
        },
    )
    assert workspace.status_code == 201, workspace.text
    workspace_manager = WorkspaceManager(workspace_root)
    managed = workspace_manager.get(uuid.UUID(workspace.json()["workspace_id"]))
    resumed = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{root_task_id}/handoffs/{handoff_id}/resume"
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed"
    assert tool_cursor == len(tool_calls), "The actual workspace tool loop must call list/read/write/lint."
    assert len(generated) >= 7
    assert workspace_manager.read_file(managed, "src/health.py").decode() == (
        "def health():\n    return {'status': 'ready'}\n"
    )

    async with async_session_factory() as db:
        await bind_tenant_scope(db, tenant_id)
        run = (
            await db.execute(
                select(VerificationRun).where(
                    VerificationRun.tenant_id == tenant_id,
                    VerificationRun.task_id == child_task_id,
                )
            )
        ).scalar_one()
        verify_approval = (
            await db.execute(select(Approval).where(Approval.verification_run_id == run.id))
        ).scalar_one()
        run_id, verify_approval_id = run.id, verify_approval.id
    approved_verification = await client.post(
        f"/api/v1/approvals/{verify_approval_id}/approve",
        json={"note": "Owner approved the sandbox lint run."},
    )
    assert approved_verification.status_code == 200, approved_verification.text
    digest_before_test = workspace_manager.fingerprint(managed)
    async with async_session_factory() as db:
        due = await scheduler._fetch_due_verification_runs(db)
    assert (run_id, tenant_id) in due
    await scheduler._execute_due_verification(run_id, tenant_id)

    criteria = await client.get(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{child_task_id}/acceptance-criteria"
    )
    assert criteria.status_code == 200, criteria.text
    criterion_id = criteria.json()["acceptance_criteria"][0]["id"]
    decision = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{child_task_id}/acceptance-criteria/{criterion_id}/decision",
        json={"decision": "accepted", "note": "The changed response matches the confirmed criterion."},
    )
    assert decision.status_code == 200, decision.text
    current_digest = workspace_manager.fingerprint(managed)
    assert current_digest == digest_before_test, "Sandbox pytest artifacts must not change approved workspace content."
    assert decision.json()["decision_workspace_digest"] == current_digest

    runs = await client.get(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{child_task_id}/verification-runs"
    )
    assert runs.status_code == 200, runs.text
    run_evidence = next(item for item in runs.json()["verification_runs"] if item["id"] == str(run_id))
    assert run_evidence["status"] == "passed", run_evidence
    assert run_evidence["kind"] == "test"
    assert run_evidence["source_digest"] == current_digest
    task_rows = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    child = next(row for row in task_rows.json()["tasks"] if row["task_id"] == str(child_task_id))
    assert child["status"] == "completed", child

    class FakeGitHub:
        def __init__(self, *, token: str) -> None:
            assert token == "test-github-token"
            self.calls: list[str] = []
            self.committed_files: list[dict[str, str]] = []
            self.pull_request: dict[str, Any] | None = None

        async def __aenter__(self) -> FakeGitHub:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        async def repository_metadata(self, _full_name: str) -> dict[str, Any]:
            return {"default_branch": "main", "private": False}

        async def commit_tree_sha(self, _full_name: str, revision: str) -> str:
            assert revision == source_sha
            return tree_sha

        async def repository_tree(self, _full_name: str, actual_tree: str) -> tuple[str, list[dict[str, Any]]]:
            assert actual_tree == tree_sha
            entries = []
            for path, content in repository_files.items():
                raw = content.encode()
                blob = hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
                entries.append({"path": path, "type": "blob", "mode": "100644", "size": len(raw), "sha": blob})
            return tree_sha, entries

        async def text_file(self, _full_name: str, path: str, revision: str, *, max_bytes: int) -> str:
            assert revision == source_sha and max_bytes > 0
            return repository_files[path]

        async def create_branch_at_commit(self, _full_name: str, name: str, sha: str) -> dict[str, str]:
            self.calls.append("branch")
            assert sha == source_sha
            return {"name": name, "sha": "c" * 40}

        async def commit_files(
            self,
            _full_name: str,
            _branch: str,
            *,
            expected_head_sha: str,
            message: str,
            files: list[dict[str, str]],
            deleted_paths: list[str],
        ) -> dict[str, str]:
            self.calls.append("commit")
            assert expected_head_sha == "c" * 40 and message
            assert deleted_paths == []
            self.committed_files = files
            assert files == [
                {"path": "src/health.py", "content": "def health():\n    return {'status': 'ready'}\n"}
            ]
            return {"sha": "d" * 40}

        async def create_pull_request(
            self, _full_name: str, *, title: str, body: str, head: str, base: str
        ) -> dict[str, Any]:
            self.calls.append("pull_request")
            assert title and body and head.startswith("solvyn/") and base == "main"
            self.pull_request = {
                "number": 1,
                "title": title,
                "url": "https://github.com/solvyn/workflow-acceptance/pull/1",
                "state": "open",
                "draft": True,
                "merged": False,
            }
            return self.pull_request

    fake_github = FakeGitHub(token="test-github-token")
    monkeypatch.setattr(verification_api, "load_organization_github_token", AsyncMock(return_value="test-github-token"))
    monkeypatch.setattr(verification_api, "GitHubClient", lambda *, token: fake_github)
    preview = await client.get(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{child_task_id}/delivery-preview"
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["can_deliver"] is True
    delivered = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{child_task_id}/deliver",
        json={
            "expected_diff_digest": preview.json()["diff_digest"],
            "branch_name": "solvyn/health-ready",
            "commit_message": "Report ready from health handler",
            "pull_request_title": "Report ready from health handler",
            "pull_request_body": "Updates the repository health response after approved sandbox lint.",
        },
    )
    assert delivered.status_code == 201, delivered.text
    assert fake_github.calls == ["branch", "commit", "pull_request"]
    assert delivered.json()["pull_request"]["draft"] is True
    assert delivered.json()["merged"] is False

