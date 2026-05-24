# Solvyn Focused Production Hardening Design

## Objective

Turn the current Solvyn repository into a defensible production baseline by correcting confirmed functional and tenant-isolation defects, tightening packaging and deployment artifacts, and adding operator and agent documentation. This pass improves the existing FastAPI, Next.js, and Kubernetes design without adding new product modules.

## Confirmed Findings

- Static workflow resources are shadowed by `GET /workflows/{name}`; the dashboard's `GET /api/v1/workflows/agents` currently returns `404`.
- The frontend sends task generation JSON, while the backend accepts `specification` as a query parameter.
- Session listing and creation do not enforce tenant ownership; session creation omits the model's required `tenant_id`.
- Workflow input resolution stores placeholders for predecessor output rather than supplying completed output to downstream agents.
- If a predecessor fails, a queued dependent node is never ready and can be requeued forever.
- Deep health reports a disconnected Redis dependency as acceptable.
- Production CORS is fixed to wildcard origins while credentials are enabled.
- Backend container packaging includes development dependencies and has no `.dockerignore`.
- Kubernetes references an unexpanded password expression inside `DATABASE_URL`, and CI deploys a frontend Deployment not declared in the manifests.
- The configured lint and type-check commands do not currently pass.

## Application Changes

### Workflow API

Fixed collection routes will be declared before dynamic workflow lookup routes. Task generation will accept a validated JSON body with a required non-empty `specification` field, matching the existing frontend client. API regression tests will protect fixed-route matching and request-body behavior.

### Tenant-Safe Sessions

Session handlers will require a resolved tenant context, apply `Session.tenant_id` filtering to list operations, and set `tenant_id` when creating a record. Missing tenant context will produce a client-visible error. These endpoints remain dependent on middleware resolving `X-Tenant-Slug` to an active tenant.

### Workflow Execution

The graph owns runtime propagation of completed predecessor outputs: immediately before an agent runs, its input will replace workflow output references using the predecessor node output. Failed dependencies will mark blocked nodes as skipped with an explanatory error and the run will terminate as failed.

Templates and the executor will encode output references without pretending they are already resolved; no persistence model redesign is required in this pass.

### Health And Configuration

Deep health will be degraded when Redis is disconnected. CORS allowed origins will be loaded from configuration, defaulting to local frontend development rather than a credentialed wildcard.

## Packaging And Deployment

- Add root and frontend `.dockerignore` files.
- Build the backend runtime with application dependencies only and avoid editable/dev installation.
- Use `npm ci` from the committed lockfile in the frontend container.
- Move the complete production `DATABASE_URL` into the Kubernetes secret example instead of interpolating a password inside a ConfigMap.
- Add the missing frontend Deployment and Service definitions.
- Use the deep dependency health route for API readiness while keeping a lightweight liveness route.
- Keep Kubernetes manifests as templates: operators must replace images, ingress hostname, and secrets.

## Quality Gates And Documentation

Ruff violations will be corrected. Mypy will remain strict for application modules while suppressing errors caused only by untyped decorator internals from FastAPI and Typer; actual return and ignore-code issues encountered by the gate will be corrected.

New repository documents:

- `README.md`: setup, architecture, validation, local and container operations.
- `AGENTS.md`: repository operating guidance for coding agents and contributors.
- `SKILLS.md`: task-oriented capability map and validation discipline.
- `docs/PRODUCTION_READINESS.md`: secrets, deployment, migrations, monitoring, backup, rollback, and remaining limitations.

## Verification

- Regression tests first for route matching, JSON task generation, tenant-safe sessions, output propagation, failed-dependency termination, and deep health.
- Full backend validation: `ruff check solvyn tests`, `ruff format --check solvyn tests`, `mypy solvyn --follow-imports=skip`, and `python -m pytest -q`.
- Frontend validation: `npm run build`.
- Static inspection for Docker and Kubernetes artifacts where an active cluster or Docker daemon is not part of the workspace.

## Constraints

The workspace is not a Git repository, so specification, implementation, and verification changes cannot be committed or reviewed through Git history in this environment.
