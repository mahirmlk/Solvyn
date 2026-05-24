# Solvyn Focused Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix verified correctness and isolation defects and add the minimum packaging, deployment, and operational documentation needed for a production baseline.

**Architecture:** Retain the FastAPI/Next.js/Kubernetes structure. Correct endpoint contracts and tenant boundaries at the API layer, propagate workflow results inside the graph executor, terminate blocked workflows explicitly, and make delivery artifacts describe secure runtime configuration rather than development defaults.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy asyncio, pytest, Ruff, mypy, Next.js 15, Docker, Kubernetes.

---

## File Structure

- Modify `solvyn/api/v1/workflows.py`: fixed-route ordering and JSON task-generation request contract.
- Modify `solvyn/api/v1/sessions.py`: tenant enforcement and query ownership.
- Modify `solvyn/agents/graph.py` and `solvyn/workflows/executor.py`: runtime dependency output propagation and blocked-node termination.
- Modify `solvyn/api/v1/health.py`, `solvyn/config.py`, and `solvyn/main.py`: readiness and configurable CORS.
- Modify `tests/test_api.py`, `tests/test_agent_graph.py`, and add focused session tests: regression coverage.
- Modify `Dockerfile`, `frontend/Dockerfile`, `k8s/*.yaml`, `.github/workflows/ci-cd.yml`: production packaging/deployment.
- Create `.dockerignore`, `frontend/.dockerignore`, `README.md`, `AGENTS.md`, `SKILLS.md`, and `docs/PRODUCTION_READINESS.md`: repository and operations guidance.

### Task 1: API Contract And Route Regressions

- [ ] Add failing API tests asserting `GET /api/v1/workflows/agents` succeeds and `POST /api/v1/workflows/generate-tasks` accepts `{"specification": "..."}`.
- [ ] Run `python -m pytest tests/test_api.py -q` and confirm the new cases fail for current routing/request parsing behavior.
- [ ] Move fixed workflow routes above `/{name}` and introduce a Pydantic JSON request body model.
- [ ] Rerun `python -m pytest tests/test_api.py -q` and confirm the regression cases pass.

### Task 2: Tenant-Owned Sessions

- [ ] Add failing tests that call session handlers with no tenant context and with a tenant context, asserting rejection without context, filtering by tenant, and owned session creation.
- [ ] Run the new tests and confirm they fail under the unscoped implementation.
- [ ] Add a tenant requirement helper and apply tenant ownership to session reads and writes.
- [ ] Rerun the session tests and API suite.

### Task 3: Workflow Runtime Correctness

- [ ] Add failing graph tests asserting completed predecessor output is supplied to a dependent agent and failure causes dependent nodes to terminate as skipped.
- [ ] Run the graph tests and confirm current placeholder/infinite-ready behavior is exposed.
- [ ] Implement runtime output reference resolution and deterministic blocked-node handling.
- [ ] Run the graph and workflow tests and confirm the behaviors pass.

### Task 4: Readiness And Browser-Safe Configuration

- [ ] Add failing tests for degraded deep health when Redis is disconnected and configuration parsing of allowed CORS origins.
- [ ] Implement health semantics and configuration-driven CORS middleware setup.
- [ ] Run affected tests and retain development-compatible defaults.

### Task 5: Delivery Artifacts

- [ ] Create Docker ignore files and update Dockerfiles to install only runtime content/dependencies using deterministic frontend installation.
- [ ] Repair Kubernetes secret/config ownership of connection URLs, declare the frontend workload/service, and set dependency-aware API readiness.
- [ ] Align CI with backend/full frontend checks and remove known quality-gate violations.

### Task 6: Operations Documentation

- [ ] Create `README.md`, `AGENTS.md`, `SKILLS.md`, and `docs/PRODUCTION_READINESS.md`.
- [ ] Document local configuration, secrets, tenant headers, migrations, probes, deployment prerequisites, rollback, backups, and remaining production limitations.

### Task 7: Final Verification

- [ ] Run `ruff check solvyn tests` and `ruff format --check solvyn tests`.
- [ ] Run `mypy solvyn --follow-imports=skip`.
- [ ] Run `python -m pytest -q`.
- [ ] Run `npm run build` from `frontend`.
- [ ] Review created manifests and documentation against the design scope; report unavailable Docker/cluster runtime verification explicitly.

## Self-Review

- Coverage: each approved application defect, delivery blocker, required document, and validation command is assigned to a task.
- Scope: broader admin panels, distributed rate limiting, E2E harness expansion, and managed infrastructure are intentionally excluded.
- Constraint: commit steps cannot be performed because the workspace contains no Git repository metadata.
