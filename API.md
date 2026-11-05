# Solvyn API Reference

> REST API v1 — Base URL: `http://localhost:8000/api/v1`

---

## Table of Contents

1. [Authentication](#authentication)
2. [Rate Limiting](#rate-limiting)
3. [Health](#health)
4. [Sessions](#sessions)
5. [Workflows](#workflows)
6. [Repository Intelligence](#repository-intelligence)
7. [Observability](#observability)
8. [WebSocket](#websocket)
9. [Error Codes](#error-codes)
10. [Common Patterns](#common-patterns)

---

## Authentication

All API endpoints except `/health`, `/metrics`, `/docs`, and `/openapi.json` require authentication.

### Headers

| Header | Required | Description |
|--------|----------|-------------|
| `X-API-Key` | Yes (non-public paths) | API key for authentication |
| `X-Tenant-Slug` | Optional | Tenant identifier for multi-tenant isolation |

### Example

```bash
curl -H "X-API-Key: your-api-key" \
     -H "X-Tenant-Slug: acme-corp" \
     http://localhost:8000/api/v1/sessions/
```

### Authentication Errors

```json
// 401 — Missing header
{ "error": "Missing X-API-Key header" }

// 401 — Invalid key
{ "error": "Invalid X-API-Key" }
```

---

## Rate Limiting

### Rate Limit Headers

Rate-limited responses include:

| Status | Body |
|--------|------|
| 429 | `{ "error": "Rate limit exceeded", "retry_after": 60, "scope": "ip" }` |
| 429 | `{ "error": "Tenant rate limit exceeded", "retry_after": 60, "scope": "tenant" }` |

### Limits

| Scope | Limit | Window |
|-------|-------|--------|
| Per IP address | 100 requests | 60 seconds |
| Per tenant | 500 requests | 60 seconds |

---

## Health

### `GET /api/v1/health`

Basic liveness check. Returns immediately without external dependencies.

**Auth required**: No

**Response** `200 OK`:
```json
{
    "status": "ok",
    "service": "solvyn"
}
```

**Example**:
```bash
curl http://localhost:8000/api/v1/health
```

---

### `GET /api/v1/health/deep`

Deep health check. Verifies connectivity to all backend services.

**Auth required**: No

**Response** `200 OK` (all healthy):
```json
{
    "status": "ok",
    "checks": {
        "database": { "status": "ok" },
        "redis": { "status": "ok" }
    }
}
```

**Response** `200 OK` (degraded):
```json
{
    "status": "degraded",
    "checks": {
        "database": { "status": "error", "detail": "Connection refused" },
        "redis": { "status": "ok" }
    }
}
```

**Checks performed**:
- **Database**: `SELECT 1` via asyncpg
- **Redis**: `PING` command

If Redis is not connected (`message_bus.redis` is `None`), status is `"disconnected"` (not an error — API can operate without Redis for basic functions).

**Example**:
```bash
curl http://localhost:8000/api/v1/health/deep
```

---

## Sessions

### `GET /api/v1/sessions/`

List sessions (limited to 50 most recent).

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "sessions": [
        {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "title": "Feature development session",
            "status": "active",
            "created_at": "2024-01-15T10:30:00+00:00"
        },
        {
            "id": "550e8400-e29b-41d4-a716-446655440001",
            "title": "Bugfix session",
            "status": "completed",
            "created_at": "2024-01-14T09:00:00+00:00"
        }
    ]
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/sessions/
```

---

### `POST /api/v1/sessions/`

Create a new session. The session is automatically assigned to the current tenant context.

**Auth required**: Yes

**Request body**: None required (empty POST)

**Response** `200 OK`:
```json
{
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "title": "New Session",
    "status": "active"
}
```

**Notes**:
- Default title: `"New Session"`
- Status: `"active"`
- Tenant ID is set from `X-Tenant-Slug` header context

**Example**:
```bash
curl -X POST \
     -H "X-API-Key: your-api-key" \
     -H "X-Tenant-Slug: acme-corp" \
     http://localhost:8000/api/v1/sessions/
```

---

### `GET /api/v1/sessions/{session_id}`

Get a specific session by ID.

> **Note**: This endpoint is defined in the route configuration but the source implements only `GET /` and `POST /`. The `GET /{id}` endpoint may require implementation.

---

## Workflows

### `GET /api/v1/workflows/`

List available workflow templates.

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "workflows": [
        {
            "name": "feature",
            "description": "Full feature delivery pipeline: PRD → Design → Code → Test → Review → Docs",
            "steps": 9
        },
        {
            "name": "bugfix",
            "description": "Bug fix pipeline: Report → Analysis → Fix → Test → Review → Deploy",
            "steps": 4
        },
        {
            "name": "release",
            "description": "Release pipeline: Changelog → Docs → Security → Deploy",
            "steps": 3
        }
    ]
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/workflows/
```

---

### `GET /api/v1/workflows/{name}`

Get details of a specific workflow template.

**Auth required**: Yes

**Parameters**:
| Parameter | Type | In | Description |
|-----------|------|-----|-------------|
| `name` | `string` | path | Workflow name (`feature`, `bugfix`, `release`) |

**Response** `200 OK`:
```json
{
    "name": "feature",
    "description": "Full feature delivery pipeline: PRD → Design → Code → Test → Review → Docs",
    "steps": [
        {
            "agent_type": "product-manager",
            "description": "Analyze requirements and generate epics/stories",
            "depends_on": null,
            "parallel_with": null
        },
        {
            "agent_type": "system-designer",
            "description": "Design system architecture from requirements",
            "depends_on": ["product-manager"],
            "parallel_with": null
        },
        {
            "agent_type": "backend-engineer",
            "description": "Generate backend code from design spec",
            "depends_on": ["system-designer"],
            "parallel_with": ["frontend-engineer"]
        },
        {
            "agent_type": "frontend-engineer",
            "description": "Generate frontend components from design spec",
            "depends_on": ["system-designer"],
            "parallel_with": ["backend-engineer"]
        },
        {
            "agent_type": "qa-engineer",
            "description": "Generate tests for all code",
            "depends_on": ["backend-engineer", "frontend-engineer"],
            "parallel_with": null
        },
        {
            "agent_type": "code-reviewer",
            "description": "Review all generated code",
            "depends_on": ["backend-engineer", "frontend-engineer"],
            "parallel_with": null
        },
        {
            "agent_type": "security-auditor",
            "description": "Security audit of all code",
            "depends_on": ["backend-engineer", "frontend-engineer"],
            "parallel_with": null
        },
        {
            "agent_type": "devops-engineer",
            "description": "Generate deployment configuration",
            "depends_on": ["backend-engineer"],
            "parallel_with": null
        },
        {
            "agent_type": "documentation-writer",
            "description": "Generate documentation",
            "depends_on": ["backend-engineer", "frontend-engineer"],
            "parallel_with": null
        }
    ],
    "default_inputs": {
        "requirement": "Enter product requirement here"
    }
}
```

**Error Response** `404 Not Found`:
```json
{
    "detail": "Workflow 'unknown-workflow' not found"
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/workflows/feature
```

---

### `POST /api/v1/workflows/{name}/run`

Execute a workflow template.

**Auth required**: Yes

**Parameters**:
| Parameter | Type | In | Description |
|-----------|------|-----|-------------|
| `name` | `string` | path | Workflow name (`feature`, `bugfix`, `release`) |

**Request Body** (JSON):
```json
{
    "requirement": "Build a user authentication system with login, registration, and password reset"
}
```

For the `bugfix` workflow:
```json
{
    "bug_report": "Users cannot log in after password reset. Steps: 1. Reset password, 2. Try to log in — get 500 error"
}
```

For the `release` workflow:
```json
{
    "changelog_input": "Added user profiles, fixed login bug, updated dependencies",
    "codebase_snapshot": "Full API codebase",
    "project_info": "User management microservice"
}
```

**Optional fields for all workflows**:
| Field | Type | Description |
|-------|------|-------------|
| `tenant_id` | `string` | Override tenant ID (defaults to `settings.default_tenant_id`) |
| `session_id` | `string` | Custom session ID (auto-generated if omitted) |

**Response** `200 OK`:
```json
{
    "workflow": "feature",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "completed",
    "nodes": [
        { "agent_type": "product-manager", "status": "completed", "error": null },
        { "agent_type": "system-designer", "status": "completed", "error": null },
        { "agent_type": "backend-engineer", "status": "completed", "error": null },
        { "agent_type": "frontend-engineer", "status": "completed", "error": null },
        { "agent_type": "qa-engineer", "status": "completed", "error": null },
        { "agent_type": "code-reviewer", "status": "completed", "error": null },
        { "agent_type": "security-auditor", "status": "completed", "error": null },
        { "agent_type": "devops-engineer", "status": "completed", "error": null },
        { "agent_type": "documentation-writer", "status": "completed", "error": null }
    ]
}
```

**Partial failure** — some nodes fail, some succeed:
```json
{
    "workflow": "feature",
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "failed",
    "nodes": [
        { "agent_type": "product-manager", "status": "completed", "error": null },
        { "agent_type": "system-designer", "status": "completed", "error": null },
        { "agent_type": "backend-engineer", "status": "failed", "error": "LLM provider timeout" },
        { "agent_type": "frontend-engineer", "status": "failed", "error": "Upstream dependency failed" },
        { "agent_type": "qa-engineer", "status": "failed", "error": "Upstream dependency failed" }
    ]
}
```

**Error Response** `404 Not Found`:
```json
{
    "detail": "Workflow 'unknown' not found"
}
```

**Example**:
```bash
curl -X POST \
     -H "X-API-Key: your-api-key" \
     -H "Content-Type: application/json" \
     -d '{"requirement": "Build a user auth system"}' \
     http://localhost:8000/api/v1/workflows/feature/run
```

---

### `POST /api/v1/workflows/{name}/resume`

Resume a paused workflow from a checkpoint. (Endpoint route is defined at `/api/v1/workflows/{id}/resume`.)

---

### `POST /api/v1/workflows/generate-tasks`

Generate a task breakdown from a product specification. Uses the Product Manager agent.

**Auth required**: Yes

**Request Body** (plain text or JSON string):
```
Build a real-time collaborative document editor with:
- User authentication via OAuth
- Real-time sync via WebSockets
- Version history and document diffing
- Export to PDF and Markdown
- Team-based access control
```

**Response** `200 OK`:
```json
{
    "tasks": [
        {
            "title": "OAuth authentication implementation",
            "description": "Implement OAuth 2.0 flow for user login...",
            "acceptance_criteria": [
                "User can sign in with Google OAuth",
                "User can sign in with GitHub OAuth"
            ],
            "epic": "User Authentication",
            "priority": "high",
            "estimated_hours": 16
        },
        {
            "title": "WebSocket real-time sync engine",
            "description": "Build WebSocket-based sync engine...",
            "epic": "Real-Time Collaboration",
            "priority": "high",
            "estimated_hours": 24
        }
    ],
    "graph": {
        "nodes": [
            { "id": "task_0", "agent_type": "backend-engineer", "title": "OAuth authentication...", "status": "pending" },
            { "id": "task_1", "agent_type": "backend-engineer", "title": "WebSocket real-time...", "status": "pending" }
        ],
        "edges": [
            { "source": "task_0", "target": "task_1" }
        ],
        "task_count": 2
    }
}
```

**Example**:
```bash
curl -X POST \
     -H "X-API-Key: your-api-key" \
     -H "Content-Type: application/json" \
     -d '"Build a real-time collaborative editor with auth, sync, and versioning"' \
     http://localhost:8000/api/v1/workflows/generate-tasks
```

---

### `GET /api/v1/workflows/agents`

List available agent types and their capabilities.

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "agents": [
        {
            "type": "backend-engineer",
            "name": "Backend Engineer",
            "description": "Generate FastAPI endpoints, models, business logic",
            "capabilities": ["code_generation"]
        },
        {
            "type": "code-reviewer",
            "name": "Code Reviewer",
            "description": "Static analysis, style, correctness verification",
            "capabilities": ["code_review"]
        },
        {
            "type": "devops-engineer",
            "name": "DevOps Engineer",
            "description": "Docker, k8s, CI/CD, Terraform",
            "capabilities": ["devops"]
        },
        {
            "type": "documentation-writer",
            "name": "Documentation Writer",
            "description": "README, API docs, release notes",
            "capabilities": ["documentation"]
        },
        {
            "type": "frontend-engineer",
            "name": "Frontend Engineer",
            "description": "React components, hooks, state management",
            "capabilities": ["code_generation"]
        },
        {
            "type": "product-manager",
            "name": "Product Manager",
            "description": "Parse PRDs, generate epics and user stories",
            "capabilities": ["product_management"]
        },
        {
            "type": "qa-engineer",
            "name": "QA Engineer",
            "description": "Unit, integration, and E2E tests",
            "capabilities": ["testing"]
        },
        {
            "type": "security-auditor",
            "name": "Security Auditor",
            "description": "Vulnerability scanning, OWASP, secrets detection",
            "capabilities": ["security"]
        },
        {
            "type": "system-designer",
            "name": "System Designer",
            "description": "Architecture diagrams, tech stack, schema design",
            "capabilities": ["system_design", "architecture"]
        }
    ]
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/workflows/agents
```

---

## Repository Intelligence

These endpoints require a user JWT in `Authorization: Bearer <access-token>` and membership in the organization that owns the project. Tenant API keys alone cannot connect or inspect repositories.

Repository inspection supports public repositories. Solvyn can write through a connected organization token using bounded branch and commit operations. Repository text and metadata remain untrusted input.

### `GET /api/v1/organizations/{organization_id}/integrations/github`

Read the GitHub credential connection status. Requires organization membership. The response reports the GitHub account login and last update time; it never returns a credential.

### `PUT /api/v1/organizations/{organization_id}/integrations/github`

Connect or replace an organization GitHub token. Requires owner or admin membership. Solvyn validates the token with GitHub, encrypts it with `INTEGRATION_ENCRYPTION_KEYS`, and returns only the connected account. If no encryption key is configured, this endpoint returns `503` without contacting GitHub or storing the token.

### `DELETE /api/v1/organizations/{organization_id}/integrations/github`

Disconnect the organization GitHub token. Requires owner or admin membership and deletes the encrypted credential.

Generate a Fernet key with the command in `.env.example`. For key rotation, configure the new key first and retain the previous keys in the comma-separated list; reconnect each organization integration so its token is encrypted with the new key before removing old keys. A fine-grained GitHub token scoped to the required repositories is recommended; never send tokens through query parameters.

### `POST /api/v1/projects/{project_id}/repositories`

Connect a public GitHub repository to a project. Requires an organization owner or admin.

```json
{ "full_name": "owner/repository" }
```

### `GET /api/v1/projects/{project_id}/repositories`

List repositories connected to a project visible to the caller.

### `GET /api/v1/repositories/{repository_id}/inspect`

Inspect the default branch. Returns repository metadata, revision SHA, file count, top-level entries, language counts, up to six common manifests (each capped at 64 KB), parsed direct dependencies, and a compact project context. `content_trust` is `untrusted_repository_data`.

### Explicit repository context for teammate runs

`POST /api/v1/teammates/{teammate_id}/run` accepts `project_id`, `input_data`, an optional `repository_id`, and up to four optional `repository_paths`. If a repository ID is supplied, it must identify an active GitHub repository connected to the same assigned project. Solvyn fetches a bounded public inspection summary and the explicitly selected text files (up to 1,500 characters each) and adds them to the user-level prompt as untrusted repository data; a fixed system instruction says not to follow instructions found there. Paths must be relative and cannot contain traversal segments. If omitted, no repository fetch occurs. This path provides context for investigation; it does not enable repository edits, sandbox execution, or automatic verification.

### Seed a delegated task workspace from repository files

`POST /api/v1/workflows/executions/{execution_id}/tasks/{task_id}/workspace` continues to accept caller-supplied `files`. Alternatively, send `repository_id` and one to 50 `repository_paths`; the two modes cannot be combined. Repository seeding requires a signed-in organization member, a delegated child task with a matching `TaskHandoff`, and a public active GitHub repository in that handoff's project. The API resolves the repository's default branch once and reads selected regular files at that commit SHA. Symlinks, submodules, `.git` paths, binary or oversized files, and invalid relative paths are rejected. Files are limited to 1 MB each and 16 MiB total. The response and `TaskWorkspace` record include the repository ID, source commit SHA, and selected paths. This is a selected-file snapshot, not a Git clone or worktree.

```json
{
  "repository_id": "<connected-repository-uuid>",
  "repository_paths": ["README.md", "src/app.py"]
}
```

### `GET /api/v1/repositories/{repository_id}/files/search?query=...`

Search file paths case-insensitively. This endpoint searches paths only; it does not search file contents.

### `GET /api/v1/repositories/{repository_id}/branches`

List up to the first 100 branches and their commit SHAs. When `possibly_truncated` is true, the repository has at least 100 branches and the response may omit later branches.

### `POST /api/v1/repositories/{repository_id}/branches`

Create a `solvyn/...` work branch from the repository default branch or a selected source branch. Requires an organization owner/admin and a connected GitHub token.

```json
{ "branch_name": "solvyn/task-123", "from_branch": "main" }
```

### `POST /api/v1/repositories/{repository_id}/commits`

Create one commit containing up to 50 UTF-8 text files (500 KB total) on a `solvyn/...` branch. The request must include the expected current branch head; stale heads return `409`. The API rejects duplicate paths, traversal paths, and `.git` paths. Requires owner/admin access and a connected GitHub token.

```json
{
  "branch": "solvyn/task-123",
  "expected_head_sha": "current-branch-head-sha",
  "message": "Add the requested feature",
  "files": [{ "path": "src/feature.py", "content": "print('hello')\n" }]
}
```

### `POST /api/v1/repositories/{repository_id}/pull-requests`

Open a draft pull request from a `solvyn/...` branch to the repository default branch. Solvyn does not mark PRs ready for review or merge them; human review remains in GitHub.

### `GET /api/v1/repositories/{repository_id}/pull-requests?state=open&limit=50&page=1`

Fetch a live, paginated public GitHub pull request snapshot. `state` accepts `open`, `closed`, or `all`; `limit` is 1–100 and `page` is 1–100. The response is a point-in-time snapshot and does not persist GitHub records locally.

### `GET /api/v1/repositories/{repository_id}/pull-requests/{number}`

Inspect pull request metadata, the latest review decision per reviewer, requested reviewers, and a bounded unified diff (maximum 1 MB). The review summary scans up to 1,100 reviews and probes the next page; `history_truncated` is true and `state` is `incomplete_history` only when that probe finds additional reviews. Diff and metadata fields are marked `untrusted_repository_data`. Pull requests created by Solvyn remain drafts for human review; Solvyn does not merge them.

GitHub 404 responses map to not found, rate limits to 429, and provider/network failures to 502 or 504. Large or truncated trees return 413.

### `POST /api/v1/webhooks/github`

Configure `GITHUB_WEBHOOK_SECRET` and subscribe GitHub to `workflow_run`, `dependabot_alert`, `code_scanning_alert`, and `secret_scanning_alert`. Solvyn verifies `X-Hub-Signature-256`, requires a UUID delivery ID, caps payloads at 256 KB, and records failed workflow runs and open security alert activity for repositories linked to active projects. Duplicate delivery UUIDs and repeated source events are deduplicated. Stored event metadata is bounded; raw webhook payloads and secret values are not stored. Verified events appear in the organization's proactive-event inbox; triggers queue human triage and do not start an agent workflow automatically.

### `GET /api/v1/organizations/{organization_id}/proactive-events?limit=50`

List verified CI failure and security alert events for organization members. Each item includes generic `summary` and `severity` fields while retaining `workflow_name` and `conclusion` for compatibility. Event details are untrusted GitHub data.

### `POST /api/v1/organizations/{organization_id}/proactive-events/{event_id}/acknowledge`

Mark a proactive event acknowledged. Requires an organization owner or admin.

### `POST /api/v1/organizations/{organization_id}/maintenance-schedules`

Create an owner/admin-configured recurring trigger for code quality, dependency review, or documentation review. The project must belong to the organization. Intervals are limited to 1–30 days, and each project can have at most 10 enabled schedules. Due triggers add a reviewable proactive event; they do not automatically run an agent workflow.

### `GET /api/v1/organizations/{organization_id}/maintenance-schedules`

List an organization's maintenance schedules. Any organization member may read them.

### `PATCH /api/v1/organizations/{organization_id}/maintenance-schedules/{schedule_id}`

Enable or pause a schedule. Requires an organization owner or admin. Re-enabling starts a fresh interval.

---

## Observability

All observability endpoints are prefixed with `/api/v1/observability`.

### `GET /api/v1/observability/metrics`

Get current Prometheus metrics values (same data as `GET /metrics`).

**Auth required**: Yes (via observability router)

**Response** `200 OK`: Prometheus text format metrics.
```text
# HELP solvyn_http_requests_total Total HTTP requests
# TYPE solvyn_http_requests_total counter
solvyn_http_requests_total{method="GET",endpoint="/api/v1/health",status="200"} 42.0
...
```

**Note**: The raw `/metrics` endpoint (mounted directly on the FastAPI app) is also available without auth for Prometheus scraping.

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/observability/metrics
```

---

### `GET /api/v1/observability/traces`

List execution traces.

**Auth required**: Yes

**Query Parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | `string` (optional) | — | Filter traces by session |
| `limit` | `integer` | `20` | Maximum number of traces |

**Response** `200 OK`:
```json
{
    "traces": [
        {
            "id": "trace_0",
            "name": "product-manager execution",
            "agent_type": "product-manager",
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "tenant_id": "default",
            "metadata": {},
            "spans": [
                {
                    "name": "llm_generate",
                    "type": "llm",
                    "input": { "prompt_length": 500 },
                    "output": null,
                    "duration_ms": 1250.5,
                    "token_count": 800,
                    "model": "openai/gpt-4o",
                    "provider": "openrouter",
                    "error": null
                }
            ]
        }
    ],
    "count": 1
}
```

**Example**:
```bash
# List all traces
curl -H "X-API-Key: your-api-key" \
     "http://localhost:8000/api/v1/observability/traces?limit=10"

# Filter by session
curl -H "X-API-Key: your-api-key" \
     "http://localhost:8000/api/v1/observability/traces?session_id=550e8400-e29b-41d4-a716-446655440000"
```

---

### `GET /api/v1/observability/traces/{trace_id}`

Get a specific trace by ID.

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "trace": {
        "id": "trace_0",
        "name": "product-manager execution",
        "agent_type": "product-manager",
        "session_id": "...",
        "spans": [...]
    }
}
```

**Response** `200 OK` (not found):
```json
{
    "error": "Trace not found"
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/observability/traces/trace_0
```

---

### `GET /api/v1/observability/audit`

Query audit log entries.

**Auth required**: Yes

**Query Parameters**:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `action` | `string` (optional) | — | Filter by action type |
| `agent_type` | `string` (optional) | — | Filter by agent type |
| `session_id` | `string` (optional) | — | Filter by session |
| `limit` | `integer` | `50` | Maximum entries |

**Response** `200 OK`:
```json
{
    "entries": [
        {
            "action": "workflow_run",
            "agent_type": "product-manager",
            "session_id": "550e8400-e29b-41d4-a716-446655440000",
            "tenant_id": "default",
            "timestamp": "2024-01-15T10:30:00+00:00",
            "details": {
                "input_length": 500,
                "success": true
            },
            "id": "audit:uuid",
            "created_at": "2024-01-15T10:30:00+00:00"
        }
    ],
    "count": 1
}
```

**Example**:
```bash
# All entries
curl -H "X-API-Key: your-api-key" \
     "http://localhost:8000/api/v1/observability/audit?limit=20"

# Filtered
curl -H "X-API-Key: your-api-key" \
     "http://localhost:8000/api/v1/observability/audit?action=workflow_run&agent_type=product-manager&limit=10"
```

---

### `GET /api/v1/observability/analytics/session/{session_id}`

Get token usage and cost analytics for a specific session.

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "session_id": "550e8400-e29b-41d4-a716-446655440000",
    "totals": {
        "total_tokens": 45200,
        "total_cost": 0.1254,
        "prompt_tokens": 28000,
        "completion_tokens": 17200
    },
    "agent_breakdown": [
        {
            "agent_type": "product-manager",
            "tokens": 8500,
            "cost": 0.0235,
            "calls": 2
        },
        {
            "agent_type": "backend-engineer",
            "tokens": 15200,
            "cost": 0.0421,
            "calls": 3
        },
        {
            "agent_type": "code-reviewer",
            "tokens": 6500,
            "cost": 0.0180,
            "calls": 1
        }
    ]
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/observability/analytics/session/550e8400-e29b-41d4-a716-446655440000
```

---

### `GET /api/v1/observability/analytics/tenant/{tenant_id}`

Get aggregate token usage and cost for a tenant across all sessions.

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "tenant_id": "acme-corp",
    "totals": {
        "total_tokens": 1250000,
        "total_cost": 3.4521
    }
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/observability/analytics/tenant/default
```

---

### `GET /api/v1/observability/analytics/health`

Get system health analytics (not a health check — observability system status).

**Auth required**: Yes

**Response** `200 OK`:
```json
{
    "service": "solvyn",
    "version": "0.1.0",
    "timestamp": 1705312200.123,
    "tracing_enabled": false,
    "active_traces": 0
}
```

**Example**:
```bash
curl -H "X-API-Key: your-api-key" \
     http://localhost:8000/api/v1/observability/analytics/health
```

---

## WebSocket

### `WS /api/v1/observability/ws/{tenant_id}`

Real-time event stream for workflow and agent execution updates.

**Query Parameters**:

| Parameter | Type | Description |
|-----------|------|-------------|
| `session_id` | `string` (optional) | Filter events to a specific session |

### Event Types

#### `graph_update`

Sent when a node in the execution graph changes status:

```json
{
    "type": "graph_update",
    "graph_id": "uuid",
    "node_id": "uuid",
    "agent_type": "backend-engineer",
    "status": "completed",
    "error": null,
    "timestamp": "2024-01-15T10:30:01+00:00"
}
```

Possible statuses: `"pending"`, `"running"`, `"completed"`, `"failed"`.

#### `agent_message`

Sent when agents communicate with each other:

```json
{
    "type": "agent_message",
    "from_agent": "product-manager",
    "to_agent": "backend-engineer",
    "message_type": "task_assignment",
    "content_preview": "{...}",
    "timestamp": "2024-01-15T10:30:01+00:00"
}
```

#### `workflow_update`

Sent when workflow steps change status:

```json
{
    "type": "workflow_update",
    "workflow_name": "feature",
    "step": "backend-engineer",
    "status": "completed",
    "timestamp": "2024-01-15T10:30:01+00:00"
}
```

### Example (JavaScript client)

```javascript
const ws = new WebSocket(
  "ws://localhost:8000/api/v1/observability/ws/default?session_id=550e8400-e29b-41d4-a716-446655440000"
);

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  switch (data.type) {
    case "graph_update":
      console.log(`Node ${data.agent_type}: ${data.status}`);
      break;
    case "agent_message":
      console.log(`${data.from_agent} → ${data.to_agent}: ${data.message_type}`);
      break;
    case "workflow_update":
      console.log(`Workflow ${data.workflow_name} step ${data.step}: ${data.status}`);
      break;
  }
};

ws.onclose = () => console.log("WebSocket disconnected");
```

### Notes

- WebSocket connections are tenant-isolated by `tenant_id`
- Connections are keyed by `tenant_id:session_id`
- Failed sends automatically disconnect and clean up dead connections
- Client should send periodic pings (keepalive) — any text message is accepted

---

## Error Codes

| Code | Meaning | Common Causes |
|------|---------|---------------|
| `200` | OK | Successful request |
| `400` | Bad Request | Invalid JSON body, missing required fields |
| `401` | Unauthorized | Missing or invalid `X-API-Key` header |
| `404` | Not Found | Unknown workflow name, non-existent resource |
| `422` | Unprocessable Entity | Request body validation failure (Pydantic) |
| `429` | Too Many Requests | Rate limit exceeded (IP or tenant scope) |
| `500` | Internal Server Error | Unhandled server exception |

### Error Response Format

```json
{
    "error": "Human-readable error message",
    "detail": "Additional detail (optional, FastAPI-style)",
    "scope": "ip|tenant (only for 429)",
    "retry_after": 60
}
```

---

## Common Patterns

### Request

```bash
# Authenticated request with tenant context
curl -X <METHOD> \
     -H "X-API-Key: $SOLVYN_API_KEY" \
     -H "X-Tenant-Slug: acme-corp" \
     -H "Content-Type: application/json" \
     -d '<json-body>' \
     http://localhost:8000/api/v1/<endpoint>
```

### Response

All successful responses return JSON. Non-JSON endpoints are the Prometheus metrics (`/metrics`) and WebSocket connections.

### IDs

All resource IDs are UUIDv4 strings:
```
550e8400-e29b-41d4-a716-446655440000
```

### Timestamps

All timestamps are ISO 8601 with timezone offset:
```
2024-01-15T10:30:00+00:00
```

### Pagination

The API currently does not use cursor or offset pagination. List endpoints return limited results (50 for sessions, 20-50 for traces/audit). Pagination may be added in future versions.

### Versioning

The API is versioned by URL path prefix: `/api/v1/`. All endpoints documented here are v1.
