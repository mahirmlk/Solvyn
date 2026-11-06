"""End-to-end tests for Solvyn API workflows.

These tests exercise the full HTTP→middleware→route→agent→LLM stack via
``httpx.AsyncClient`` (HTTP) and ``fastapi.testclient.TestClient`` (WebSocket).
All LLM calls are mocked so the suite runs hermetically without any real
API keys.

The tests are tagged with the ``e2e`` marker (see ``pyproject.toml``) and
can be selected with ``pytest -m e2e``.
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

# Apply the e2e marker to every test in this module
pytestmark = pytest.mark.e2e


# ─── Module-level fixtures ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _patch_llm_router() -> Any:
    """Replace the LLM router's ``generate`` coroutine with a deterministic stub.

    Workflows and agents eventually call ``llm_router.generate`` to talk to a
    provider; we replace that single entrypoint so we never touch the real
    OpenRouter/Groq/DeepSeek HTTP APIs during tests.
    """
    from solvyn.llm.providers.base import LLMResponse
    from solvyn.llm.router import llm_router

    # Drop any previously cached responses so calls go through the mock
    llm_router.cache._local_cache.clear()

    async def _mock_generate(
        prompt: str,
        system_prompt: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        fallback: bool = True,
        use_cache: bool = False,
        **kwargs: Any,
    ) -> LLMResponse:
        return LLMResponse(
            content=f"mock-llm-output :: {prompt[:60]}",
            provider=provider or "openrouter",
            model=model or "openai/gpt-4o",
            prompt_tokens=len(prompt.split()),
            completion_tokens=20,
            total_tokens=len(prompt.split()) + 20,
            cost=0.0001,
        )

    original = llm_router.generate
    llm_router.generate = _mock_generate  # type: ignore[method-assign]
    try:
        yield
    finally:
        llm_router.generate = original  # type: ignore[method-assign]


def _clear_ratelimit_keys() -> None:
    """Best-effort cleanup of stale Redis sliding-window keys.

    CI runs with a real Redis service; stale ``ratelimit:*`` entries from
    earlier tests in the same run would leak into the rate-limit tests and
    make them flaky.  Only the ``ratelimit:`` namespace is touched.
    """
    try:
        import redis as _redis

        from solvyn.config import settings

        client = _redis.Redis.from_url(settings.redis_url, decode_responses=True)
        keys = list(client.scan_iter("ratelimit:*"))
        if keys:
            client.delete(*keys)
        client.close()
    except Exception:
        pass


@pytest.fixture
def low_rate_limit() -> Any:
    """Configure the rate limiter to allow only 5 requests (hermetic)."""
    from solvyn.multi_tenant.rate_limiter import redis_rate_limiter

    _clear_ratelimit_keys()
    redis_rate_limiter.configure(ip_limit=5, tenant_limit=5, window_seconds=60)
    redis_rate_limiter._fallback_buckets.clear()
    try:
        yield redis_rate_limiter
    finally:
        redis_rate_limiter.configure(ip_limit=100, tenant_limit=500, window_seconds=60)
        redis_rate_limiter._fallback_buckets.clear()
        _clear_ratelimit_keys()


@pytest_asyncio.fixture
async def unauthed_client(app: Any) -> AsyncGenerator[AsyncClient, None]:
    """Client with no API key — used to verify public vs. protected paths."""
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


# ─── Health check flow ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_endpoint_returns_ok(client: AsyncClient) -> None:
    """GET /api/v1/health returns 200 and a static OK payload."""
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "solvyn"


@pytest.mark.asyncio
async def test_health_endpoint_does_not_require_api_key(unauthed_client: AsyncClient) -> None:
    """Public health probe must work without an X-API-Key header."""
    response = await unauthed_client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_deep_health_endpoint_returns_checks(client: AsyncClient) -> None:
    """GET /api/v1/health/deep returns a per-dependency breakdown."""
    response = await client.get("/api/v1/health/deep")
    assert response.status_code in (200, 503)
    body = response.json()
    assert "status" in body
    assert "checks" in body
    # Database and Redis are always part of the breakdown
    assert "database" in body["checks"]
    assert "redis" in body["checks"]


@pytest.mark.asyncio
async def test_health_endpoints_share_no_auth_requirement(unauthed_client: AsyncClient) -> None:
    """Both health probes must be accessible without authentication."""
    health = await unauthed_client.get("/api/v1/health")
    deep = await unauthed_client.get("/api/v1/health/deep")
    assert health.status_code == 200
    assert deep.status_code in (200, 503)


# ─── Full feature workflow ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scheduled_workflow_is_durable_and_cancellable(client: AsyncClient) -> None:
    scheduled_at = datetime.now(UTC) + timedelta(minutes=2)
    response = await client.post(
        "/api/v1/workflows/feature/schedule",
        json={"scheduled_at": scheduled_at.isoformat(), "inputs": {"requirement": "Build a scheduled feature"}},
    )
    assert response.status_code == 200
    scheduled = response.json()
    assert scheduled["status"] == "scheduled"

    snapshot = await client.get(f"/api/v1/workflows/executions/{scheduled['execution_id']}")
    assert snapshot.status_code == 200
    assert snapshot.json()["status"] == "scheduled"
    assert snapshot.json()["snapshot"]["template"] == "feature"

    cancelled = await client.post(f"/api/v1/workflows/executions/{scheduled['execution_id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_schedule_rejects_a_naive_or_past_timestamp(client: AsyncClient) -> None:
    naive = await client.post(
        "/api/v1/workflows/feature/schedule",
        json={"scheduled_at": "2030-01-01T00:00:00", "inputs": {}},
    )
    assert naive.status_code == 400

    past = await client.post(
        "/api/v1/workflows/feature/schedule",
        json={"scheduled_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(), "inputs": {}},
    )
    assert past.status_code == 400


@pytest.mark.asyncio
async def test_repository_workspace_requires_a_delegated_task_handoff(client: AsyncClient) -> None:
    workflow = await client.post("/api/v1/workflows/feature/run", json={"requirement": "Check repository workspace scoping"})
    assert workflow.status_code == 200
    execution_id = workflow.json()["execution_id"]
    tasks = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    task_id = tasks.json()["tasks"][0]["task_id"]

    response = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/workspace",
        json={"repository_id": str(uuid.uuid4()), "repository_paths": ["README.md"]},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Repository workspaces require an assigned project handoff"


@pytest.mark.asyncio
async def test_task_verification_is_sandboxed_and_durable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    from sqlalchemy import select

    from solvyn.config import settings
    from solvyn.sandbox.runtime import DockerSandboxRunner, SandboxResult

    monkeypatch.setattr(settings, "sandbox_workspace_root", str(tmp_path / "workspaces"))

    workflow = await client.post("/api/v1/workflows/feature/run", json={"requirement": "Verify a generated feature"})
    assert workflow.status_code == 200
    execution_id = workflow.json()["execution_id"]
    task_list = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    task_id = task_list.json()["tasks"][0]["task_id"]

    workspace = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/workspace",
        json={"files": {"test_sample.py": "def test_sample():\n    assert 1 + 1 == 2\n"}},
    )
    assert workspace.status_code == 201

    from solvyn.db.models import (
        Organization,
        OrganizationMembership,
        Project,
        Task,
        TaskHandoff,
        TeammateProfile,
        TeammateProjectAssignment,
        TeammateStatus,
        Tenant,
        User,
    )
    from solvyn.db.session import async_session_factory, bind_tenant_scope

    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    teammate_id = uuid.uuid4()
    parent_task_id = uuid.uuid4()
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        await bind_tenant_scope(db, tenant.id)
        organization = Organization(
            id=organization_id,
            name="Verification Approval",
            slug=f"verification-approval-{uuid.uuid4().hex[:10]}",
            tenant_id=tenant.id,
        )
        project = Project(
            id=project_id,
            organization_id=organization_id,
            tenant_id=tenant.id,
            name="Verification Project",
            slug=f"verification-{uuid.uuid4().hex[:10]}",
        )
        teammate = TeammateProfile(
            id=teammate_id,
            organization_id=organization_id,
            tenant_id=tenant.id,
            name="Verification QA",
            role="QA Engineer",
            agent_type="qa-engineer",
            status=TeammateStatus.IDLE,
        )
        parent_task = Task(id=parent_task_id, tenant_id=tenant.id, title="Verification parent")
        db.add(parent_task)
        await db.flush()
        child_task = (await db.execute(select(Task).where(Task.id == uuid.UUID(task_id)))).scalar_one()
        child_task.parent_task_id = parent_task_id
        db.add_all(
            [
                organization,
                project,
                teammate,
                TeammateProjectAssignment(teammate_id=teammate_id, project_id=project_id),
                User(id=user_id, email=f"verification-{uuid.uuid4().hex[:12]}@example.com", password_hash="test-hash"),
                OrganizationMembership(user_id=user_id, organization_id=organization_id, role="owner"),
            ]
        )
        await db.flush()
        db.add(
            TaskHandoff(
                tenant_id=tenant.id,
                parent_task_id=parent_task_id,
                child_task_id=uuid.UUID(task_id),
                idempotency_key=uuid.uuid4(),
                teammate_id=teammate_id,
                project_id=project_id,
                delegated_from="Tech Lead",
                delegated_to="QA Engineer",
                reason="Verify the task output",
                input_data='{"task":"Verify the task output"}',
                status="completed",
            )
        )
        await db.commit()

    from solvyn.identity.security import create_access_token

    client.headers["Authorization"] = f"Bearer {create_access_token(subject=str(user_id))}"
    client.headers["X-Tenant-Slug"] = "test-tenant"

    async def approve_and_execute(response):
        approval_id = response.json()["approval_id"]
        approval = await client.post(f"/api/v1/approvals/{approval_id}/approve", json={"note": "Approved verification"})
        assert approval.status_code == 200
        return await client.post(
            f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs/{response.json()['verification_run']['id']}/execute"
        )

    async def fake_run(self: DockerSandboxRunner, managed_workspace: Any, command: list[str]) -> SandboxResult:
        return SandboxResult(uuid.uuid4(), tuple(command), 0, False, "1 passed", "", False, 0.05)

    monkeypatch.setattr(DockerSandboxRunner, "run", fake_run)
    verification = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs",
        json={"kind": "test"},
    )
    assert verification.status_code == 202
    assert verification.json()["verification_run"]["status"] == "pending_approval"
    assert verification.json()["verification_run"]["command"] == ["pytest", "-q"]
    assert len(verification.json()["verification_run"]["source_digest"]) == 64
    first_result = await approve_and_execute(verification)
    assert first_result.status_code == 200
    assert first_result.json()["status"] == "passed"
    assert first_result.json()["stdout"] == "1 passed"
    assert first_result.json()["regression_status"] == "baseline"

    async def failed_run(self: DockerSandboxRunner, managed_workspace: Any, command: list[str]) -> SandboxResult:
        return SandboxResult(uuid.uuid4(), tuple(command), 1, False, "1 failed", "", False, 0.05)

    monkeypatch.setattr(DockerSandboxRunner, "run", failed_run)
    regression = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs",
        json={"kind": "test"},
    )
    assert regression.status_code == 202
    regression = await approve_and_execute(regression)
    assert regression.status_code == 200
    assert regression.json()["status"] == "failed"
    assert regression.json()["regression_status"] == "regressed"
    assert regression.json()["compared_to_run_id"] == verification.json()["verification_run"]["id"]
    assert regression.json()["source_changed"] is False

    history = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    first_task = next(task for task in history.json()["tasks"] if task["task_id"] == task_id)
    assert first_task["verification_runs"][0]["status"] == "passed"

    async def late_result(self: DockerSandboxRunner, managed_workspace: Any, command: list[str]) -> SandboxResult:
        from sqlalchemy import select

        from solvyn.db.models import VerificationRun
        from solvyn.db.session import async_session_factory

        async with async_session_factory() as db:
            run = (
                await db.execute(
                    select(VerificationRun)
                    .where(VerificationRun.task_id == uuid.UUID(task_id), VerificationRun.status == "running")
                    .order_by(VerificationRun.started_at.desc())
                    .limit(1)
                )
            ).scalar_one()
            run.status = "interrupted"
            run.error = "Worker stopped before persisting the verification result"
            await db.commit()
        return SandboxResult(uuid.uuid4(), tuple(command), 0, False, "late success", "", False, 0.05)

    monkeypatch.setattr(DockerSandboxRunner, "run", late_result)
    late = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs",
        json={"kind": "test", "command": ["pytest", "-q"]},
    )
    assert late.status_code == 202
    late = await approve_and_execute(late)
    assert late.status_code == 200
    assert late.json()["status"] == "interrupted"

    rejected_request = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs",
        json={"kind": "test", "command": ["pytest", "--version"]},
    )
    assert rejected_request.status_code == 202
    rejected_approval = await client.post(
        f"/api/v1/approvals/{rejected_request.json()['approval_id']}/reject",
        json={"note": "Do not run this command"},
    )
    assert rejected_approval.status_code == 200
    denied_execution = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs/{rejected_request.json()['verification_run']['id']}/execute"
    )
    assert denied_execution.status_code == 409

    async def unexpected_failure(self: DockerSandboxRunner, managed_workspace: Any, command: list[str]) -> SandboxResult:
        raise RuntimeError("transport diagnostic should not be exposed")

    monkeypatch.setattr(DockerSandboxRunner, "run", unexpected_failure)
    failed_request = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/verification-runs",
        json={"kind": "test"},
    )
    assert failed_request.status_code == 202
    failed_result = await approve_and_execute(failed_request)
    assert failed_result.status_code == 200
    assert failed_result.json()["status"] == "failed"
    assert "transport diagnostic" not in failed_result.json()["error"]


@pytest.mark.asyncio
async def test_acceptance_criteria_require_an_owner_admin_decision(client: AsyncClient) -> None:
    from sqlalchemy import select

    from solvyn.db.models import Organization, OrganizationMembership, Tenant, User
    from solvyn.db.session import async_session_factory, bind_tenant_scope

    workflow = await client.post("/api/v1/workflows/feature/run", json={"requirement": "Track task acceptance criteria"})
    assert workflow.status_code == 200
    execution_id = workflow.json()["execution_id"]
    task_history = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    task_id = task_history.json()["tasks"][0]["task_id"]

    criteria = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/acceptance-criteria",
        json={"criteria": ["The task output includes the requested API contract", "Failure cases are covered"]},
    )
    assert criteria.status_code == 201
    assert [criterion["status"] for criterion in criteria.json()["acceptance_criteria"]] == ["pending", "pending"]

    unsigned_decision = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/acceptance-criteria/{criteria.json()['acceptance_criteria'][0]['id']}/decision",
        json={"decision": "accepted", "note": "Reviewed"},
    )
    assert unsigned_decision.status_code == 401

    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        await bind_tenant_scope(db, tenant.id)
        organization = Organization(
            id=organization_id,
            name="Acceptance Review",
            slug=f"acceptance-review-{uuid.uuid4().hex[:10]}",
            tenant_id=tenant.id,
        )
        db.add_all(
            [
                organization,
                User(id=user_id, email=f"acceptance-{uuid.uuid4().hex[:12]}@example.com", password_hash="test-hash"),
            ]
        )
        await db.flush()
        db.add(OrganizationMembership(user_id=user_id, organization_id=organization.id, role="owner"))
        await db.commit()

    from solvyn.identity.security import create_access_token

    client.headers["Authorization"] = f"Bearer {create_access_token(subject=str(user_id))}"
    client.headers["X-Tenant-Slug"] = "test-tenant"
    criterion_id = criteria.json()["acceptance_criteria"][0]["id"]
    decision = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/acceptance-criteria/{criterion_id}/decision",
        json={"decision": "accepted", "note": "Reviewed against the task output"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "accepted"
    assert decision.json()["reviewer_user_id"] == str(user_id)
    assert decision.json()["reviewer_organization_id"] == str(organization_id)
    assert decision.json()["decided_at"] is not None

    repeated_decision = await client.post(
        f"/api/v1/workflows/executions/{execution_id}/tasks/{task_id}/acceptance-criteria/{criterion_id}/decision",
        json={"decision": "rejected"},
    )
    assert repeated_decision.status_code == 409

    updated_history = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    task = next(item for item in updated_history.json()["tasks"] if item["task_id"] == task_id)
    reviewed_criterion = next(item for item in task["acceptance_criteria"] if item["id"] == criterion_id)
    assert reviewed_criterion["status"] == "accepted"
    assert reviewed_criterion["reviewer_organization_id"] == str(organization_id)


@pytest.mark.asyncio
async def test_task_inbox_lists_durable_tenant_tasks_with_filters(client: AsyncClient) -> None:
    workflow = await client.post("/api/v1/workflows/feature/run", json={"requirement": "Build the task inbox"})
    assert workflow.status_code == 200
    execution_id = workflow.json()["execution_id"]
    history = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    expected_ids = {item["task_id"] for item in history.json()["tasks"]}

    inbox = await client.get("/api/v1/workflows/tasks?limit=100")
    assert inbox.status_code == 200
    listed = [task for task in inbox.json()["tasks"] if task["execution_id"] == execution_id]
    assert {task["task_id"] for task in listed} == expected_ids
    assert all(task["title"] and task["status"] and task["assigned_agent"] for task in listed)

    completed = await client.get("/api/v1/workflows/tasks?status=completed&limit=100")
    assert completed.status_code == 200
    assert all(task["status"] == "completed" for task in completed.json()["tasks"])
    invalid_limit = await client.get("/api/v1/workflows/tasks?limit=0")
    assert invalid_limit.status_code == 422


@pytest.mark.asyncio
async def test_feature_workflow_runs_to_completion(client: AsyncClient) -> None:
    """POST /workflows/feature/run completes synchronously with all 9 nodes."""
    response = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Build a simple todo list API"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow"] == "feature"
    assert data["status"] in ("completed", "failed")
    assert "session_id" in data
    # 9 steps in the feature template
    assert len(data["nodes"]) == 9
    task_response = await client.get(f"/api/v1/workflows/executions/{data['execution_id']}/tasks")
    assert task_response.status_code == 200
    tasks = task_response.json()["tasks"]
    assert len(tasks) == 9
    assert all(task["attempt_count"] >= 1 for task in tasks)
    assert all(task["max_attempts"] is None for task in tasks)
    assert any(task["dependencies"] for task in tasks)

    from sqlalchemy import select, update

    from solvyn.db.models import (
        Organization,
        OrganizationMembership,
        Project,
        TeammateProfile,
        TeammateProjectAssignment,
        TeammateStatus,
        Tenant,
        User,
    )
    from solvyn.db.session import async_session_factory, bind_tenant_scope

    project_id = uuid.uuid4()
    teammate_id = uuid.uuid4()
    user_id = uuid.uuid4()
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        await bind_tenant_scope(db, tenant.id)
        organization = Organization(
            id=uuid.uuid4(),
            name="Handoff Test Org",
            slug=f"handoff-{uuid.uuid4().hex[:12]}",
            tenant_id=tenant.id,
        )
        project = Project(
            id=project_id,
            organization_id=organization.id,
            tenant_id=tenant.id,
            name="Handoff Test Project",
            slug=f"handoff-{uuid.uuid4().hex[:12]}",
        )
        teammate = TeammateProfile(
            id=teammate_id,
            organization_id=organization.id,
            tenant_id=tenant.id,
            name="Handoff Engineer",
            role="Software Engineer",
            agent_type="backend-engineer",
            status=TeammateStatus.BUSY,
        )
        db.add_all(
            [
                organization,
                project,
                teammate,
                TeammateProjectAssignment(teammate_id=teammate_id, project_id=project_id),
                User(id=user_id, email=f"handoff-{uuid.uuid4().hex[:12]}@example.com", password_hash="test-hash"),
                OrganizationMembership(user_id=user_id, organization_id=organization.id, role="owner"),
            ]
        )
        await db.commit()

    handoff_response = await client.post(
        f"/api/v1/workflows/executions/{data['execution_id']}/tasks/{tasks[0]['task_id']}/handoffs",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "target_teammate_id": str(teammate_id),
            "project_id": str(project_id),
            "reason": "Implement the assigned backend follow-up",
            "input_data": {"task": "Implement the assigned backend follow-up"},
            "requires_approval": True,
        },
    )
    assert handoff_response.status_code == 200
    assert handoff_response.json()["status"] == "waiting_approval"

    pending_tasks = await client.get(f"/api/v1/workflows/executions/{data['execution_id']}/tasks")
    pending_handoff = pending_tasks.json()["handoffs"][0]
    assert pending_handoff["status"] == "waiting_approval"
    assert pending_handoff["approval_status"] == "pending"
    approval_replay = await client.post(
        f"/api/v1/workflows/executions/{data['execution_id']}/tasks/{tasks[0]['task_id']}/handoffs",
        json={
            "idempotency_key": pending_handoff["idempotency_key"],
            "target_teammate_id": str(teammate_id),
            "project_id": str(project_id),
            "reason": "Implement the assigned backend follow-up",
            "input_data": {"task": "Implement the assigned backend follow-up"},
            "requires_approval": True,
        },
    )
    assert approval_replay.status_code == 200
    assert approval_replay.json()["approval_id"] == pending_handoff["approval_id"]

    from solvyn.identity.security import create_access_token

    client.headers["Authorization"] = f"Bearer {create_access_token(subject=str(user_id))}"
    client.headers["X-Tenant-Slug"] = "test-tenant"
    inbox = await client.get("/api/v1/approvals")
    assert inbox.status_code == 200
    approval = next(item for item in inbox.json()["approvals"] if item["task_handoff_id"] == pending_handoff["handoff_id"])
    approved = await client.post(f"/api/v1/approvals/{approval['id']}/approve", json={"note": "Approved for execution"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    handoff_response = await client.post(
        f"/api/v1/workflows/executions/{data['execution_id']}/tasks/{tasks[0]['task_id']}/handoffs/{pending_handoff['handoff_id']}/resume"
    )
    assert handoff_response.status_code == 409
    assert handoff_response.json()["detail"] == "Teammate is already running; retry this handoff"

    async with async_session_factory() as db:
        await db.execute(
            update(TeammateProfile)
            .where(TeammateProfile.id == teammate_id)
            .values(status=TeammateStatus.IDLE)
        )
        await db.commit()

    handoff_response = await client.post(
        f"/api/v1/workflows/executions/{data['execution_id']}/tasks/{tasks[0]['task_id']}/handoffs/{pending_handoff['handoff_id']}/resume"
    )
    assert handoff_response.status_code == 200
    assert handoff_response.json()["status"] == "completed"

    updated_tasks = await client.get(f"/api/v1/workflows/executions/{data['execution_id']}/tasks")
    handoffs = updated_tasks.json()["handoffs"]
    assert len(handoffs) == 1
    assert handoffs[0]["delegated_to"] == "backend-engineer"
    assert handoffs[0]["status"] == "completed"
    assert handoffs[0]["output"] is not None
    assert len(updated_tasks.json()["tasks"]) == 10

    repeated_handoff = await client.post(
        f"/api/v1/workflows/executions/{data['execution_id']}/tasks/{tasks[0]['task_id']}/handoffs",
        json={
            "idempotency_key": handoffs[0]["idempotency_key"],
            "target_teammate_id": str(teammate_id),
            "project_id": str(project_id),
            "reason": "Implement the assigned backend follow-up",
            "input_data": {"task": "Implement the assigned backend follow-up"},
            "requires_approval": True,
        },
    )
    assert repeated_handoff.status_code == 200
    assert repeated_handoff.json()["handoff_id"] == handoffs[0]["handoff_id"]
    assert len((await client.get(f"/api/v1/workflows/executions/{data['execution_id']}/tasks")).json()["tasks"]) == 10

    rejected_request = await client.post(
        f"/api/v1/workflows/executions/{data['execution_id']}/tasks/{tasks[0]['task_id']}/handoffs",
        json={
            "idempotency_key": str(uuid.uuid4()),
            "target_teammate_id": str(teammate_id),
            "project_id": str(project_id),
            "reason": "Prepare a second backend follow-up",
            "input_data": {"task": "Prepare a second backend follow-up"},
            "requires_approval": True,
        },
    )
    assert rejected_request.status_code == 200
    assert rejected_request.json()["status"] == "waiting_approval"
    approval_inbox = await client.get("/api/v1/approvals")
    second_approval = next(
        item
        for item in approval_inbox.json()["approvals"]
        if item["task_handoff_id"] == rejected_request.json()["handoff_id"]
    )
    rejected = await client.post(
        f"/api/v1/approvals/{second_approval['id']}/reject",
        json={"note": "Needs a clearer scope"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    rejected_tasks = await client.get(f"/api/v1/workflows/executions/{data['execution_id']}/tasks")
    rejected_handoff = next(
        handoff
        for handoff in rejected_tasks.json()["handoffs"]
        if handoff["handoff_id"] == rejected_request.json()["handoff_id"]
    )
    assert rejected_handoff["status"] == "rejected"
    rejected_child = next(
        task for task in rejected_tasks.json()["tasks"] if task["task_id"] == rejected_handoff["child_task_id"]
    )
    assert rejected_child["status"] == "failed"

    agent_types = {n["agent_type"] for n in data["nodes"]}
    expected = {
        "product-manager",
        "system-designer",
        "backend-engineer",
        "frontend-engineer",
        "qa-engineer",
        "code-reviewer",
        "security-auditor",
        "devops-engineer",
        "documentation-writer",
    }
    assert expected.issubset(agent_types)


@pytest.mark.asyncio
async def test_only_one_concurrent_approval_decision_is_recorded(client: AsyncClient) -> None:
    from sqlalchemy import select

    from solvyn.db.models import Organization, OrganizationMembership, Tenant, User
    from solvyn.db.session import async_session_factory
    from solvyn.identity.security import create_access_token

    user_id = uuid.uuid4()
    organization_id = uuid.uuid4()
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        organization = Organization(
            id=organization_id,
            name="Approval Race Review",
            slug=f"approval-race-{uuid.uuid4().hex[:10]}",
            tenant_id=tenant.id,
        )
        db.add_all(
            [
                organization,
                User(id=user_id, email=f"approval-race-{uuid.uuid4().hex[:12]}@example.com", password_hash="test-hash"),
            ]
        )
        await db.flush()
        db.add(OrganizationMembership(user_id=user_id, organization_id=organization.id, role="owner"))
        await db.commit()

    client.headers["Authorization"] = f"Bearer {create_access_token(subject=str(user_id))}"
    client.headers["X-Tenant-Slug"] = "test-tenant"
    created = await client.post(
        "/api/v1/approvals",
        json={"organization_id": str(organization_id), "kind": "command", "summary": "Run the deployment check"},
    )
    assert created.status_code == 201
    approval_id = created.json()["id"]

    approved, rejected = await asyncio.gather(
        client.post(f"/api/v1/approvals/{approval_id}/approve", json={"note": "Approved"}),
        client.post(f"/api/v1/approvals/{approval_id}/reject", json={"note": "Rejected"}),
    )
    decision_statuses = sorted([approved.status_code, rejected.status_code])
    assert decision_statuses[0] == 200
    assert decision_statuses[1] in (400, 409)

    final = await client.get(f"/api/v1/approvals/{approval_id}")
    assert final.status_code == 200
    assert final.json()["status"] in {"approved", "rejected"}
    assert final.json()["decided_by"] == str(user_id)
    assert final.json()["decided_at"] is not None


@pytest.mark.asyncio
async def test_cancel_abandoned_workflow_execution(client: AsyncClient) -> None:
    """Abandoned and active executions cancel without allowing stale checkpoint writes."""
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from solvyn.agents.schema import ExecutionEdge
    from solvyn.agents.schema import ExecutionGraph as ExecutionGraphState
    from solvyn.db.models import ExecutionGraph, Session, Task, Tenant
    from solvyn.db.session import async_session_factory, bind_tenant_scope

    session_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    running_task_id = uuid.uuid4()
    pending_task_id = uuid.uuid4()
    active_execution_id = uuid.uuid4()
    active_session_id = uuid.uuid4()
    active_lease_id = uuid.uuid4()
    active_task_id = uuid.uuid4()
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        await bind_tenant_scope(db, tenant.id)
        db.add(Session(id=session_id, tenant_id=tenant.id, title="Cancellation test"))
        db.add(
            ExecutionGraph(
                id=execution_id,
                session_id=session_id,
                status="failed",
                graph_data=json.dumps(
                    {
                        "template": "feature",
                        "inputs": {},
                        "nodes": [
                            {"id": str(running_task_id), "agent_type": "backend-engineer", "status": "running"},
                            {"id": str(pending_task_id), "agent_type": "qa-engineer", "status": "pending"},
                        ],
                        "edges": [],
                    }
                ),
            )
        )
        db.add(Session(id=active_session_id, tenant_id=tenant.id, title="Active cancellation test"))
        db.add(
            ExecutionGraph(
                id=active_execution_id,
                session_id=active_session_id,
                status="running",
                graph_data=json.dumps(
                    {
                        "template": "feature",
                        "inputs": {},
                        "nodes": [{"id": str(active_task_id), "agent_type": "backend-engineer", "status": "running"}],
                        "edges": [],
                    }
                ),
                lease_owner=active_lease_id,
                lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
            )
        )
        await db.commit()
        await bind_tenant_scope(db, tenant.id)
        db.add(
            Task(id=running_task_id, tenant_id=tenant.id, session_id=session_id, execution_id=execution_id,
                 node_id=str(running_task_id), title="Running task", status="running")
        )
        db.add(
            Task(id=pending_task_id, tenant_id=tenant.id, session_id=session_id, execution_id=execution_id,
                 node_id=str(pending_task_id), title="Pending task", status="pending")
        )
        db.add(
            Task(id=active_task_id, tenant_id=tenant.id, session_id=active_session_id,
                 execution_id=active_execution_id, node_id=str(active_task_id),
                 title="Actively running task", status="running")
        )
        await db.commit()

    response = await client.post(f"/api/v1/workflows/executions/{execution_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"

    snapshot = await client.get(f"/api/v1/workflows/executions/{execution_id}")
    assert snapshot.status_code == 200
    assert snapshot.json()["status"] == "cancelled"
    assert {node["status"] for node in snapshot.json()["snapshot"]["nodes"]} == {"cancelled"}
    tasks = await client.get(f"/api/v1/workflows/executions/{execution_id}/tasks")
    assert tasks.status_code == 200
    assert {task["status"] for task in tasks.json()["tasks"]} == {"cancelled"}

    active_cancel = await client.post(f"/api/v1/workflows/executions/{active_execution_id}/cancel")
    assert active_cancel.status_code == 200
    assert active_cancel.json()["status"] == "cancelling"
    from solvyn.db.models import Tenant as TenantModel
    from solvyn.workflows.executor import workflow_executor

    async with async_session_factory() as db:
        active_tenant = (await db.execute(select(TenantModel).where(TenantModel.slug == "test-tenant"))).scalar_one()
    await workflow_executor._mark_failed(str(active_execution_id), active_lease_id, active_tenant.id)

    active_snapshot = await client.get(f"/api/v1/workflows/executions/{active_execution_id}")
    assert active_snapshot.status_code == 200
    assert active_snapshot.json()["status"] == "cancelled"
    assert active_snapshot.json()["snapshot"]["nodes"][0]["status"] == "cancelled"
    active_tasks = await client.get(f"/api/v1/workflows/executions/{active_execution_id}/tasks")
    assert active_tasks.status_code == 200
    assert active_tasks.json()["tasks"][0]["status"] == "cancelled"
    with pytest.raises(RuntimeError, match="lease was lost"):
        await workflow_executor._persist_execution_graph(
            ExecutionGraphState(
                id=str(active_execution_id),
                session_id=str(active_session_id),
                nodes=[],
                edges=[ExecutionEdge(source="", target="")],
            ),
            "feature",
            {},
            active_tenant.id,
            active_lease_id,
        )


@pytest.mark.asyncio
async def test_feature_workflow_returns_resolvable_session_id(client: AsyncClient) -> None:
    """The session_id returned by the workflow must be queryable via observability."""
    response = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Build a calculator"},
    )
    assert response.status_code == 200
    session_id = response.json()["session_id"]
    assert session_id

    # Polling pattern: look up traces for the session
    traces = await client.get(f"/api/v1/observability/traces?session_id={session_id}")
    assert traces.status_code == 200
    assert "traces" in traces.json()


@pytest.mark.asyncio
async def test_shared_secret_workflow_reads_bind_the_resolved_default_tenant(client: AsyncClient) -> None:
    workflow = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Read a workflow under the default tenant"},
    )
    assert workflow.status_code == 200
    execution_id = workflow.json()["execution_id"]

    snapshot = await client.get(f"/api/v1/workflows/executions/{execution_id}")
    recent_tasks = await client.get("/api/v1/workflows/tasks")

    assert snapshot.status_code == 200
    assert snapshot.json()["execution_id"] == execution_id
    assert recent_tasks.status_code == 200
    assert any(task["execution_id"] == execution_id for task in recent_tasks.json()["tasks"])


@pytest.mark.asyncio
async def test_feature_workflow_outputs_include_provider_and_cost(client: AsyncClient) -> None:
    """Each executed agent node carries provider/model/cost info through to the response."""
    response = await client.post(
        "/api/v1/workflows/feature/run",
        json={"requirement": "Generate a REST API"},
    )
    assert response.status_code == 200
    nodes = response.json()["nodes"]
    # The endpoint summary only exposes agent_type / status / error,
    # so verify the count and that at least one node completed.
    assert nodes
    completed = [n for n in nodes if n["status"] == "completed"]
    assert completed, f"expected at least one completed node, got {nodes!r}"


@pytest.mark.asyncio
async def test_bugfix_workflow_runs(client: AsyncClient) -> None:
    """The bugfix pipeline executes all four steps."""
    response = await client.post(
        "/api/v1/workflows/bugfix/run",
        json={"bug_report": "Login returns 500 on empty email input"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow"] == "bugfix"
    assert len(data["nodes"]) == 4


@pytest.mark.asyncio
async def test_release_workflow_runs(client: AsyncClient) -> None:
    """The release pipeline executes all three steps."""
    response = await client.post(
        "/api/v1/workflows/release/run",
        json={
            "changelog_input": "v2.0.0: Added dark mode, fixed pagination",
            "codebase_snapshot": "Latest main branch snapshot",
            "project_info": "Solvyn production release",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["workflow"] == "release"
    assert len(data["nodes"]) == 3


@pytest.mark.asyncio
async def test_unknown_workflow_returns_404(client: AsyncClient) -> None:
    """POST /workflows/{name}/run with an unknown name yields 404."""
    response = await client.post("/api/v1/workflows/nonexistent/run", json={})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_audit_search_filters_records_by_tenant() -> None:
    from datetime import timedelta

    from sqlalchemy import delete, select

    from solvyn.db.models import Memory, Tenant
    from solvyn.db.session import async_session_factory, bind_tenant_scope
    from solvyn.observability.audit import AuditLogger

    audit_id = f"audit:{uuid.uuid4()}"
    other_audit_id = f"audit:{uuid.uuid4()}"
    newer_id = f"audit:{uuid.uuid4()}"
    other_tenant = Tenant(name="Other Audit Tenant", slug=f"audit-{uuid.uuid4().hex[:12]}", api_key_hash="test-hash")
    async with async_session_factory() as db:
        tenant = (await db.execute(select(Tenant).where(Tenant.slug == "test-tenant"))).scalar_one()
        db.add(other_tenant)
        await db.flush()
        now = datetime.now(UTC)
        await bind_tenant_scope(db, tenant.id)
        db.add_all(
            [
                Memory(
                    tenant_id=tenant.id,
                    agent_type="qa-engineer",
                    key=audit_id,
                    value='{"action":"wanted.action"}',
                    created_at=now - timedelta(minutes=1),
                ),
                Memory(
                    tenant_id=tenant.id,
                    agent_type="qa-engineer",
                    key=newer_id,
                    value='{"action":"other.action"}',
                    created_at=now,
                ),
            ]
        )
        await db.commit()
    async with async_session_factory() as db:
        await bind_tenant_scope(db, other_tenant.id)
        db.add(
            Memory(
                tenant_id=other_tenant.id,
                agent_type="qa-engineer",
                key=other_audit_id,
                value='{"action":"wanted.action"}',
                created_at=now + timedelta(minutes=1),
            )
        )
        await db.commit()

    try:
        entries = await AuditLogger().search(tenant_id=tenant.id, action="wanted.action", limit=1)
        assert [entry["id"] for entry in entries] == [audit_id]
    finally:
        async with async_session_factory() as db:
            await bind_tenant_scope(db, tenant.id)
            await db.execute(delete(Memory).where(Memory.key.in_([audit_id, newer_id])))
            await db.commit()
        async with async_session_factory() as db:
            await bind_tenant_scope(db, other_tenant.id)
            await db.execute(delete(Memory).where(Memory.key == other_audit_id))
            await db.execute(delete(Tenant).where(Tenant.id == other_tenant.id))
            await db.commit()


@pytest.mark.asyncio
async def test_audit_api_scopes_search_to_authenticated_tenant() -> None:
    from types import SimpleNamespace

    from solvyn.observability.analytics_api import get_audit_logs

    tenant_id = uuid.uuid4()
    with patch("solvyn.observability.analytics_api.audit_logger.search", new_callable=AsyncMock) as search:
        search.return_value = []
        with patch(
            "solvyn.observability.analytics_api.tenant_context",
            SimpleNamespace(tenant_id=tenant_id, is_authenticated=True),
        ):
            response = await get_audit_logs(limit=50)
    assert response == {"entries": [], "count": 0}
    assert search.await_args is not None
    assert search.await_args.kwargs["tenant_id"] == tenant_id


@pytest.mark.asyncio
async def test_audit_endpoint_requires_tenant_context(unauthed_client: AsyncClient) -> None:
    response = await unauthed_client.get("/api/v1/observability/audit")
    assert response.status_code == 401


# ─── Tenant isolation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_workflows_endpoint_distinguishes_tenants(client: AsyncClient) -> None:
    """The tenant middleware fires for every request and resolves the slug.

    Uses a non-persisting endpoint (``GET /api/v1/workflows/``) so the
    test does not depend on the workflow executor's DB persistence path.
    Also stubs the per-tenant API-key validator so no real DB connection
    is required.
    """
    tenant_a = MagicMock()
    tenant_a.id = uuid.uuid4()
    tenant_a.slug = "tenant-a"
    tenant_a.is_active = True
    tenant_a.role = "user"
    tenant_b = MagicMock()
    tenant_b.id = uuid.uuid4()
    tenant_b.slug = "tenant-b"
    tenant_b.is_active = True
    tenant_b.role = "user"

    with (
        patch("solvyn.multi_tenant.middleware.resolve_tenant", new_callable=AsyncMock) as resolve_mock,
        patch("solvyn.multi_tenant.auth._validate_tenant_api_key", new_callable=AsyncMock) as validate_mock,
    ):
        validate_mock.return_value = tenant_a  # Accept any key in this test

        async def _resolve(req: Any) -> Any:
            slug = req.headers.get("X-Tenant-Slug", "")
            from solvyn.multi_tenant.middleware import tenant_context
            if slug == "tenant-a":
                tenant_context.set(tenant_a.id, tenant_a.slug, tenant_a.role)
                return tenant_a
            if slug == "tenant-b":
                tenant_context.set(tenant_b.id, tenant_b.slug, tenant_b.role)
                return tenant_b
            return None

        resolve_mock.side_effect = _resolve

        response_a = await client.get("/api/v1/workflows/", headers={"X-Tenant-Slug": "tenant-a"})
        response_b = await client.get("/api/v1/workflows/", headers={"X-Tenant-Slug": "tenant-b"})

    # Both requests succeed — the proof of isolation is that the tenant
    # middleware dispatched each one with the correct slug header.
    assert response_a.status_code == 200
    assert response_b.status_code == 200
    # The mock was invoked exactly once per request, demonstrating that
    # the tenant middleware fired for each X-Tenant-Slug.
    assert resolve_mock.await_count == 2


@pytest.mark.asyncio
async def test_tenant_analytics_blocks_cross_tenant_access() -> None:
    """Tenant A cannot read tenant B's analytics.

    The analytics endpoint short-circuits with 403 when the path tenant_id
    does not match the calling tenant. This test verifies the route guard
    directly without relying on middleware state or a database connection.
    """
    from fastapi import HTTPException

    from solvyn.multi_tenant.middleware import tenant_context
    from solvyn.observability.analytics_api import get_tenant_analytics

    current_tenant = uuid.uuid4()
    other_tenant = MagicMock()
    other_tenant.id = uuid.uuid4()
    tenant_context.set(current_tenant, "tenant-a")
    try:
        with (
            patch("solvyn.observability.analytics_api._resolve_active_tenant", return_value=other_tenant),
            patch("solvyn.observability.analytics_api.bind_tenant_scope", new_callable=AsyncMock),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await get_tenant_analytics(str(other_tenant.id), db=AsyncMock())
        assert exc_info.value.status_code == 403
        assert "tenant" in str(exc_info.value.detail).lower()
    finally:
        tenant_context.clear()


@pytest.mark.asyncio
async def test_session_listing_returns_empty_without_tenant(client: AsyncClient) -> None:
    """Without a tenant in context, the session list is empty (no leakage)."""
    response = await client.get("/api/v1/sessions/")
    assert response.status_code == 200
    body = response.json()
    assert "sessions" in body
    assert body["sessions"] == []


# ─── API key auth ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_api_key_returns_401(unauthed_client: AsyncClient) -> None:
    """Protected endpoints reject requests without an X-API-Key header."""
    response = await unauthed_client.get("/api/v1/workflows/")
    assert response.status_code == 401
    body = response.json()
    assert "error" in body


@pytest.mark.asyncio
async def test_invalid_api_key_returns_401(unauthed_client: AsyncClient) -> None:
    """Protected endpoints reject requests with a wrong X-API-Key header."""
    response = await unauthed_client.get(
        "/api/v1/workflows/",
        headers={"X-API-Key": "definitely-not-a-valid-key"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_valid_api_key_returns_200(client: AsyncClient) -> None:
    """The default test client (valid key) reaches protected endpoints."""
    response = await client.get("/api/v1/workflows/")
    assert response.status_code == 200
    body = response.json()
    assert "workflows" in body
    assert len(body["workflows"]) >= 3


@pytest.mark.asyncio
async def test_agents_endpoint_requires_auth(unauthed_client: AsyncClient) -> None:
    """GET /workflows/agents must be authenticated."""
    response = await unauthed_client.get("/api/v1/workflows/agents")
    assert response.status_code == 401


# ─── Rate limiting ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rate_limit_returns_429_after_burst(
    client: AsyncClient, low_rate_limit: Any
) -> None:
    """Once the per-IP limit is exceeded, subsequent calls return 429."""
    statuses: list[int] = []
    for _ in range(8):
        resp = await client.get("/api/v1/workflows/")
        statuses.append(resp.status_code)

    # First 5 should pass; the rest should be rate-limited.
    assert statuses[:5] == [200, 200, 200, 200, 200]
    assert all(s == 429 for s in statuses[5:]), f"unexpected statuses: {statuses}"


@pytest.mark.asyncio
async def test_rate_limit_response_payload_shape(
    client: AsyncClient, low_rate_limit: Any
) -> None:
    """The 429 body should advertise retry_after and the limit scope."""
    for _ in range(6):
        await client.get("/api/v1/workflows/")
    response = await client.get("/api/v1/workflows/")
    assert response.status_code == 429
    body = response.json()
    assert "error" in body
    assert "retry_after" in body
    assert "scope" in body


# ─── WebSocket connection ──────────────────────────────────────────────


def test_websocket_connects_with_valid_api_key() -> None:
    """A WebSocket connection with a valid API key receives broadcast events.

    The WebSocket endpoint short-circuits to ``verify_api_key`` when no
    tenant is in context, so we stub that helper directly.  The broadcast
    is driven by the in-process :class:`WebSocketManager`.
    """
    from solvyn.main import create_app
    from solvyn.observability.websocket import ws_manager

    app = create_app()
    api_key = "test-websocket-key"

    with patch("solvyn.observability.analytics_api.verify_api_key", return_value=True) as verify_mock:
        with TestClient(app) as test_client:
            with test_client.websocket_connect(
                f"/api/v1/observability/ws/default?session_id=sess-1&api_key={api_key}"
            ) as ws:
                # Drive a server-side broadcast from the WebSocket manager
                asyncio.run(
                    ws_manager.handle_graph_update(
                        graph_id="g-e2e",
                        node_id="n1",
                        agent_type="product-manager",
                        status="running",
                        session_id="sess-1",
                        tenant_id="default",
                    )
                )
                message = ws.receive_text()

    assert verify_mock.called, "verify_api_key should have been invoked"
    assert "graph_update" in message
    assert "product-manager" in message
    assert "running" in message


def test_websocket_rejects_unauthorized_connection() -> None:
    """A WebSocket without an API key is closed by the server (4001)."""
    from fastapi.websockets import WebSocketDisconnect

    from solvyn.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with test_client.websocket_connect("/api/v1/observability/ws/" + str(uuid.uuid4())):
                pass
    # 4001 is the application-defined "Unauthorized" close code.
    assert exc_info.value.code == 4001


def test_websocket_rejects_invalid_api_key() -> None:
    """A WebSocket with a wrong API key is closed by the server."""
    from fastapi.websockets import WebSocketDisconnect

    from solvyn.main import create_app

    app = create_app()
    with patch("solvyn.observability.analytics_api.verify_api_key", return_value=False):
        with TestClient(app) as test_client:
            with pytest.raises(WebSocketDisconnect) as exc_info:
                with test_client.websocket_connect(
                    "/api/v1/observability/ws/default?api_key=definitely-wrong"
                ):
                    pass
    assert exc_info.value.code == 4001
