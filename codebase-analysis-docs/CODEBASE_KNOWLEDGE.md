# Solvyn Codebase Knowledge Base

> Complete architectural and technical reference — v0.1.0
> Generated: 2026-06-11

---

## Table of Contents

1. [High-Level Overview](#1-high-level-overview)
2. [System Architecture](#2-system-architecture)
3. [Technology Stack](#3-technology-stack)
4. [Data Model & Database Schema](#4-data-model--database-schema)
5. [Feature-by-Feature Analysis](#5-feature-by-feature-analysis)
6. [Data Flow Diagrams](#6-data-flow-diagrams)
7. [Cross-Cutting Concerns](#7-cross-cutting-concerns)
8. [Things You Must Know Before Changing Code](#8-things-you-must-know-before-changing-code)
9. [Technical Reference](#9-technical-reference)
10. [Open Questions & Risks](#10-open-questions--risks)

---

## 1. High-Level Overview

### Purpose

Solvyn is an **AI Operating Center for Engineering Teams** — an autonomous multi-agent system where 9 specialized AI agents collaborate to convert product requirements into working software. It handles architecture, task generation, code writing, review, testing, security auditing, deployment, and documentation — all orchestrated through a DAG-based workflow engine.

### Domain

AI agent orchestration / autonomous software engineering / multi-agent collaboration.

### Key Capabilities

| Capability | Implementation |
|---|---|
| 9 specialized agents | PM, System Designer, Backend, Frontend, QA, Code Reviewer, Security Auditor, DevOps, Documentation Writer |
| DAG-based workflow orchestration | `AgentGraph` with topological parallel execution |
| Multi-LLM provider fallback | OpenRouter → Groq → DeepSeek with automatic failover |
| Persistent long-term memory | pgvector-powered semantic recall with tenant scoping |
| Human-in-the-loop checkpoints | Agents pause at decision gates; state snapshots for resume |
| Multi-tenant isolation | `contextvars`-based tenant scoping through full request lifecycle |
| Real-time observability | WebSocket graph viz, 8 Prometheus metrics, structured JSON logging, audit trail |
| LLM response caching | Two-tier cache (in-memory + Redis) with TTL |
| Content moderation & PII scrubbing | Regex-based guardrails for toxic content and sensitive data |
| A2A (Agent-to-Agent) protocol | Versioned, envelope-based inter-agent messaging via Redis Streams |
| MCP (Model Context Protocol) tool registry | Discoverable, schema-validated tool execution with auth and rate limiting |
| Agent output evaluation | JSON schema validation, code quality scoring, hallucination detection |

### Architecture Type

**Monolithic backend** (FastAPI + async Python) with a **separate frontend** (Next.js 15). The agent runtime is in-process; there's no microservice decomposition. Redis Streams and PostgreSQL serve as the message bus and persistence layer respectively.

---

## 2. System Architecture

### Component Map

```mermaid
graph TB
    subgraph "Client Layer"
        CLI["CLI<br/>Typer + Rich<br/>13 commands"]
        WEB["Web Dashboard<br/>Next.js 15 / React 19"]
    end

    subgraph "FastAPI Application"
        MW["Middleware Stack<br/>CORS → Auth → RateLimit<br/>→ Tenant → Metrics"]
        API["API Router v1<br/>health · sessions · workflows<br/>observability"]
    end

    subgraph "Agent Runtime"
        REG["AgentRegistry<br/>9 agents"]
        GRAPH["AgentGraph<br/>DAG executor<br/>parallel nodes"]
        CHECKPOINT["CheckpointManager<br/>human approval gates"]
        RETRY["RetryHandler<br/>exp backoff · DLQ"]
        COMM["AgentCommunication<br/>Redis pub/sub + A2A"]
        COST["CostTracker<br/>per-session/tenant"]
    end

    subgraph "Guardrails"
        MODERATOR["ContentModerator<br/>toxic + bias + injection"]
        PII["PIIScrubber<br/>12 PII patterns"]
        VALIDATOR["OutputValidator<br/>JSON + schema + PII"]
    end

    subgraph "LLM Gateway"
        ROUTER["LLMRouter<br/>fallback chain"]
        CACHE["LLMResponseCache<br/>in-memory + Redis"]
        REGISTRY["ModelRegistry<br/>9 models, cost/quality"]
        OR["OpenRouter"]
        GQ["Groq"]
        DS["DeepSeek"]
        EMBED["EmbeddingService<br/>API + hash fallback"]
    end

    subgraph "Data Layer"
        PG[("PostgreSQL 16<br/>+ pgvector")]
        REDIS[("Redis 7<br/>Streams · Cache · Pub/Sub")]
    end

    subgraph "Supporting Systems"
        A2A["A2ABroker<br/>versioned envelope protocol"]
        MCP["MCPServerManager<br/>tool discovery + invocation"]
        EVAL["AgentEvaluator<br/>output quality scoring"]
        PROMPTS["PromptRegistry<br/>Jinja2 templates"]
    end

    subgraph "Observability"
        METRICS["Prometheus (8 metrics)"]
        TRACING["TracingManager<br/>in-memory"]
        AUDIT["AuditLogger<br/>Memory table"]
        WS["WebSocketManager<br/>real-time events"]
        OTLP["OpenTelemetry<br/>OTLP exporter"]
    end

    CLI & WEB --> MW --> API
    API --> REG --> GRAPH
    GRAPH --> CHECKPOINT & RETRY & ROUTER
    REG --> COMM
    ROUTER --> CACHE --> REDIS
    ROUTER --> OR & GQ & DS
    REG --> PG
    COMM --> A2A --> REDIS
    GRAPH --> PG
    API --> METRICS & TRACING & AUDIT & WS

    style ROUTER fill:#0f3460,stroke:#e94560
    style GRAPH fill:#0f3460,stroke:#f5a623
    style PG fill:#1a1a2e,stroke:#4169E1
    style REDIS fill:#1a1a2e,stroke:#DC382D
```

### Middleware Pipeline Order

```
Request → CORS → AuthMiddleware → RateLimitMiddleware → TenantMiddleware → MetricsMiddleware → Route Handler
```

Each middleware is an ASGI middleware added via `app.middleware("http")()`. They execute in reverse registration order for request, forward for response. The actual order in `main.py` is:

1. `CORSMiddleware` (FastAPI built-in)
2. `MetricsMiddleware` (outermost custom)
3. `tenant_middleware` (resolves `X-Tenant-Slug` → `contextvars`)
4. `RateLimitMiddleware` (per-IP + per-tenant sliding window)
5. `auth_middleware` (validates `X-API-Key`)

---

## 3. Technology Stack

### Backend

| Component | Technology | Version | Purpose |
|---|---|---|---|
| Framework | FastAPI | 0.115+ | Async API server with OpenAPI docs |
| Runtime | Uvicorn | 0.32+ | ASGI server with hot reload |
| Language | Python | 3.12+ | Primary backend language |
| ORM | SQLAlchemy 2.0 | 2.0.36+ | Async ORM with `asyncpg` driver |
| Database | PostgreSQL 16 | pg16 | Relational data + vector embeddings |
| Vector Extension | pgvector | 0.3.0+ | Semantic memory similarity search |
| Migrations | Alembic | 1.14+ | Schema versioning with asyncpg |
| Cache/Broker | Redis 7 | 5.2+ (`redis[hiredis]`) | Streams, pub/sub, LLM cache, dead-letter |
| HTTP Client | httpx | 0.28+ | Async HTTP for LLM API calls |
| CLI | Typer + Rich | 0.15+ / 13.9+ | 13-command CLI with formatted output |
| Templates | Jinja2 | 3.1+ | Prompt template rendering |
| Metrics | prometheus_client | 0.21+ | 8 metrics + `/metrics` endpoint |
| Settings | pydantic-settings | 2.6+ | Type-safe env var loading |
| Embeddings | pgvector + OpenAI API | text-embedding-3-small | 1536-dim vector embeddings |

### Frontend

| Component | Technology | Version |
|---|---|---|
| Framework | Next.js (App Router) | 15.1+ |
| UI Library | React | 19.0+ |
| CSS | Tailwind CSS | 4.0+ |
| Language | TypeScript | 5.7+ |

### DevOps

| Component | Technology |
|---|---|
| Containerization | Docker (Python 3.12-slim) |
| Orchestration | Docker Compose (dev), Kubernetes (prod) |
| CI/CD | GitHub Actions (lint → test → build → staging → prod) |
| Monitoring | Prometheus + Grafana (8-panel dashboard) |
| Pre-commit | Ruff, MyPy, YAML/JSON/TOML checks |

### Quality Tools

| Tool | Config | Purpose |
|---|---|---|
| Ruff | `line-length=160`, rules `E,F,I,N,W,UP` | Linting + import sorting |
| MyPy | `strict=true`, Python 3.12 | Static type checking |
| Pytest | `asyncio_mode="auto"`, `pytest-cov` | Test suite with coverage |
| Pre-commit | 8 hooks | Code quality gate |

---

## 4. Data Model & Database Schema

### 9 SQLAlchemy Models (in `solvyn/db/models.py`)

```mermaid
erDiagram
    TENANTS ||--o{ SESSIONS : has
    TENANTS ||--o{ AGENT_INSTANCES : has
    TENANTS ||--o{ MEMORY : has
    TENANTS ||--o{ ARTIFACTS : has
    TENANTS ||--o{ TASKS : has
    TENANTS ||--o{ TOKEN_USAGE : has
    SESSIONS ||--o{ MESSAGES : has
    SESSIONS ||--o{ EXECUTION_GRAPHS : has
    SESSIONS ||--o{ MEMORY : has
    SESSIONS ||--o{ TASKS : has
    SESSIONS ||--o{ TOKEN_USAGE : has
    TASKS ||--o{ TASKS : self_ref

    TENANTS {
        uuid id PK
        string name
        string slug UK "indexed, unique"
        string api_key_hash
        string role "admin|user|viewer"
        bool is_active
        datetime created_at
        datetime updated_at
    }

    SESSIONS {
        uuid id PK
        uuid tenant_id FK "indexed"
        string title
        string status "active|completed"
        datetime created_at
        datetime updated_at
    }

    MESSAGES {
        uuid id PK
        uuid session_id FK "indexed"
        string role
        text content
        text metadata
        datetime created_at
    }

    AGENT_INSTANCES {
        uuid id PK
        uuid tenant_id FK "indexed"
        string agent_type
        string name
        text config
        string status
        datetime created_at
        datetime updated_at
    }

    MEMORY {
        uuid id PK
        uuid tenant_id FK "indexed"
        uuid session_id FK "nullable"
        string agent_type
        string key UK "(tenant, agent_type, key)"
        text value
        vector_1536 embedding "pgvector"
        datetime created_at
    }

    ARTIFACTS {
        uuid id PK
        uuid tenant_id FK "indexed"
        uuid session_id FK "nullable"
        string agent_type
        string artifact_type
        string name
        text content
        datetime created_at
    }

    TASKS {
        uuid id PK
        uuid tenant_id FK "indexed"
        uuid session_id FK "nullable"
        string title
        text description
        string assigned_agent
        string status
        int priority
        uuid parent_task_id FK "self-ref"
        datetime created_at
        datetime updated_at
    }

    EXECUTION_GRAPHS {
        uuid id PK
        uuid session_id FK "indexed"
        text graph_data "JSON"
        string status
        datetime created_at
        datetime updated_at
    }

    TOKEN_USAGE {
        uuid id PK
        uuid tenant_id FK "indexed"
        uuid session_id FK "nullable"
        string agent_type
        string provider
        string model
        int prompt_tokens
        int completion_tokens
        int total_tokens
        decimal cost "Numeric(12,6)"
        datetime created_at
    }
```

### Key Schema Details

| Table | PK | Indexes | Cascade |
|---|---|---|---|
| `tenants` | UUID | `slug` (unique) | → `sessions`, `agent_instances` |
| `sessions` | UUID | `tenant_id` | → `messages`, `execution_graphs` |
| `memory` | UUID | `tenant_id`, `key` | Unique: `(tenant_id, agent_type, key)` |
| `token_usage` | UUID | `tenant_id` | — |

### Memory Table Multi-Purpose

The `memory` table is used for three distinct purposes, discriminated by key prefix:
- `memory:<key>` — Agent long-term memory
- `checkpoint:<uuid>` — Human-in-the-loop state snapshots
- `audit:<uuid>` — Agent action audit log

This is a deliberate design choice to avoid table proliferation. The `embedding` column (Vector 1536) is only populated for actual memory entries.

### UUID Default for "default" Tenant

When `tenant_id` is the string `"default"`, it's converted to `uuid.UUID("00000000-0000-0000-0000-000000000000")`. This is hardcoded in multiple modules (`memory.py`, `checkpoint.py`, `audit.py`, `cost_tracker.py`).

---

## 5. Feature-by-Feature Analysis

### 5.1 Agent System

**Entry point**: `solvyn/agents/__init__.py` → `solvyn/agents/base.py`

**Business purpose**: Execute specialized AI tasks through LLM-powered agents that collaborate like a real software team.

#### Component Stack

```
AgentRegistry (discovery + factory)
  → BaseAgent (lifecycle: IDLE → RUNNING → COMPLETED/FAILED)
    → ConcreteAgent (9 implementations)
      → llm_generate() → LLMRouter → LLMProvider
      → request_approval() → CheckpointManager → Memory table
      → AgentMemory (pgvector semantic recall)
      → CostTracker (TokenUsage table)
      → AgentCommunication (Redis pub/sub)
```

#### Agent Lifecycle

```
IDLE → RUNNING → (WAITING_APPROVAL → RUNNING) → COMPLETED
                     ↓ (rejected)
                   PAUSED
                     ↓ (unhandled)
                   FAILED
```

**Key files**:
- `solvyn/agents/base.py` — `BaseAgent` ABC with `run()`, `execute()`, `llm_generate()`, `request_approval()`
- `solvyn/agents/registry.py` — `AgentRegistry` singleton (`agent_registry`)
- `solvyn/agents/schema.py` — `AgentStatus`, `AgentCapability`, `AgentMetadata`, `ExecutionNode`, `ExecutionEdge`, `ExecutionGraph`
- `solvyn/agents/graph.py` — `AgentGraph` DAG executor with parallel execution

#### Agent Implementations

| Agent | File | System Prompt Focus | Input Keys | Output Keys |
|---|---|---|---|---|
| `product-manager` | `product_manager.py` | PRD parsing, epics, stories, acceptance criteria | `requirement`, `text`, `context` | `analysis`, `provider`, `model`, `cost` |
| `system-designer` | `system_designer.py` | Architecture, tech stack, DB schema, API contracts | `requirements` | Architecture diagrams |
| `backend-engineer` | `backend_engineer.py` | FastAPI endpoints, SQLAlchemy, Alembic, SOLID | `specification`, `language`, `framework` | `code`, `provider`, `model`, `cost` |
| `frontend-engineer` | `frontend_engineer.py` | React/TS components, hooks, state management | `design_spec` | Frontend code |
| `qa-engineer` | `qa_engineer.py` | Unit/integration/E2E tests, coverage, mocks | `code` | Test code |
| `code-reviewer` | `code_reviewer.py` | Static analysis, issues/suggestions/notes, severity | `code` | Review findings |
| `security-auditor` | `security_auditor.py` | OWASP Top 10, secrets, auth, injection, compliance | `code` | Security report |
| `devops-engineer` | `devops_engineer.py` | Dockerfiles, k8s, CI/CD, Terraform | `project_type` | Infrastructure config |
| `documentation-writer` | `documentation_writer.py` | README, API docs, release notes | `source`, `doc_type` | Documentation |

#### AgentGraph DAG Execution

**File**: `solvyn/agents/graph.py`

Key behaviors:
1. **Topological levels** — Nodes are grouped by level; nodes within the same level run concurrently via `asyncio.gather`
2. **Output resolution** — Node inputs can reference predecessor outputs via `"agent_type.output"` or `{"$node_output": "<node_id>"}`
3. **Failure propagation** — If an upstream node fails, all downstream dependents are marked `skipped`
4. **Cycle breaking** — If circular dependency is detected, one node is picked to break the cycle (logged as warning)
5. **Single-node optimization** — If only one node is runnable at a level, it runs directly without `gather` overhead

### 5.2 Workflow System

**Entry point**: `solvyn/workflows/templates.py` → `solvyn/workflows/executor.py`

**Business purpose**: Orchestrate multi-agent pipelines for feature delivery, bug fixes, and releases.

#### 3 Pre-built Templates

| Workflow | Steps | DAG Shape |
|---|---|---|
| `feature` | 9 | PM → Designer → [Backend ∥ Frontend] → [QA + Review + Security] → DevOps → Docs |
| `bugfix` | 4 | QA → Backend → Review + DevOps (parallel) |
| `release` | 3 | [Docs ∥ Security] → DevOps |

#### WorkflowExecutor Flow

```
1. Lookup template by name
2. Merge user inputs with template defaults
3. Build AgentGraph:
   - For each WorkflowStep: add_node(agent_type, resolved_input)
   - For each depends_on: add_edge(source, target)
4. Execute graph topologically (parallel where possible)
5. Post-execution: re-resolve inputs with runtime outputs
6. Persist ExecutionGraph to DB
```

**Key detail**: `_resolve_inputs()` supports comma-separated values for combining outputs from multiple upstream agents (e.g., `"backend-engineer.output,frontend-engineer.output"` → joined with `\n\n`, each truncated to 500 chars).

### 5.3 LLM Gateway

**Entry point**: `solvyn/llm/router.py`

**Business purpose**: Route LLM requests through multiple providers with automatic fallback, caching, and cost tracking.

#### LLMRouter Architecture

```
LLMRouter
├── providers: dict[str, LLMProvider]  # openrouter, groq, deepseek
├── registry: ModelRegistry             # 9 models with metadata
└── cache: LLMResponseCache            # in-memory + Redis
```

#### Fallback Chain

```
Primary (default: openrouter)
  ↓ ConnectionError/TimeoutError
Fallback 1 (groq)
  ↓
Fallback 2 (deepseek)
  ↓
RuntimeError("All LLM providers failed")
```

**Note**: Fallback only triggers on `ConnectionError`, `TimeoutError`, and `httpx.TimeoutException`. Other errors (e.g., 400 Bad Request) are NOT retried with fallback.

#### LLMResponseCache (Two-Tier)

1. **In-memory** — `dict[str, tuple[LLMResponse, float]]` with 10K max entries; LRU eviction at 50% capacity
2. **Redis** — `llm_cache:<sha256_key>` with configurable TTL (default 3600s)

Cache key: `SHA256(prompt + ":" + model + ":" + system_prompt)`

#### ModelRegistry

9 registered models with metadata: `provider`, `model`, `cost_per_1m_prompt`, `cost_per_1m_completion`, `context_window`, `latency_class`, `quality_class`. Supports `find_cheapest()`, `find_fastest()`, `find_by_quality()`.

#### Embedding Service

**File**: `solvyn/llm/embeddings.py`

Two providers:
1. **OpenAIEmbeddingProvider** — Uses OpenRouter or OpenAI API for `text-embedding-3-small` (1536 dimensions)
2. **HashEmbeddingProvider** — Deterministic SHA-512 based fallback for dev/testing (unit-normalized vectors)

The `EmbeddingService` auto-selects: API if key configured, hash fallback otherwise. Supports Redis-cached embeddings via `embed_with_cache()`.

### 5.4 Multi-Tenant System

**Entry point**: `solvyn/multi_tenant/middleware.py` + `solvyn/multi_tenant/auth.py`

**Business purpose**: Isolate data and access across tenants using context variables.

#### Tenant Context

```python
class TenantContext:
    _tenant_id: contextvars.ContextVar[UUID | None]
    _tenant_slug: contextvars.ContextVar[str | None]
    _tenant_role: contextvars.ContextVar[str]  # default: VIEWER
```

Set per-request by `tenant_middleware` via `X-Tenant-Slug` header lookup.

#### Auth Flow

```
1. Skip public paths (/health, /metrics, /docs, /openapi.json)
2. Extract X-API-Key header
3. If X-Tenant-Slug present:
   - Query all active tenants, compare SHA-256 hash of provided key against api_key_hash
   - Set tenant context with tenant's role
4. Else (shared-secret mode):
   - Compare hash against settings.api_key_secret
5. Forward request
```

**Important**: The per-tenant auth (`_validate_tenant_api_key`) queries ALL active tenants and iterates through them. This is O(n) where n = number of tenants. For production, consider an index on `api_key_hash` or a lookup table.

#### RBAC

`RequireRole` dependency class for route-level role enforcement. `TenantRole` enum: `ADMIN`, `USER`, `VIEWER`.

### 5.5 Observability

**Entry point**: `solvyn/observability/` (metrics, tracing, audit, websocket, analytics_api)

**Business purpose**: Monitor system health, track LLM costs, audit agent actions, and provide real-time updates.

#### 8 Prometheus Metrics

| Metric | Type | Labels |
|---|---|---|
| `solvyn_http_requests_total` | Counter | method, endpoint, status |
| `solvyn_http_request_duration_seconds` | Histogram | method, endpoint |
| `solvyn_llm_calls_total` | Counter | provider, model, status |
| `solvyn_agent_executions_total` | Counter | agent_type, status |
| `solvyn_active_agents` | Gauge | — |
| `solvyn_workflow_runs_total` | Counter | workflow_name, status |
| `solvyn_memory_operations_total` | Counter | operation, agent_type |
| `solvyn_token_usage_total` | Counter | provider, model |

#### Tracing

`TracingManager` — in-memory deque (maxlen=10,000 traces). Each trace has spans with name, type, duration_ms, token_count, model, provider.

**Limitation**: Traces are lost on process restart. Not persistent.

#### Audit Logging

All agent actions persisted to `Memory` table with `audit:` prefix. Searchable by action, agent_type, session_id.

#### WebSocket

`ConnectionManager` keys connections by `tenant_id:session_id`. Supports three event types: `graph_update`, `agent_message`, `workflow_update`. Dead connections auto-cleaned on send failure.

#### OpenTelemetry

Optional OTLP exporter integration. Disabled by default (`otel_enabled=False`). Falls back to `_NoOpTracer` when not configured.

### 5.6 Guardrails

**Business purpose**: Filter and sanitize content before persistence or downstream use.

#### ContentModerator (`content_moderator.py`)

- 8 toxic patterns (violence, slurs, sexual violence, self-harm, hate speech, doxxing, injection)
- 3 bias patterns (gender, racial, age)
- Severity levels: `LOW`, `MEDIUM`, `HIGH`, `CRITICAL`
- Actions: `BLOCK` (raise `ModerationError`), `FLAG` (log), `PASS` (noop)
- Default threshold: `HIGH` — only HIGH and CRITICAL trigger the action

#### PIIScrubber (`pii_scrubber.py`)

12 PII patterns: email, phone, SSN, credit card, IP, AWS keys, private keys, JWT, generic API keys, passwords, IPv6. Replaces matches with `[REDACTED_*]` placeholders.

#### OutputValidator (`output_validator.py`)

Chain: `scrub PII → extract JSON → validate schema → return`. Supports markdown code block extraction for JSON.

### 5.7 A2A (Agent-to-Agent) Protocol

**Business purpose**: Standardized, versioned inter-agent messaging.

**Files**: `solvyn/a2a/protocol.py`, `solvyn/a2a/broker.py`

Message types: `TASK_REQUEST`, `TASK_RESPONSE`, `STATUS_UPDATE`, `ERROR_REPORT`, `DELEGATION`

Envelope fields: `message_id`, `message_type`, `from_agent`, `to_agent`, `session_id`, `tenant_id`, `correlation_id`, `version`, `timestamp`, `metadata`

The `A2ABroker` routes messages via Redis pub/sub channels (`agent:<type>` or `session:<id>`).

### 5.8 MCP (Model Context Protocol) Tool System

**Business purpose**: Discoverable, schema-validated tool execution with auth and stats tracking.

**Files**: `solvyn/mcp/server.py`, `solvyn/mcp/tool.py`

`MCPServerManager` — registry of `MCPTool` instances with schema validation, parameter validation, auth requirements, and per-tool statistics (call count, error count, avg latency).

### 5.9 Agent Output Evaluation

**Business purpose**: Score agent output quality against ground truth.

**File**: `solvyn/eval/evaluator.py`

4 evaluation types:
1. `json_schema` — Checks required keys in JSON output
2. `length` — Min/max length bounds
3. `no_hallucination` — Forbidden phrase detection
4. `code_quality` — Security checks (eval/exec, SQL injection, hardcoded passwords, broad exceptions, TODO/FIXME)

### 5.10 Prompt Registry

**Business purpose**: Versioned, Jinja2-based prompt management.

**File**: `solvyn/prompts/registry.py`

Supports multiple versions per prompt, active version switching, and template rendering with variables.

### 5.11 Message Bus

**Business purpose**: Inter-process communication via Redis.

**File**: `solvyn/message_bus/redis.py`

- `publish()` — Redis `PUBLISH` + `XADD` to `stream:<channel>`
- `subscribe()` — Redis `SUBSCRIBE` with async iteration
- `send_to_agent()` — Channel `agent:<agent_id>`
- `send_to_session()` — Channel `session:<session_id>`
- `add_to_dead_letter()` — `XADD` to `dead_letter` stream

### 5.12 Rate Limiting

**Two layers**:

1. **HTTP Rate Limiting** (`solvyn/multi_tenant/auth.py` → `RateLimitMiddleware`): Redis-backed sliding window via `redis_rate_limiter` — 100 req/min per IP, 500 req/min per tenant
2. **LLM/Agent Rate Limiting** (`solvyn/agents/rate_limiter.py`): In-memory token bucket — 10 calls/sec per tenant LLM, 5 calls/sec per agent

### 5.13 CLI

**Business purpose**: Full-featured command-line interface.

**File**: `solvyn/cli.py`

13 commands: `status`, `agents`, `chat`, `run`, `workflows`, `pipeline`, `generate-tasks`, `traces`, `audit`, `list-sessions`, `providers`, `version`, `serve`

Entry point: `solvyn.cli:app` (registered as `solvyn` in `pyproject.toml`)

---

## 6. Data Flow Diagrams

### 6.1 API Request Flow

```mermaid
sequenceDiagram
    participant Client
    participant CORS
    participant Auth
    participant RateLimit
    participant Tenant
    participant Metrics
    participant Route

    Client->>CORS: HTTP Request
    CORS->>Auth: Origin check
    Auth->>RateLimit: X-API-Key validated
    RateLimit->>Tenant: IP + tenant limits checked
    Tenant->>Metrics: contextvars set
    Metrics->>Route: Timer started
    Route-->>Metrics: Response + duration
    Metrics-->>Client: HTTP Response
```

### 6.2 Workflow Execution Flow

```mermaid
sequenceDiagram
    participant Client
    participant API
    participant Executor
    participant Graph
    participant Agent
    participant LLM
    participant DB

    Client->>API: POST /workflows/feature/run
    API->>Executor: run(template, inputs)
    Executor->>Graph: Build DAG from template
    loop For each topological level
        par Independent nodes
            Graph->>Agent: Create via registry
            Agent->>LLM: llm_generate(prompt)
            LLM-->>Agent: LLMResponse
            Agent->>Agent: Process output
        end
        Graph->>Graph: Update node status
    end
    Graph->>DB: Persist ExecutionGraph
    Graph-->>Client: ExecutionGraph result
```

### 6.3 LLM Request with Caching

```mermaid
sequenceDiagram
    participant Agent
    participant Router
    participant Cache
    participant Redis
    participant Provider

    Agent->>Router: generate(prompt, model)
    Router->>Cache: Check local cache
    alt Cache hit (local)
        Cache-->>Router: LLMResponse
    else Cache miss
        Router->>Redis: Check Redis cache
        alt Redis hit
            Redis-->>Router: LLMResponse
        else Redis miss
            Router->>Provider: HTTP POST /chat/completions
            alt Provider success
                Provider-->>Router: LLMResponse
                Router->>Cache: Store (local + Redis)
            else Provider failure
                Provider-->>Router: ConnectionError
                Router->>Router: Try fallback provider
            end
        end
    end
    Router-->>Agent: LLMResponse
```

### 6.4 Agent Memory with Semantic Search

```mermaid
sequenceDiagram
    participant Agent
    participant Memory
    participant Embedding
    participant DB

    Agent->>Memory: search(query, agent_type)
    Memory->>Embedding: embed(query)
    alt API key configured
        Embedding->>Embedding: OpenAI API call
    else No API key
        Embedding->>Embedding: Hash-based fallback
    end
    Embedding-->>Memory: EmbeddingResponse
    Memory->>DB: pgvector cosine similarity
    DB-->>Memory: Ranked results
    Memory-->>Agent: Memory entries with similarity
```

---

## 7. Cross-Cutting Concerns

### 7.1 Authentication

- **Mechanism**: API key in `X-API-Key` header, SHA-256 hashed
- **Verification**: `hmac.compare_digest()` (constant-time)
- **Public paths**: `/api/v1/health`, `/metrics`, `/docs`, `/openapi.json`
- **Per-tenant mode**: Queries all active tenants, compares hash against `api_key_hash` column
- **Shared-secret mode**: Falls back to `settings.api_key_secret` comparison

### 7.2 Authorization

- **Tenant isolation**: `contextvars.ContextVar` flows through async call stack
- **RBAC**: `RequireRole` dependency for route-level enforcement
- **WebSocket**: Authenticated via `api_key` query param or `X-API-Key` header

### 7.3 Caching

| Layer | Location | TTL | Max Size |
|---|---|---|---|
| LLM Response (local) | `LLMResponseCache._local_cache` | Configurable (default 3600s) | 10,000 entries (50% eviction) |
| LLM Response (Redis) | `llm_cache:<key>` | Configurable (default 3600s) | Unlimited |
| Embedding (Redis) | `embedding:<key>` | Configurable (default 3600s) | Unlimited |

### 7.4 Logging

- **Format**: Structured JSON via `JsonFormatter`
- **Fields**: timestamp, level, name, message, exception
- **Configuration**: `LOG_LEVEL` + `JSON_LOGGING` env vars
- **Output**: stdout (no file rotation — rely on log aggregation)

### 7.5 Error Handling

- **API errors**: FastAPI `HTTPException` + `JSONResponse` with structured error bodies
- **Agent errors**: Caught in `BaseAgent.run()`, status set to `FAILED`, error logged
- **LLM errors**: Fallback chain; only `ConnectionError`/`TimeoutError` trigger fallback
- **Tool errors**: `ToolErrorHandler` maps to structured responses with retryability flags
- **Audit persistence**: Failures logged as warnings, don't block the request

### 7.6 Security Headers

Applied via custom middleware in `main.py`:
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `X-XSS-Protection: 1; mode=block`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` (HTTPS only)

---

## 8. Things You Must Know Before Changing Code

### 8.1 The `memory` Table is Multi-Purpose

The `memory` table stores **three different things** using key prefixes:
- `memory:*` — Agent long-term memory
- `checkpoint:*` — Human-in-the-loop state snapshots
- `audit:*` — Agent action audit log

**Impact**: Changing the `memory` table schema affects all three subsystems. The `embedding` column (Vector 1536) is only meaningful for `memory:*` entries.

### 8.2 UUID "default" Tenant

Multiple modules hardcode `uuid.UUID("00000000-0000-0000-0000-000000000000")` for the `"default"` tenant:
- `solvyn/agents/memory.py:15`
- `solvyn/agents/checkpoint.py:13`
- `solvyn/observability/audit.py:14`
- `solvyn/agents/cost_tracker.py:10`

Each has its own `_to_uuid()` helper. Changing one without the others causes inconsistency.

### 8.3 Rate Limiter is In-Memory

Both `RateLimitMiddleware` (HTTP) and `RateLimiter` (LLM/agent) store state in process memory. In multi-worker/multi-pod deployments, each process maintains independent counters. The HTTP middleware uses a Redis-backed implementation (`redis_rate_limiter`), but the LLM/agent `TokenBucket` is purely in-process.

### 8.4 AgentGraph is Not Truly Parallel in Production

Despite `asyncio.gather` usage, execution is bounded by the Python GIL for CPU-bound work. The parallel execution is effective for I/O-bound operations (LLM API calls) but won't speed up CPU-bound tasks.

### 8.5 LLM Fallback Only Catches Network Errors

The `LLMRouter.generate()` fallback only triggers on `ConnectionError`, `TimeoutError`, and `httpx.TimeoutException`. API errors like 400, 401, 429, 500 are NOT retried with fallback providers.

### 8.6 WorkflowExecutor Resolves Inputs Twice

`WorkflowExecutor.run()` calls `_resolve_inputs()` twice:
1. Before execution (to build initial node inputs)
2. After execution (to re-resolve with runtime outputs for persistence)

The second resolution is what gets persisted in the `ExecutionGraph`.

### 8.7 Audit Logger Query Inefficiency

`AuditLogger.search()` fetches all audit records matching the prefix, then filters `action` and `session_id` in Python (not SQL). For high-volume systems, this is a performance concern.

### 8.8 Per-Tenant API Key Validation is O(n)

`_validate_tenant_api_key()` in `auth.py` queries ALL active tenants and iterates through them, comparing SHA-256 hashes. No database index on `api_key_hash` exists. For many tenants, this becomes a bottleneck.

### 8.9 Graph Input Resolution Uses Agent Type, Not Node ID

The `AgentGraph._resolve_node_inputs()` method resolves `"agent_type.output"` references by finding the **last predecessor** with that agent type. If the same agent type appears multiple times in the graph, it picks the most recent one — which may not be the intended source.

### 8.10 TaskGenerator Keyword Matching is Naive

`TaskGenerator.build_workflow_graph()` assigns agent types by checking if any keyword appears in the task title. The keyword check is `in` (substring match), not word boundary. "frontend" matches "frontendend". The default agent is `backend-engineer`.

### 8.11 LLM Cache Key Doesn't Include Provider

The cache key is `SHA256(prompt + ":" + model + ":" + system_prompt)` — it does NOT include the provider name. If the same model is available from two providers with different costs/quality, the cached result from one provider will be served for the other.

### 8.12 CheckpointManager Doesn't Associate with Sessions

`CheckpointManager.save()` accepts `session_id` but the query in `list_for_session()` uses `Memory.session_id` which is nullable. Checkpoints may not be findable by session if `session_id` was not properly set during save.

### 8.13 Frontend API Client Missing Tenant Header

`frontend/src/lib/api.ts` sends `X-API-Key` but does NOT send `X-Tenant-Slug`. Multi-tenant functionality from the dashboard is broken unless the API key itself encodes tenant context.

---

## 9. Technical Reference

### 9.1 Glossary

| Term | Definition |
|---|---|
| **Agent** | An AI entity (e.g., `product-manager`) that executes specialized tasks via LLM calls |
| **AgentGraph** | A DAG that orchestrates multi-agent execution with topological ordering |
| **Checkpoint** | A human-in-the-loop gate where an agent pauses for approval before continuing |
| **A2A** | Agent-to-Agent protocol — standardized envelope-based inter-agent messaging |
| **MCP** | Model Context Protocol — discoverable, schema-validated tool system |
| **DLQ** | Dead Letter Queue — Redis stream for unprocessable messages |
| **TokenBucket** | In-memory rate limiting algorithm with refill and burst capacity |
| **ContextVar** | Python context variable that propagates through async call stacks |
| **pgvector** | PostgreSQL extension for vector similarity search |
| **ExecutionNode** | A single step in an AgentGraph (agent_type + input + output + status) |
| **WorkflowTemplate** | A pre-defined multi-step agent pipeline (feature/bugfix/release) |

### 9.2 Key Classes & Modules

| Module | Class | Purpose |
|---|---|---|
| `agents/base.py` | `BaseAgent` | Abstract agent with lifecycle, LLM, checkpoints |
| `agents/graph.py` | `AgentGraph` | DAG executor with parallel topological levels |
| `agents/registry.py` | `AgentRegistry` | Agent discovery and factory (singleton) |
| `agents/schema.py` | `ExecutionNode`, `ExecutionGraph`, `AgentStatus` | Core data models |
| `agents/memory.py` | `AgentMemory` | pgvector-backed semantic memory |
| `agents/checkpoint.py` | `CheckpointManager` | Human-in-the-loop state management |
| `agents/retry.py` | `RetryHandler`, `DeadLetterHandler` | Exponential backoff retry + DLQ |
| `agents/communication.py` | `AgentCommunication` | Redis pub/sub + A2A messaging |
| `agents/cost_tracker.py` | `CostTracker` | Per-session/tenant token + cost tracking |
| `agents/rate_limiter.py` | `RateLimiter`, `TokenBucket` | In-memory token bucket rate limiting |
| `llm/router.py` | `LLMRouter`, `ModelRegistry`, `LLMResponseCache` | Multi-provider LLM routing + caching |
| `llm/embeddings.py` | `EmbeddingService` | Vector embedding generation with fallback |
| `workflows/executor.py` | `WorkflowExecutor` | Template → DAG → Execute → Persist |
| `workflows/task_generator.py` | `TaskGenerator` | Spec → PM agent → Task breakdown |
| `multi_tenant/middleware.py` | `TenantContext` | contextvars-based tenant scoping |
| `multi_tenant/auth.py` | `auth_middleware`, `RateLimitMiddleware` | Auth + rate limiting |
| `observability/metrics.py` | `Metrics`, `MetricsMiddleware` | 8 Prometheus metrics |
| `observability/tracing.py` | `TracingManager` | In-memory trace/span storage |
| `observability/audit.py` | `AuditLogger` | DB-persisted agent action journal |
| `observability/websocket.py` | `WebSocketManager` | Real-time event broadcasting |
| `message_bus/redis.py` | `RedisMessageBus` | Pub/sub + streams + DLQ |
| `guardrails/content_moderator.py` | `ContentModerator` | Toxic content detection |
| `guardrails/pii_scrubber.py` | `PIIScrubber` | 12 PII pattern redaction |
| `guardrails/output_validator.py` | `OutputValidator` | JSON + PII + schema validation |
| `a2a/broker.py` | `A2ABroker` | Versioned agent-to-agent message routing |
| `a2a/protocol.py` | `A2AEnvelope`, `TaskRequest`, `TaskResponse` | A2A data models |
| `mcp/server.py` | `MCPServerManager` | Tool discovery + invocation |
| `eval/evaluator.py` | `AgentEvaluator` | Output quality scoring |
| `prompts/registry.py` | `PromptRegistry` | Versioned Jinja2 prompt management |

### 9.3 Configuration Reference

| Variable | Default | Location |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn` | `config.py` |
| `REDIS_URL` | `redis://localhost:6379/0` | `config.py` |
| `OPENROUTER_API_KEY` | `""` | `config.py` |
| `GROQ_API_KEY` | `""` | `config.py` |
| `DEEPSEEK_API_KEY` | `""` | `config.py` |
| `LLM_DEFAULT_PROVIDER` | `"openrouter"` | `config.py` |
| `LLM_DEFAULT_MODEL` | `"openai/gpt-4o"` | `config.py` |
| `LLM_CACHE_TTL` | `3600` | `config.py` |
| `DEFAULT_TENANT_ID` | `"default"` | `config.py` |
| `API_KEY_SECRET` | `""` | `config.py` |
| `RATE_LIMIT_PER_IP` | `100` | `config.py` |
| `RATE_LIMIT_PER_TENANT` | `500` | `config.py` |
| `RATE_LIMIT_WINDOW` | `60` | `config.py` |
| `COST_BUDGET_PER_TENANT` | `100.0` | `config.py` |
| `COST_ALERT_THRESHOLD` | `0.8` | `config.py` |
| `MEMORY_TTL_DAYS` | `90` | `config.py` |
| `EMBEDDING_MODEL` | `"text-embedding-3-small"` | `config.py` |
| `EMBEDDING_DIMENSIONS` | `1536` | `config.py` |
| `OTEL_ENABLED` | `False` | `config.py` |
| `OTLP_ENDPOINT` | `None` | `config.py` |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | `config.py` |

### 9.4 API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/api/v1/health` | No | Liveness check |
| GET | `/api/v1/health/deep` | No | DB + Redis connectivity |
| GET | `/api/v1/sessions/` | Yes | List sessions (limit 50) |
| POST | `/api/v1/sessions/` | Yes | Create session |
| GET | `/api/v1/workflows/` | Yes | List workflow templates |
| GET | `/api/v1/workflows/{name}` | Yes | Workflow detail |
| POST | `/api/v1/workflows/{name}/run` | Yes | Execute workflow |
| POST | `/api/v1/workflows/generate-tasks` | Yes | PM agent task generation |
| GET | `/api/v1/workflows/agents` | Yes | List registered agents |
| GET | `/api/v1/observability/metrics` | Yes | Prometheus metrics |
| GET | `/api/v1/observability/traces` | Yes | List traces |
| GET | `/api/v1/observability/traces/{trace_id}` | Yes | Trace detail |
| GET | `/api/v1/observability/audit` | Yes | Audit log search |
| GET | `/api/v1/observability/analytics/session/{id}` | Yes | Session cost analytics |
| GET | `/api/v1/observability/analytics/tenant/{id}` | Yes | Tenant cost analytics |
| GET | `/api/v1/observability/analytics/health` | Yes | Analytics subsystem health |
| GET | `/metrics` | No | Prometheus scrape endpoint |
| WS | `/api/v1/observability/ws/{tenant_id}` | Yes | Real-time events |

### 9.5 Database Connection

- **Engine**: `create_async_engine(settings.database_url, echo=False)` — singleton in `solvyn/db/session.py`
- **Session factory**: `async_sessionmaker(engine, expire_on_commit=False)`
- **Pattern**: `async with async_session_factory() as session:` for each DB operation
- **No connection pooling config** — uses SQLAlchemy defaults

---

## 10. Open Questions & Risks

### Open Questions

1. **Per-tenant API key enforcement**: `Tenant.api_key_hash` column exists but is only used in per-tenant mode. When should this be the default?
2. **Rate limiter production readiness**: In-memory token bucket won't work across workers. Redis-backed replacement needed?
3. **Tracing persistence**: In-memory traces are lost on restart. Should they be persisted to DB or an external system?
4. **LLM cache provider gap**: Cache key doesn't include provider — same model from different providers shares cache.
5. **Dependency vulnerability scanning**: No `pip-audit` or Dependabot configured.

### Known Risks

| Risk | Severity | Area |
|---|---|---|
| Per-tenant API key validation is O(n) over all tenants | Medium | `multi_tenant/auth.py` |
| Audit log search filters in Python, not SQL | Medium | `observability/audit.py` |
| TaskGenerator keyword matching is substring, not word-boundary | Low | `workflows/task_generator.py` |
| Graph output resolution uses agent type, not node ID | Medium | `agents/graph.py` |
| Frontend API client missing `X-Tenant-Slug` header | Medium | `frontend/src/lib/api.ts` |
| No connection pooling configuration for PostgreSQL | Low | `db/session.py` |
| LLM fallback doesn't catch API errors (4xx/5xx) | Low | `llm/router.py` |
| Checkpoint session_id may not be set during save | Low | `agents/checkpoint.py` |
