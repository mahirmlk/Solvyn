# Solvyn Architecture

> Your self-hosted AI engineering team — Architecture version 2.0

---

## Table of Contents

1. [High-Level Architecture](#high-level-architecture)
2. [System Components](#system-components)
3. [Data Flow & Request Lifecycle](#data-flow--request-lifecycle)
4. [Agent Lifecycle & Orchestration](#agent-lifecycle--orchestration)
5. [Multi-Tenant Isolation](#multi-tenant-isolation)
6. [Database Schema](#database-schema)
7. [LLM Routing & Fallback Strategy](#llm-routing--fallback-strategy)
8. [Workflow Automation Pipeline](#workflow-automation-pipeline)
9. [Observability Stack](#observability-stack)
10. [Technology Decisions](#technology-decisions)
11. [Directory Structure](#directory-structure)
12. [Repository Intelligence](#repository-intelligence)
13. [Sandbox Runtime](#sandbox-runtime)

---

## High-Level Architecture

Solvyn is an AI agent orchestration platform where 9 specialized AI agents collaborate as a virtual software team. It converts product requirements into architecture, generates tasks, writes and reviews code, creates tests, runs CI/CD, deploys, and monitors — all autonomously.

```
┌──────────────────────────────────────────────────────────────────┐
│                         CLI (Typer + Rich)                       │
│   status · agents · chat · run · workflows · pipeline           │
│   generate-tasks · traces · audit · list-sessions · providers   │
└───────────────────────────┬──────────────────────────────────────┘
                            │ HTTP / WebSocket
┌───────────────────────────▼──────────────────────────────────────┐
│                    FastAPI Application (main.py)                   │
│  ┌──────────────┐ ┌──────────────┐ ┌───────────────────────────┐ │
│  │ Auth MW      │ │ Rate Limiter │ │ Tenant Context MW         │ │
│  │ (X-API-Key)  │ │ (IP+Tenant)  │ │ (X-Tenant-Slug)           │ │
│  └──────────────┘ └──────────────┘ └───────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                    API Router v1                              │ │
│  │  /health  ·  /sessions  ·  /workflows  ·  /observability     │ │
│  └──────────────────────────────────────────────────────────────┘ │
│  ┌──────────────────────────────────────────────────────────────┐ │
│  │                 /metrics (Prometheus endpoint)                │ │
│  └──────────────────────────────────────────────────────────────┘ │
└───┬─────────────────┬──────────────────┬──────────────────────────┘
    │                 │                  │
    ▼                 ▼                  ▼
┌─────────┐   ┌──────────────┐   ┌──────────────┐
│ Agents  │   │  Workflows   │   │ Observability │
│ (9 sp.) │   │ (3 templates)│   │ (Metrics,     │
│         │   │              │   │  Traces,      │
│         │   │              │   │  Audit, WS)   │
└────┬────┘   └──────┬───────┘   └──────┬───────┘
     │               │                  │
     └───────┬───────┴──────────────────┘
             │
    ┌────────┴────────┐
    │                 │
    ▼                 ▼
┌────────┐     ┌──────────┐
│PostgreSQL│   │  Redis 7 │
│(pgvector)│   │ (Streams)│
└────────┘     └──────────┘
```

```
                ┌─────────────────────────────┐
                │       Solvyn Dashboard       │
                │  (Next.js 15 / React 19)    │
                │  /dashboard · /workflows    │
                │  /sessions · /analytics     │
                └─────────────┬───────────────┘
                              │ WebSocket (real-time)
                              ▼
```

---

## System Components

### 1. FastAPI Application (`solvyn/main.py`)

The application entry point. Uses the `lifespan` context manager for startup/shutdown lifecycle:

| Phase | Action |
|-------|--------|
| **Startup** | Connect Redis message bus, log startup message |
| **Shutdown** | Drain Redis connections, close LLM HTTP clients, dispose DB engine |

**Middleware stack** (applied in order):
1. `CORS` — configurable origins via `allowed_origins`
2. `auth_middleware` — validates `X-API-Key` header with SHA-256 HMAC comparison
3. `RateLimitMiddleware` — 100 req/min per IP, 500 req/min per tenant (sliding window)
4. `tenant_middleware` — resolves `X-Tenant-Slug` to tenant context via `contextvars`
5. `MetricsMiddleware` — records HTTP request count/duration for Prometheus

### 2. Agent System (`solvyn/agents/`)

Nine specialized AI agents, each extending `BaseAgent`:

| # | Agent Type | Capability | Role |
|---|-----------|------------|------|
| 1 | `product-manager` | PRODUCT_MANAGEMENT | Parse PRDs → epics, user stories, acceptance criteria |
| 2 | `system-designer` | SYSTEM_DESIGN, ARCHITECTURE | Architecture diagrams, tech stack, DB schema, API contracts |
| 3 | `backend-engineer` | CODE_GENERATION | FastAPI endpoints, SQLAlchemy models, business logic |
| 4 | `frontend-engineer` | CODE_GENERATION | React/TS components, hooks, state management |
| 5 | `qa-engineer` | TESTING | Unit/integration/E2E tests, coverage analysis |
| 6 | `code-reviewer` | CODE_REVIEW | Issues/Suggestions/Notes with severity scoring |
| 7 | `security-auditor` | SECURITY | OWASP Top 10, secrets detection, compliance |
| 8 | `devops-engineer` | DEVOPS | Dockerfiles, k8s manifests, CI/CD configs |
| 9 | `documentation-writer` | DOCUMENTATION | README, API docs, release notes |

#### Agent Lifecycle State Machine

```
IDLE ──► RUNNING ──► WAITING_APPROVAL ──► RUNNING ──► COMPLETED
  │         │                                    │
  │         │                                    │
  │         └────────────► FAILED ◄──────────────┘
  │                              │
  └──────────────────────────────┘ (reset)
```

- **IDLE**: Agent is initialized but not yet executing
- **RUNNING**: Agent is executing its task
- **WAITING_APPROVAL**: Agent has hit a checkpoint, waiting for human review
- **PAUSED**: Checkpoint was rejected; agent can be resumed with new input
- **COMPLETED**: Agent finished successfully
- **FAILED**: Agent encountered an unhandled exception

### 3. Agent Graph (`solvyn/agents/graph.py`)

The `AgentGraph` is a DAG (Directed Acyclic Graph) state machine that orchestrates multi-agent workflows:

- **Topological execution**: Nodes execute only when all upstream dependencies complete
- **.output resolution**: Node inputs can reference predecessor outputs (e.g., `"product-manager.output"`)
- **Failure propagation**: If an upstream node fails, all downstream nodes that depend on it are marked as failed
- **No parallel execution pools**: Execution is sequential but determined by topological ordering with parallel-with annotations

### 4. Registry Pattern (`solvyn/agents/registry.py`)

`AgentRegistry` provides centralized discovery and creation:

- `register(agent_class)` — registers an agent by its `agent_type` string
- `create(agent_type, ...)` — instantiates an agent by type with config
- `list_agents()` — returns all metadata for CLI listing
- `find_by_capability(capability)` — search agents by `AgentCapability` enum

### 5. Workflow System (`solvyn/workflows/`)

Three pre-built workflow templates (`WorkflowTemplate`):

| Workflow | Steps | Description |
|----------|-------|-------------|
| `feature` | 9 steps | PRD → Design → Backend + Frontend (parallel) → QA + Review + Security → DevOps → Docs |
| `bugfix` | 4 steps | Report → Analysis → Fix → Review → Deploy |
| `release` | 3 steps | Changelog + Security (parallel) → Deploy |

The `WorkflowExecutor` converts a template into an `AgentGraph`, executes it topologically, resolves inter-step inputs, and persists the execution graph to the database.

### 6. LLM Router (`solvyn/llm/router.py`)

Multi-provider routing with automatic fallback:

```
Primary: openrouter (default)
    ↓ fail
Fallback 1: groq
    ↓ fail
Fallback 2: deepseek
    ↓ fail
RuntimeError("All LLM providers failed")
```

Each provider implements the `LLMProvider` abstract class:
- **OpenRouter**: `https://openrouter.ai/api/v1` — supports GPT-4o, Claude 3.5 Sonnet, Gemini 2.0 Flash
- **Groq**: `https://api.groq.com/openai/v1` — supports Llama 3.3 70B, Llama 3.1 8B, Mixtral 8x7B
- **DeepSeek**: `https://api.deepseek.com/v1` — supports deepseek-chat, deepseek-reasoner

Cost tracking is per-million-tokens with provider-specific pricing tables built into each provider.

### 7. Message Bus (`solvyn/message_bus/redis.py`)

`RedisMessageBus` on Redis 7 Streams provides:

- **Pub/Sub**: `publish()` / `subscribe()` for real-time agent communication
- **Stream persistence**: `XADD` / `XREVRANGE` for durable message history per channel
- **Dead letter queue**: `add_to_dead_letter()` stores failed messages for inspection
- **Channel naming**: `agent:{agent_type}` for agent-specific, `session:{session_id}` for session-wide

### 8. Multi-Tenant Module (`solvyn/multi_tenant/`)

- `auth.py`: API key authentication middleware using SHA-256 HMAC constant-time comparison; public paths (`/health`, `/metrics`, `/docs`, `/openapi.json`) skip auth
- `middleware.py`: `TenantContext` via `contextvars` — resolves `X-Tenant-Slug` header; all DB queries are scoped by tenant

> **⚠️ HUMAN INPUT NEEDED**: Determine whether the `api_key_secret` model (single shared secret) is sufficient or if per-tenant API key hashing via the `Tenant.api_key_hash` field should be implemented.

---

## Data Flow & Request Lifecycle

### API Request Flow

```
1. Incoming HTTP request
   │
2. CORS middleware ─── validates origin
   │
3. Auth middleware ─── validates X-API-Key (skip public paths)
   │
4. Rate Limiter ────── check IP window (100/min) + tenant window (500/min)
   │
5. Tenant middleware ─ resolves X-Tenant-Slug → TenantContext (contextvars)
   │
6. Metrics middleware ─ start timer
   │
7. Route handler ───── delegates to agent/workflow/DB layer
   │
8. Metrics middleware ─ record duration + status code
   │
9. Response returned
```

### Workflow Execution Flow

```
1. Client POST /api/v1/workflows/{name}/run with inputs
   │
2. WorkflowExecutor.run(template_name, inputs, session_id, tenant_id)
   │
3. Template lookup from WORKFLOW_TEMPLATES dict
   │
4. AgentGraph construction:
   ├── For each WorkflowStep: add_node(agent_type, resolved_input)
   └── For each depends_on: add_edge(source, target)
   │
5. AgentGraph.execute() — topological walk:
   ├── Get start nodes (no incoming edges)
   ├── For each ready node:
   │   ├── AgentRegistry.create(agent_type)
   │   ├── Resolve .output references in inputs
   │   ├── agent.run(resolved_input)
   │   └── Collect output
   └── Propagate failures to downstream
   │
6. Persist execution graph to ExecutionGraph model
   │
7. Return result with node statuses
```

---

## Agent Lifecycle & Orchestration

Each agent (`BaseAgent`) follows this execution model:

```python
async def run(self, input_data: dict) -> dict:
    self.status = RUNNING
    self.execution_node = ExecutionNode(...)
    try:
        output = await self.execute(input_data)  # abstract — agent-specific logic
        self.status = COMPLETED
        return output
    except Exception as e:
        self.status = FAILED
        raise
```

The `execute()` method is abstract and implemented by each agent. Inside `execute()`, agents typically:

1. Extract relevant fields from `input_data`
2. Build a prompt with system instructions
3. Call `self.llm_generate(prompt, system_prompt=...)` → routes through `llm_router`
4. Parse the LLM response
5. Optionally call `self.request_approval()` to create a checkpoint

### Checkpoints (Human-in-the-Loop)

Agents can pause execution for human approval:

```python
# Agent calls this during execute()
checkpoint = await self.request_approval(
    summary="Generated 5 API endpoints. Approve to continue?",
    state_snapshot=self.state
)
# System persists checkpoint, returns to caller
# Human approves/rejects → agent.resume_from_checkpoint(resolution, approved)
```

Checkpoints are persisted to the `Memory` table with key prefix `checkpoint:`.

### Self-Healing Retry

`RetryHandler` wraps agent execution with exponential backoff:

- Default: 3 retries, 1s base delay, 30s max, 2x backoff factor
- On final failure: message routed to dead-letter queue via `DeadLetterHandler`

### Inter-Agent Communication

Agents communicate via Redis pub/sub:

- `AgentCommunication.send_message()` → publishes to `agent:{to_agent}` or `session:{session_id}`
- `AgentCommunication.broadcast_to_session()` → publishes to all agents in a session
- `AgentCommunication.handle_incoming()` → routes incoming messages to registered handlers

---

## Multi-Tenant Isolation

### Design

Isolation is achieved through `contextvars.ContextVar` — a context variable that flows through the entire request lifecycle without being passed explicitly:

```python
# Set at request start (tenant_middleware)
tenant_context.set(tenant_id=UUID(...), slug="acme-corp")

# Read anywhere in the call stack
tenant_id = tenant_context.tenant_id  # UUID or None
```

### Database Scoping

All database queries implicitly scope by tenant via relationships:
- `Tenant.id` → FK in `Session`, `AgentInstance`, `Memory`, `Artifact`, `Task`, `TokenUsage`
- Sessions API creates records with `tenant_id=tenant_context.tenant_id`

The `Memory` table uses tenant-scoped queries via `AgentMemory` which accepts `tenant_id` at construction.

### Limitations

> **⚠️ HUMAN INPUT NEEDED**: The current implementation uses a single shared `api_key_secret` for authentication, not per-tenant API keys. The `Tenant.api_key_hash` column exists but is not currently used in the auth middleware. Per-tenant auth is a future feature.

---

## Database Schema

All 9 models are SQLAlchemy ORM classes in `solvyn/db/models.py`. All use UUID primary keys and timezone-aware timestamps.

### Entity-Relationship Diagram

```
┌──────────┐
│  Tenant  │
│  id (PK) │──┐
│  name    │  │
│  slug    │  │
│  api_key │  │
│  is_actv │  │
└──────────┘  │
              │ 1:N
    ┌─────────┼──────────────────────────────┐
    │         │                              │
    ▼         ▼                              ▼
┌────────┐ ┌──────────────┐  ┌─────────────┐
│Session │ │AgentInstance │  │   Memory     │
│ id (PK)│ │ id (PK)      │  │  id (PK)     │
│tenant  │ │ tenant (FK)  │  │ tenant (FK)  │
│ title  │ │ agent_type   │  │ session(FK)  │
│ status │ │ name, config │  │ agent_type   │
└───┬────┘ │ status       │  │ key, value   │
    │      └──────────────┘  │ embedding    │
    │ 1:N                    └─────────────┘
    ├────► Message      (id, session, role, content, metadata)
    ├────► ExecutionGraph(id, session, graph_data, status)
    ├────► Artifact      (id, tenant, session, agent, type, name, content)
    ├────► Task          (id, tenant, session, title, status, priority)
    └────► TokenUsage    (id, tenant, session, agent, provider, model, tokens, cost)
```

### Table Details

| # | Table | Purpose | Key Columns |
|---|-------|---------|-------------|
| 1 | `tenants` | Multi-tenant isolation | `id` (UUID PK), `name`, `slug` (unique, indexed), `api_key_hash`, `is_active` |
| 2 | `sessions` | Group agent interactions | `id` (UUID PK), `tenant_id` (FK, indexed), `title`, `status` |
| 3 | `messages` | Conversation history | `id` (UUID PK), `session_id` (FK, indexed), `role`, `content`, `metadata` |
| 4 | `agent_instances` | Agent configuration per tenant | `id` (UUID PK), `tenant_id` (FK, indexed), `agent_type`, `name`, `config`, `status` |
| 5 | `memory` | Long-term agent memory (pgvector) | `id` (UUID PK), `tenant_id` (FK, indexed), `session_id`, `agent_type`, `key` (indexed), `value`, `embedding` |
| 6 | `artifacts` | Generated code/docs/configs | `id` (UUID PK), `tenant_id` (FK, indexed), `session_id`, `agent_type`, `artifact_type`, `name`, `content` |
| 7 | `tasks` | Work breakdown items | `id` (UUID PK), `tenant_id` (FK, indexed), `session_id`, `title`, `assigned_agent`, `status`, `priority`, `parent_task_id` (self-ref) |
| 8 | `execution_graphs` | DAG execution snapshots | `id` (UUID PK), `session_id` (FK, indexed), `graph_data` (JSON), `status` |
| 9 | `token_usage` | LLM cost/usage tracking | `id` (UUID PK), `tenant_id` (FK, indexed), `session_id`, `agent_type`, `provider`, `model`, `prompt/completion/total_tokens`, `cost` |

### Cascade Rules

- `Tenant` → cascades delete to `Session` and `AgentInstance`
- `Session` → cascades delete to `Message` and `ExecutionGraph`

---

## LLM Routing & Fallback Strategy

### Router Flow

```
llm_router.generate(prompt, system_prompt, provider=None, model=None, fallback=True)
    │
    ▼
provider = provider or settings.llm_default_provider  (default: "openrouter")
    │
    ▼
Try primary provider ─── success? → return LLMResponse(content, model, tokens, cost)
    │ fail
    ▼
Fallback through remaining providers in order:
    [openrouter → groq → deepseek] (excluding already-tried)
    │ all fail
    ▼
RuntimeError("All LLM providers failed")
```

### Provider Cost Tables

Token costs are per million tokens:

| Provider | Model | Prompt ($/M) | Completion ($/M) |
|----------|-------|-------------|-------------------|
| OpenRouter | `openai/gpt-4o` | $2.50 | $10.00 |
| OpenRouter | `openai/gpt-4o-mini` | $0.15 | $0.60 |
| OpenRouter | `anthropic/claude-3.5-sonnet` | $3.00 | $15.00 |
| OpenRouter | `google/gemini-2.0-flash-001` | $0.10 | $0.40 |
| Groq | `llama-3.3-70b-versatile` | $0.59 | $0.79 |
| Groq | `llama-3.1-8b-instant` | $0.05 | $0.08 |
| Groq | `mixtral-8x7b-32768` | $0.24 | $0.24 |
| DeepSeek | `deepseek-chat` | $0.27 | $1.10 |
| DeepSeek | `deepseek-reasoner` | $0.55 | $2.19 |

Default model: `openai/gpt-4o` via `openrouter`.

### Rate Limiting (LLM-specific)

The `RateLimiter` (`solvyn/agents/rate_limiter.py`) uses a token bucket algorithm:
- **Per-tenant LLM calls**: 10 calls/sec, burst 20 (configurable)
- **Per-agent execution**: 5 calls/sec, burst 10 (configurable)

> **⚠️ HUMAN INPUT NEEDED**: Rate limiting is in-memory only. Multi-worker deployments need a Redis-backed rate limiter for cross-process consistency. The existing `RateLimitMiddleware` (HTTP-level, sliding window) is also in-memory.

---

## Workflow Automation Pipeline

### End-to-End Flow

```
1. User provides a product specification
        │
2. TaskGenerator.generate_from_spec(spec)
   ├── Creates ProductManagerAgent
   ├── Runs with spec → gets JSON with epics/stories
   └── Parses into task list (json.loads or line-by-line fallback)
        │
3. TaskGenerator.build_workflow_graph(tasks)
   ├── Maps task keywords to agent types:
   │   api/database → backend-engineer
   │   ui → frontend-engineer
   │   test → qa-engineer
   │   deploy → devops-engineer
   │   doc → documentation-writer
   │   auth → security-auditor
   │   review → code-reviewer
   └── Creates linear DAG edges between tasks
        │
4. WorkflowExecutor.run(template_name, inputs)
   ├── Loads pre-built template (feature/bugfix/release)
   ├── Builds AgentGraph from template steps
   ├── Resolves input_map references (.output), .depends_on, .parallel_with
   ├── Executes DAG topologically
   └── Persists to ExecutionGraph DB record
```

### Parallel Execution

Steps annotated with `parallel_with` in the template do NOT have edges between them. The graph executor treats them as siblings — they can be visited in any topological order. Currently, execution is sequential (no async task pools), but the DAG structure supports future parallel execution:

```python
# From feature template: backend + frontend run in parallel after system-designer
WorkflowStep(agent_type="backend-engineer",   parallel_with=["frontend-engineer"], depends_on=["system-designer"])
WorkflowStep(agent_type="frontend-engineer",  parallel_with=["backend-engineer"], depends_on=["system-designer"])
```

---

## Observability Stack

### 1. Prometheus Metrics (8 metrics)

| Metric Name | Type | Labels |
|-------------|------|--------|
| `solvyn_http_requests_total` | Counter | method, endpoint, status |
| `solvyn_http_request_duration_seconds` | Histogram | method, endpoint |
| `solvyn_llm_calls_total` | Counter | provider, model, status |
| `solvyn_agent_executions_total` | Counter | agent_type, status |
| `solvyn_active_agents` | Gauge | — |
| `solvyn_workflow_runs_total` | Counter | workflow_name, status |
| `solvyn_memory_operations_total` | Counter | operation, agent_type |
| `solvyn_token_usage_total` | Counter | provider, model |

Exposed at `GET /metrics` (separate mount on the FastAPI app, not the API router).

### 2. Prometheus Alerts (7 rules)

| Alert | Severity | Condition |
|-------|----------|-----------|
| SolvynHighErrorRate | Critical | HTTP 5xx rate > 5% over 5min |
| SolvynHighLLMFailures | Warning | Any LLM call failures in 2min |
| SolvynAgentExecutionFailure | Warning | Any agent failure rate in 5min |
| SolvynHighTokenUsage | Info | >100K tokens/sec over 5min |
| SolvynNoActiveAgents | Info | Zero active agents for 10min |
| SolvynWorkflowFailures | Warning | Workflow failures in 5min |
| SolvynMemoryOperationsSlow | Warning | P95 memory op latency > 1s |

### 3. Tracing (In-Memory)

`TracingManager` provides in-memory trace/spans storage:
- `create_trace(name, agent_type, session_id)` → creates a trace with span list
- `add_span(trace_id, name, span_type, duration_ms, token_count, ...)` → records an execution span
- `get_trace(trace_id)`, `list_traces(limit)`, `get_session_traces(session_id)` → query

API endpoints: `GET /api/v1/observability/traces`, `GET /api/v1/observability/traces/{trace_id}`

### 4. Audit Logging

`AuditLogger` persists all agent actions to the `Memory` table with key prefix `audit:`:
- `log(action, agent_type, details, session_id, tenant_id)` → persists JSON record
- `search(action, agent_type, session_id, limit)` → filters and returns entries

API endpoint: `GET /api/v1/observability/audit?action=&agent_type=&session_id=&limit=`

### 5. WebSocket (Real-Time Updates)

`WebSocketManager` provides real-time event broadcasting:

- **Endpoint**: `WS /api/v1/observability/ws/{tenant_id}?session_id=`
- **Events**: `graph_update`, `agent_message`, `workflow_update`
- **Tenant isolation**: Connections are keyed by `tenant_id:session_id`
- **Dead connection cleanup**: Failed sends automatically disconnect

### 6. Cost & Usage Analytics

API endpoints (`/api/v1/observability/analytics/*`):
- `GET /analytics/session/{session_id}` — per-session token totals + per-agent breakdown
- `GET /analytics/tenant/{tenant_id}` — per-tenant aggregate totals
- `GET /analytics/health` — service status and trace counts

### 7. Structured Logging

`JsonFormatter` outputs all logs as JSON with `timestamp`, `level`, `name`, `message`, `exception` fields. Configurable via `LOG_LEVEL` and `json_logging` settings.

---

## Technology Decisions

| Decision | Rationale |
|----------|-----------|
| **FastAPI** | Native async support, automatic OpenAPI docs, Pydantic validation |
| **PostgreSQL + pgvector** | Single DB for relational data + vector embeddings; eliminates sync between separate vector store |
| **Redis 7 Streams** | Durable pub/sub with message history; dead letter queue support |
| **SQLAlchemy 2.0 async** | Mature ORM with async support via asyncpg driver |
| **ContextVars for tenant isolation** | Thread-safe, no explicit parameter threading; works naturally with async FastAPI |
| **Pydantic-settings** | Type-safe env var loading with `.env` file support |
| **Typer + Rich** | Rich CLI with formatted tables and colored output; auto-generated help |
| **Prometheus client** | Standard metrics format; Grafana dashboard included |
| **In-memory rate limiting** | Simplicity for single-worker deployments; Redis-backed needed for scaling |
| **AgentGraph DAG** | Topological ordering ensures correct execution order; supports parallel annotations |
| **Custom BaseAgent** | Lightweight — no heavy framework dependency; full control over lifecycle |
| **Multi-provider LLM routing** | Vendor independence; automatic fallback prevents single-provider outages |

---

## Directory Structure

```
solvyn/
├── solvyn/                         # Main Python package
│   ├── __init__.py                 # Version: 0.1.0
│   ├── main.py                     # FastAPI app factory + middleware + lifespan
│   ├── config.py                   # Pydantic settings (env vars)
│   ├── cli.py                      # Typer CLI (13 commands)
│   ├── logging_config.py           # JSON formatter + setup
│   │
│   ├── agents/                     # Agent implementations
│   │   ├── __init__.py             # Registers all 9 agents
│   │   ├── base.py                 # BaseAgent: run → execute lifecycle
│   │   ├── registry.py             # AgentRegistry: discovery + creation
│   │   ├── graph.py                # AgentGraph DAG executor
│   │   ├── schema.py               # Data models (AgentStatus, AgentCapability, etc.)
│   │   ├── checkpoint.py           # CheckpointManager: pause/approve/resume
│   │   ├── memory.py               # AgentMemory: pgvector-backed long-term memory
│   │   ├── retry.py                # RetryHandler + DeadLetterHandler
│   │   ├── communication.py        # AgentCommunication: Redis pub/sub messaging
│   │   ├── rate_limiter.py         # TokenBucket per-agent/per-LLM rate limiting
│   │   ├── cost_tracker.py         # Token usage + cost per session/tenant
│   │   ├── product_manager.py      # Product Manager agent
│   │   ├── system_designer.py      # System Designer agent
│   │   ├── backend_engineer.py     # Backend Engineer agent
│   │   ├── frontend_engineer.py    # Frontend Engineer agent
│   │   ├── qa_engineer.py          # QA Engineer agent
│   │   ├── code_reviewer.py        # Code Reviewer agent
│   │   ├── security_auditor.py     # Security Auditor agent
│   │   ├── devops_engineer.py      # DevOps Engineer agent
│   │   └── documentation_writer.py # Documentation Writer agent
│   │
│   ├── api/v1/                     # REST API endpoints
│   │   ├── health.py               # /health, /health/deep
│   │   ├── sessions.py             # GET/POST /sessions
│   │   └── workflows.py            # GET/POST /workflows, agents, generate-tasks
│   │
│   ├── db/                         # Database layer
│   │   ├── models.py               # 9 SQLAlchemy ORM models
│   │   ├── session.py              # Async engine, session factory, dependency
│   │   └── migrations/             # Alembic (asyncpg driver)
│   │
│   ├── llm/                        # LLM gateway
│   │   ├── router.py               # LLMRouter: multi-provider + fallback
│   │   └── providers/
│   │       ├── base.py             # LLMProvider ABC + LLMResponse dataclass
│   │       ├── openrouter.py       # OpenRouter API client
│   │       ├── groq.py             # Groq API client
│   │       └── deepseek.py         # DeepSeek API client
│   │
│   ├── message_bus/
│   │   └── redis.py                # RedisMessageBus: pub/sub + streams + dead-letter
│   │
│   ├── multi_tenant/
│   │   ├── auth.py                 # API key middleware + RateLimitMiddleware
│   │   └── middleware.py           # TenantContext (contextvars) + resolve_tenant
│   │
│   ├── observability/
│   │   ├── metrics.py              # 8 Prometheus metrics + MetricsMiddleware
│   │   ├── tracing.py              # TracingManager: in-memory trace/span storage
│   │   ├── audit.py                # AuditLogger: persist + search agent actions
│   │   ├── websocket.py            # WebSocketManager: real-time event broadcast
│   │   └── analytics_api.py        # Analytics + WebSocket endpoints
│   │
│   └── workflows/
│       ├── templates.py            # 3 workflows: feature, bugfix, release
│       ├── executor.py             # WorkflowExecutor: template → DAG → execute
│       └── task_generator.py       # TaskGenerator: spec → PM agent → tasks
│
├── frontend/                       # Next.js 15 dashboard
│   ├── src/
│   │   ├── app/                    # App Router (dashboard, workflows, sessions, analytics)
│   │   ├── components/             # AgentCard, ExecutionGraph, Sidebar
│   │   └── lib/api.ts              # Typed API client
│   └── Dockerfile                  # Multi-stage frontend build
│
├── k8s/                            # Kubernetes manifests
│   ├── namespace.yaml              # solvyn namespace
│   ├── configmap.yaml              # Non-secret env vars
│   ├── secrets.example.yaml        # Secret template (not committed)
│   ├── pvc.yaml                    # 50Gi persistent volume claim
│   ├── deployment.yaml             # API, DB, Redis deployments
│   ├── service.yaml                # ClusterIP services
│   ├── hpa.yaml                    # HPA: 2-10 replicas, CPU 70%, Mem 80%
│   └── ingress.yaml                # TLS ingress (nginx + cert-manager)
│
├── monitoring/
│   ├── prometheus-alerts.yml       # 7 alert rules
│   └── grafana-dashboard.json      # Dashboard with 8 panels
│
├── .github/workflows/
│   └── ci-cd.yml                   # lint → test → build → deploy-staging → deploy-prod
│
├── pyproject.toml                  # Python deps, ruff, mypy, pytest config
├── Makefile                        # dev, lint, test, docker, deploy targets
├── Dockerfile                      # Python 3.12-slim, pip install
├── docker-compose.yml              # API + pgvector/pg16 + Redis 7
├── alembic.ini                     # PostgreSQL + asyncpg
├── .pre-commit-config.yaml         # ruff, mypy, yaml/json/toml checks
└── .env.example                    # All config env vars
```

## Repository Intelligence

The Phase 2 repository provider boundary currently has a read-only GitHub implementation. Project members can connect public repositories and request a bounded default-branch inspection. The API derives file-path matches, language counts, direct dependencies, and a small project context from GitHub metadata and common manifests.

## Sandbox Runtime

Phase 3 now includes an internal Docker-backed runner and path-confined temporary workspaces (`solvyn/sandbox/`). Commands run in a fresh container with no network, a read-only root, a non-root UID, dropped capabilities, resource limits, bounded output, and a size-limited tmpfs workspace. Only the selected workspace is staged into the container; changed files are returned through a bounded archive that rejects symlinks and special files. The host Docker socket is used only by the trusted supervisor and is never mounted into the execution container.

This is an execution primitive, not yet an agent-facing tool. Repository cloning, project and teammate permission checks, human approval, persisted execution events, and task lifecycle integration remain future Phase 3 work. See [Sandbox Runtime](docs/SANDBOX_RUNTIME.md) for setup and the opt-in container test.

Provider responses are treated as untrusted repository data. This slice does not clone or execute repository code, mutate branches, or create pull requests. Private repository support requires a separate credential and permission design. See [the current scope and endpoint limits](docs/REPOSITORY_INTELLIGENCE.md).
