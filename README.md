
<div align="center">

```
    ╔══════════════════════════════════════════════════════════════════╗
    ║                                                                  ║
    ║    ███████╗  ██████╗  ██╗      ██╗   ██╗  ██╗   ██╗  ███╗   ██╗  ║
    ║    ██╔════╝ ██╔═══██╗ ██║      ██║   ██║  ╚██╗ ██╔╝  ████╗  ██║  ║
    ║    ███████╗ ██║   ██║ ██║      ██║   ██║   ╚████╔╝   ██╔██╗ ██║  ║
    ║    ╚════██║ ██║   ██║ ██║      ╚██╗ ██╔╝    ╚██╔╝    ██║╚██╗██║  ║
    ║    ███████║ ╚██████╔╝ ███████╗  ╚████╔╝      ██║     ██║ ╚████║  ║
    ║    ╚══════╝  ╚═════╝  ╚══════╝   ╚═══╝       ╚═╝     ╚═╝  ╚═══╝  ║
    ║                                                                  ║
    ║            Your self-hosted AI engineering team                   ║
    ╚══════════════════════════════════════════════════════════════════╝
```

</div>

<div align="center">

[![Python](https://img.shields.io/badge/python-3.12%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/fastapi-0.115%2B-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Next.js](https://img.shields.io/badge/next.js-15%2B-000000?logo=next.js&logoColor=white)](https://nextjs.org/)
[![React](https://img.shields.io/badge/react-19%2B-61DAFB?logo=react&logoColor=black)](https://react.dev/)
[![Docker](https://img.shields.io/badge/docker-✓-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Kubernetes](https://img.shields.io/badge/k8s-✓-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![PostgreSQL](https://img.shields.io/badge/postgres-16%2B-4169E1?logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/redis-7%2B-DC382D?logo=redis&logoColor=white)](https://redis.io/)
[![License](https://img.shields.io/badge/license-MIT-green)](./LICENSE)
[![CI/CD](https://img.shields.io/badge/ci%2Fcd-github%20actions-2088FF?logo=githubactions&logoColor=white)](https://github.com)

</div>

---

## Overview

**Solvyn** is your self-hosted AI engineering team. It gives developers a persistent team of specialized AI workers — Tech Lead, Software Engineer, QA Engineer, and Security Engineer — that understand real projects, execute engineering work in isolated environments, verify it before delivery, and maintain project knowledge over time.

### Why Solvyn Stands Out

| Capability | How Solvyn Delivers It |
|---|---|
| **Autonomous agent collaboration** | 9 specialized agents (PM, Designer, Backend, Frontend, QA, Reviewer, Security, DevOps, Docs) communicate via Redis Streams and execute in a topological DAG |
| **Persistent long-term memory** | pgvector-powered semantic recall with tenant-scoped embeddings — agents remember past sessions |
| **Human-in-the-loop checkpoints** | Agents pause for human approval at critical decision points, with full state snapshots for resume |
| **Self-healing workflows** | Retry with exponential backoff (3 retries, 1s→30s, 2x factor), dead-letter queue for unprocessable messages |
| **Multi-LLM resilience** | Automatic provider fallback: OpenRouter → Groq → DeepSeek — no single provider outage blocks execution |
| **Real-time observability** | WebSocket graph visualization, Prometheus metrics (8 counters/gauges/histograms), structured JSON logging, audit trail |
| **Cost analytics** | Per-session and per-tenant token/cost tracking with provider-specific pricing tables |
| **Multi-tenant isolation** | ContextVar-based tenant scoping through the full request lifecycle; API key authentication with constant-time HMAC comparison |
| **Repository intelligence (early)** | Connect public GitHub repositories to projects, inspect bounded file trees and manifests, and search file paths without executing repository code |
| **Sandbox runtime (internal preview)** | Run argv commands in a one-shot restricted Docker container; the runner is not yet exposed to teammates or API users |
| **Production-ready deployment** | Docker Compose for dev, Kubernetes with HPA autoscaling (2-10 replicas), GitHub Actions CI/CD with staging→prod promotion |

---

## Architecture

```mermaid
graph TB
    subgraph "Client Layer"
        CLI["CLI<br/>Typer + Rich<br/>13 commands"]
        WEB["Web Dashboard<br/>Next.js 15 / React 19<br/>Tailwind CSS 4"]
    end

    subgraph "API Gateway"
        API["FastAPI Application<br/>lifespan: startup/shutdown hooks"]
    end

    subgraph "Middleware Stack"
        CORS["CORS Middleware"]
        AUTH["Auth Middleware<br/>X-API-Key (SHA-256 HMAC)"]
        RL["Rate Limiter<br/>100 req/min IP · 500 req/min tenant"]
        TENANT["Tenant Middleware<br/>X-Tenant-Slug → contextvars"]
        METRICS["Metrics Middleware<br/>request count + duration"]
    end

    subgraph "API Routes"
        HEALTH["/health<br/>/health/deep<br/>DB + Redis"]
        SESSIONS["/sessions<br/>CRUD + list"]
        WORKFLOWS["/workflows<br/>list·get·run·generate-tasks·agents"]
        OBSERVABILITY["/observability<br/>metrics·traces·audit·analytics"]
        PROM["/metrics<br/>Prometheus endpoint"]
        WS["WS /ws/{tenant_id}<br/>real-time graph updates"]
    end

    subgraph "Agent Runtime"
        REGISTRY["AgentRegistry<br/>9 registered agents"]
        PM["Product Manager"]
        SD["System Designer"]
        BE["Backend Engineer"]
        FE["Frontend Engineer"]
        QA["QA Engineer"]
        CR["Code Reviewer"]
        SA["Security Auditor"]
        DO["DevOps Engineer"]
        DW["Documentation Writer"]
    end

    subgraph "AgentGraph DAG"
        GRAPH["AgentGraph Executor<br/>topological walk<br/>.output resolution<br/>failure propagation"]
        CHECKPOINT["Checkpoint Manager<br/>human approval gates<br/>state snapshots"]
        RETRY["Retry Handler<br/>3 retries · exp backoff<br/>dead-letter queue"]
    end

    subgraph "LLM Gateway"
        ROUTER["<b>LLM Router</b><br/>primary: openrouter<br/>fallback 1: groq<br/>fallback 2: deepseek"]
        OR["OpenRouter<br/>GPT-4o · Claude 3.5<br/>Gemini 2.0 Flash"]
        GQ["Groq<br/>Llama 3.3 70B<br/>Mixtral 8x7B"]
        DS["DeepSeek<br/>deepseek-chat<br/>deepseek-reasoner"]
    end

    subgraph "Data Layer"
        PG[("PostgreSQL 16<br/>+ pgvector<br/>9 tables · semantic memory")]
        REDIS[("Redis 7 Streams<br/>pub/sub · dead-letter<br/>stream persistence")]
    end

    subgraph "Observability"
        GRAFANA["Grafana<br/>8-panel dashboard"]
        ALERTS["Prometheus Alerts<br/>7 alert rules"]
        TRACING["Tracing Manager<br/>in-memory spans"]
        AUDIT["Audit Logger<br/>agent actions journal"]
    end

    subgraph "Infrastructure"
        K8S["Kubernetes<br/>Deployment · HPA 2-10 · Ingress<br/>ConfigMap · PVC 50Gi"]
        GH["GitHub Actions<br/>lint → test → build<br/>→ staging → prod"]
        DOCKER["Docker Compose<br/>API · pgvector · Redis 7"]
    end

    CLI & WEB --> API
    API --> CORS --> AUTH --> RL --> TENANT --> METRICS
    METRICS --> HEALTH & SESSIONS & WORKFLOWS & OBSERVABILITY & PROM & WS
    WORKFLOWS --> REGISTRY
    REGISTRY --> PM & SD & BE & FE & QA & CR & SA & DO & DW
    PM & SD & BE & FE & QA & CR & SA & DO & DW --> GRAPH
    GRAPH --> CHECKPOINT
    GRAPH --> RETRY
    GRAPH --> ROUTER
    ROUTER --> OR & GQ & DS
    REGISTRY --> PG
    REGISTRY --> REDIS
    OBSERVABILITY --> GRAFANA & ALERTS & TRACING & AUDIT
    API --> K8S & DOCKER & GH

    style CLI fill:#1a1a2e,stroke:#00d4ff,color:#e0e0e0
    style WEB fill:#1a1a2e,stroke:#00d4ff,color:#e0e0e0
    style API fill:#16213e,stroke:#00d4ff,color:#e0e0e0
    style ROUTER fill:#0f3460,stroke:#e94560,color:#e0e0e0
    style GRAPH fill:#0f3460,stroke:#f5a623,color:#e0e0e0
    style PG fill:#1a1a2e,stroke:#4169E1,color:#e0e0e0
    style REDIS fill:#1a1a2e,stroke:#DC382D,color:#e0e0e0
    style K8S fill:#16213e,stroke:#326CE5,color:#e0e0e0
```

---

## Quick Start

### Prerequisites

- **Python 3.12+** with `pip`
- **Node.js 20+** with `npm`
- **Docker & Docker Compose** (for PostgreSQL, Redis)
- **LLM API Key** from [OpenRouter](https://openrouter.ai/) (free credits available)

### 1. Clone & Install

```bash
git clone https://github.com/your-org/solvyn.git
cd solvyn

# Install Python packages + dev tools
pip install -e ".[dev]"

# Install frontend dependencies
cd frontend && npm install && cd ..
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env — add your OpenRouter API key at minimum:
#   OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

### 3. Start Infrastructure

```bash
docker compose up -d    # PostgreSQL 16 (pgvector) + Redis 7
```

### 4. Run the API Server

```bash
make dev                # uvicorn solvyn.main:app --reload --host 0.0.0.0 --port 8000
```

API docs at [http://localhost:8000/docs](http://localhost:8000/docs) · Health check at [http://localhost:8000/api/v1/health](http://localhost:8000/api/v1/health)

### 5. Run the Dashboard (optional)

```bash
make dev-frontend       # Next.js dev server on http://localhost:3000
```

### 6. Try the CLI

```bash
solvyn status           # System status overview
solvyn agents           # List all 9 agents
solvyn chat             # Interactive chat with Product Manager
solvyn run --task "Design a REST API for a blog" --agent system-designer
solvyn workflows        # List workflow templates
solvyn pipeline feature --input "Build a user authentication system"
```

---

## CLI Quick Reference

Solvyn ships with a full-featured CLI built with [Typer](https://typer.tiangolo.com/) and [Rich](https://rich.readthedocs.io/):

| # | Command | Arguments / Options | Description |
|---|---------|---------------------|-------------|
| 1 | `solvyn status` | — | Show system status: API, database, Redis, LLM providers |
| 2 | `solvyn agents` | — | List all 9 registered agents with descriptions and capabilities |
| 3 | `solvyn chat` | `[agent]` (default: `product-manager`) | Interactive chat session with any agent |
| 4 | `solvyn run` | `--task`, `--agent`, `--output` | Execute a single non-interactive task with an agent |
| 5 | `solvyn workflows` | — | List available workflow templates (feature, bugfix, release) |
| 6 | `solvyn pipeline` | `[workflow]`, `--input` | Run a full multi-step workflow pipeline |
| 7 | `solvyn generate-tasks` | `--spec` | Generate tasks from a product specification |
| 8 | `solvyn traces` | `--limit` (default: 10) | Show recent execution traces with spans and sessions |
| 9 | `solvyn audit` | `--action`, `--agent`, `--limit` | Search and display audit log entries |
| 10 | `solvyn list-sessions` | — | Instructions for listing sessions via the API |
| 11 | `solvyn providers` | — | Show LLM providers with status, default models, and API key status |
| 12 | `solvyn version` | — | Show version, Python, platform, and core dependency versions |
| 13 | `solvyn serve` | (entry point via `uvicorn`) | Start the FastAPI server from the CLI context |

Every command supports `--help` for detailed usage information.

<details>
<summary><b>CLI usage examples</b></summary>

```bash
# Check what's running and configured
solvyn status

# List all available agent types
solvyn agents

# Chat with a specific agent
solvyn chat system-designer

# Run a one-shot task and save results
solvyn run --task "Write a SQL schema for an e-commerce app" --agent backend-engineer --output schema.md

# See available workflow templates
solvyn workflows

# Execute a full feature delivery pipeline
solvyn pipeline feature --input "Build a real-time chat application with WebSocket support"

# Generate task breakdown from a spec
solvyn generate-tasks --spec "User management: registration, login, roles, permissions, audit log"

# Check LLM provider configurations
solvyn providers

# Show recent execution traces
solvyn traces --limit 20

# Search audit log for security reviews
solvyn audit --agent security-auditor --limit 10
```
</details>

---

## Agents

Solvyn's 9 specialized AI agents form a complete virtual software team. Each agent extends `BaseAgent` and follows a standardized lifecycle: **IDLE → RUNNING → (WAITING_APPROVAL) → COMPLETED/FAILED**.

| # | Agent Type | Role | Description | Capability |
|---|-----------|------|-------------|-------------|
| 1 | `product-manager` | Product Manager | Parse PRDs, generate epics and user stories with acceptance criteria and dependency graphs | `product_management` |
| 2 | `system-designer` | System Designer | Architecture diagrams (Mermaid), tech stack recommendations, DB schema design, API contracts | `system_design`, `architecture` |
| 3 | `backend-engineer` | Backend Engineer | Generate FastAPI endpoints, SQLAlchemy models, business logic, Alembic migrations, input validation | `code_generation` |
| 4 | `frontend-engineer` | Frontend Engineer | React/TypeScript components, API integration hooks, state management, responsive design | `code_generation` |
| 5 | `qa-engineer` | QA Engineer | Unit, integration, and E2E tests with coverage analysis, edge case identification, and mocks | `testing` |
| 6 | `code-reviewer` | Code Reviewer | Static analysis for correctness, performance, security, and style — categorized as Issues / Suggestions / Notes | `code_review` |
| 7 | `security-auditor` | Security Auditor | OWASP Top 10 scan, secrets detection, auth review, injection risks, compliance (GDPR/HIPAA/PCI) | `security` |
| 8 | `devops-engineer` | DevOps Engineer | Dockerfiles, Kubernetes manifests, CI/CD pipelines, Terraform, monitoring configuration | `devops` |
| 9 | `documentation-writer` | Documentation Writer | README files, API docs, architecture guides, release notes, onboarding docs | `documentation` |

**Key framework features across all agents:**
- `llm_generate()` — routes through the multi-provider LLM router with automatic fallback
- `request_approval()` — creates a human-in-the-loop checkpoint with full state snapshot
- `resume_from_checkpoint()` — resumes execution after human approval/rejection
- Cost tracking — every agent call records provider, model, tokens, and cost

---

## Workflow Templates

Pre-built multi-agent pipelines that orchestrate agents through a topological DAG execution.

### Feature Delivery Pipeline

```
PRD → Architecture → [Backend ∥ Frontend] → QA + Review + Security → DevOps → Docs
```

| Step | Agent | Depends On | Parallel With |
|------|-------|------------|---------------|
| 1. Analyze requirements | `product-manager` | — | — |
| 2. Design architecture | `system-designer` | `product-manager` | — |
| 3. Generate backend code | `backend-engineer` | `system-designer` | `frontend-engineer` |
| 4. Generate frontend code | `frontend-engineer` | `system-designer` | `backend-engineer` |
| 5. Generate tests | `qa-engineer` | `backend-engineer`, `frontend-engineer` | — |
| 6. Code review | `code-reviewer` | `backend-engineer`, `frontend-engineer` | — |
| 7. Security audit | `security-auditor` | `backend-engineer`, `frontend-engineer` | — |
| 8. Deployment config | `devops-engineer` | `backend-engineer` | — |
| 9. Documentation | `documentation-writer` | `backend-engineer`, `frontend-engineer` | — |

```bash
solvyn pipeline feature --input "Build an inventory management system with barcode scanning"
```

### Bugfix Pipeline

```
Bug Report → Analysis → Fix → Review → Deploy
```

| Step | Agent | Depends On |
|------|-------|------------|
| 1. Analyze & reproduce | `qa-engineer` | — |
| 2. Implement fix | `backend-engineer` | `qa-engineer` |
| 3. Review fix | `code-reviewer` | `backend-engineer` |
| 4. Hotfix deploy | `devops-engineer` | `backend-engineer` |

```bash
solvyn pipeline bugfix --input "Login returns 500 when email contains '+' character"
```

### Release Pipeline

```
Changelog + Security Scan (parallel) → Deploy
```

| Step | Agent | Depends On | Parallel With |
|------|-------|------------|---------------|
| 1. Release notes | `documentation-writer` | — | `security-auditor` |
| 2. Final security scan | `security-auditor` | — | `documentation-writer` |
| 3. Production deploy | `devops-engineer` | `documentation-writer`, `security-auditor` | — |

```bash
solvyn pipeline release --input "v2.1.0: Added dark mode, fixed pagination, updated dependencies"
```

---

## API Endpoints

Base URL: `http://localhost:8000/api/v1`

### Health

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/health` | Public | Basic liveness check |
| `GET` | `/health/deep` | Public | Deep check: database connectivity + Redis ping |

### Sessions

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/sessions/` | Required | List recent sessions (limit 50) |
| `POST` | `/sessions/` | Required | Create a new session |

### Workflows

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/workflows/` | Required | List available workflow templates |
| `GET` | `/workflows/{name}` | Required | Get workflow detail with steps |
| `POST` | `/workflows/{name}/run` | Required | Execute a workflow pipeline |
| `GET` | `/workflows/executions/{execution_id}` | Required | Read a tenant-scoped durable execution snapshot |
| `POST` | `/workflows/executions/{execution_id}/resume` | Required | Resume unfinished nodes after a worker lease expires |
| `POST` | `/workflows/generate-tasks` | Required | Generate tasks from a specification |
| `GET` | `/workflows/agents` | Required | List all registered agents |

Workflow snapshots are saved as nodes start and finish. Workers renew a 60-second lease while executing; a resume request is rejected while that lease is active and can reclaim an expired or failed run. Completed nodes are retained, while unfinished nodes run again at least once, so an external side effect may repeat if a worker stopped after performing it but before saving its result. Execution snapshots and resume operations are scoped to the authenticated tenant.

### Observability

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/observability/metrics` | Public | Prometheus-format metrics |
| `GET` | `/observability/traces` | Required | List traces (filterable by `session_id`) |
| `GET` | `/observability/traces/{trace_id}` | Required | Get trace detail with spans |
| `GET` | `/observability/audit` | Required | Search audit logs (filter by `action`, `agent_type`, `session_id`) |
| `GET` | `/observability/analytics/session/{session_id}` | Required | Per-session token/cost totals + per-agent breakdown |
| `GET` | `/observability/analytics/tenant/{tenant_id}` | Required | Per-tenant aggregate analytics |
| `GET` | `/observability/analytics/health` | Required | Service health status for analytics subsystem |

### Metrics (Prometheus)

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| `GET` | `/metrics` | Public | Prometheus metrics endpoint (8 metrics) |

### WebSocket

| Protocol | Endpoint | Auth | Description |
|----------|----------|------|-------------|
| `WS` | `/observability/ws/{tenant_id}?session_id=` | Required | Real-time events: `graph_update`, `agent_message`, `workflow_update` |

> See [API.md](./API.md) for detailed request/response schemas, authentication, and error codes.

---

## Development

### Makefile Targets

| Target | Command | Description |
|--------|---------|-------------|
| `dev` | `uvicorn solvyn.main:app --reload` | Start API server with hot reload |
| `dev-frontend` | `cd frontend && npm run dev` | Start Next.js dev server |
| `install` | `pip install -e ".[dev]"` + `npm install` | Install all dependencies |
| `lint` | `ruff check` + `ruff format --check` | Lint and check formatting |
| `format` | `ruff check --fix` + `ruff format` | Auto-fix lint and format issues |
| `typecheck` | `mypy solvyn/ --follow-imports=skip` | Static type checking (strict mode) |
| `test` | `pytest tests/ -v` | Run test suite |
| `test-cov` | `pytest tests/ -v --cov=solvyn` | Run tests with coverage report |
| `docker-build` | `docker compose build` | Build Docker images |
| `docker-up` | `docker compose up -d` | Start all services |
| `docker-down` | `docker compose down` | Stop all services |
| `docker-logs` | `docker compose logs -f` | Tail service logs |
| `deploy` | `kubectl apply -f k8s/` (7 resources) | Deploy to Kubernetes |
| `pre-commit` | `lint` + `typecheck` + `test` | Full pre-commit quality gate |
| `clean` | Remove caches, `.next`, `htmlcov` | Clean build artifacts |

### Pre-commit Hooks

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff          # Linting + import sorting + dead code
      - id: ruff-format   # Code formatting (160 char line length)
      - id: mypy          # Static type checking (strict mode, Python 3.12)
      - id: check-yaml    # YAML syntax validation
      - id: check-json    # JSON syntax validation
      - id: check-toml    # TOML syntax validation
```

```bash
pre-commit install        # Install git hooks
make pre-commit           # Run all checks manually
```

### Project Conventions

- **Line length**: 160 characters (configured in `pyproject.toml`)
- **Linting**: Ruff with rules `E`, `F`, `I`, `N`, `W`, `UP`
- **Type checking**: Mypy strict mode (`strict = true`), Python 3.12
- **Testing**: pytest with `asyncio_mode = "auto"` and `pytest-cov` for coverage
- **Imports**: Ruff auto-sorts imports; project-internal imports use `solvyn.` prefix

---

## Deployment

### Docker Compose (Development)

```bash
docker compose up -d
```

Services:
- **API**: Python 3.12-slim, uvicorn with `--reload`, port 8000
- **Database**: `pgvector/pgvector:pg16`, health check with `pg_isready`, port 5432
- **Redis**: `redis:7-alpine`, port 6379

### Kubernetes (Production)

```bash
make deploy          # Apply namespace, configmap, pvc, deployment, service, hpa, ingress
make deploy-secrets  # Apply secrets (from k8s/secrets.yaml — not committed)
```

| Resource | Spec |
|----------|------|
| **Namespace** | `solvyn` — isolated namespace |
| **Deployment** | 2 replicas, `solvyn/api:latest`, Prometheus scrape annotations |
| **HPA** | Min 2, max 10 replicas; CPU 70%, memory 80% |
| **Resources** | 500m–2000m CPU, 512Mi–2Gi memory |
| **PVC** | 50Gi persistent volume for PostgreSQL data |
| **Service** | ClusterIP, port 8000 |
| **Ingress** | TLS via nginx ingress + cert-manager |
| **Health checks** | Liveness: `GET /api/v1/health`, Readiness: `GET /api/v1/health` |

### CI/CD Pipeline

```
Push to main → lint (ruff + mypy) → test (pytest + pgvector + Redis) → build (Docker) → deploy-staging → deploy-prod
```

| Stage | Trigger | Gates |
|-------|---------|-------|
| **Lint** | Push + PR | ruff check, mypy type check |
| **Test** | Push + PR | pytest with PostgreSQL + Redis service containers |
| **Build** | Push to `main` only | Docker build + push to `ghcr.io` (API + frontend images) |
| **Deploy Staging** | Push to `main` only | `kubectl set image` + `rollout status` (5min timeout) |
| **Deploy Prod** | Push to `main` only | Manual approval gate via environment protection rules |

> See [DEPLOYMENT.md](./DEPLOYMENT.md) for full deployment guide, rollback procedures, and environment setup.

---

## Configuration

All configuration uses environment variables, loaded via Pydantic-settings from `.env`.

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn` | Async PostgreSQL connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `OPENROUTER_API_KEY` | *(empty)* | OpenRouter API key (required for default LLM) |
| `GROQ_API_KEY` | *(empty)* | Groq API key (fallback provider) |
| `DEEPSEEK_API_KEY` | *(empty)* | DeepSeek API key (fallback provider) |
| `LLM_DEFAULT_PROVIDER` | `openrouter` | Default LLM provider: `openrouter`, `groq`, or `deepseek` |
| `LLM_DEFAULT_MODEL` | `openai/gpt-4o` | Default model ID for the primary provider |
| `DEFAULT_TENANT_ID` | `default` | Default tenant identifier for single-tenant deployments |
| `API_KEY_SECRET` | `change-me-to-a-random-secret` | Shared secret for API key HMAC validation |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `LOG_LEVEL` | `INFO` | Python log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `JSON_LOGGING` | `true` | Enable structured JSON log output |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | CORS allowed origins (JSON list) |

> ⚠️ **Security**: Change `API_KEY_SECRET` to a strong random value before deploying. The default value triggers a startup warning.

---

## Project Structure

```
solvyn/
├── solvyn/                           # Main Python package
│   ├── __init__.py                   # Version: 0.1.0
│   ├── main.py                       # FastAPI app factory, middleware stack, lifespan
│   ├── config.py                     # Pydantic-settings (env → config)
│   ├── cli.py                        # Typer CLI — 13 commands
│   ├── logging_config.py             # JSON log formatter + setup
│   │
│   ├── agents/                       # 9 specialized AI agents
│   │   ├── __init__.py               # Auto-registers all 9 agents
│   │   ├── base.py                   # BaseAgent — lifecycle, checkpoints, LLM routing
│   │   ├── registry.py               # AgentRegistry — discovery + factory
│   │   ├── graph.py                  # AgentGraph — DAG topological executor
│   │   ├── schema.py                 # Data models (AgentStatus, ExecutionNode, etc.)
│   │   ├── checkpoint.py             # CheckpointManager — pause/approve/resume
│   │   ├── memory.py                 # AgentMemory — pgvector semantic recall
│   │   ├── retry.py                  # RetryHandler + DeadLetterHandler
│   │   ├── communication.py          # Redis pub/sub inter-agent messaging
│   │   ├── rate_limiter.py           # Token bucket — per-agent, per-LLM
│   │   ├── cost_tracker.py           # Token + cost per session/tenant
│   │   ├── product_manager.py        # Product Manager agent
│   │   ├── system_designer.py        # System Designer agent
│   │   ├── backend_engineer.py       # Backend Engineer agent
│   │   ├── frontend_engineer.py      # Frontend Engineer agent
│   │   ├── qa_engineer.py            # QA Engineer agent
│   │   ├── code_reviewer.py          # Code Reviewer agent
│   │   ├── security_auditor.py       # Security Auditor agent
│   │   ├── devops_engineer.py        # DevOps Engineer agent
│   │   └── documentation_writer.py   # Documentation Writer agent
│   │
│   ├── api/v1/                       # REST API
│   │   ├── health.py                 # /health, /health/deep
│   │   ├── sessions.py               # GET/POST /sessions
│   │   └── workflows.py              # Workflows, agents, task generation
│   │
│   ├── db/                           # Database layer
│   │   ├── models.py                 # 9 SQLAlchemy ORM models
│   │   ├── session.py                # Async engine, session factory
│   │   └── migrations/               # Alembic (asyncpg)
│   │
│   ├── llm/                          # LLM gateway
│   │   ├── router.py                 # Multi-provider router + fallback
│   │   └── providers/                # OpenRouter, Groq, DeepSeek clients
│   │
│   ├── message_bus/
│   │   └── redis.py                  # RedisMessageBus — pub/sub + streams + DLQ
│   │
│   ├── multi_tenant/
│   │   ├── auth.py                   # API key auth + RateLimitMiddleware
│   │   └── middleware.py             # TenantContext (contextvars)
│   │
│   ├── observability/
│   │   ├── metrics.py                # 8 Prometheus metrics + MetricsMiddleware
│   │   ├── tracing.py                # In-memory trace/span storage
│   │   ├── audit.py                  # Agent actions audit journal
│   │   ├── websocket.py              # Real-time event broadcasting
│   │   └── analytics_api.py          # Analytics + WebSocket endpoints
│   │
│   └── workflows/
│       ├── templates.py              # 3 workflows: feature, bugfix, release
│       ├── executor.py               # Template → AgentGraph → execute → persist
│       └── task_generator.py         # Spec → PM agent → task breakdown
│
├── frontend/                         # Next.js 15 dashboard
│   ├── src/app/                      # App Router pages
│   ├── src/components/               # AgentCard, ExecutionGraph, Sidebar
│   ├── src/lib/api.ts                # Typed API client
│   └── Dockerfile                    # Multi-stage frontend build
│
├── k8s/                              # Kubernetes manifests (7 files)
│   ├── namespace.yaml                # solvyn namespace
│   ├── configmap.yaml                # Non-secret environment config
│   ├── secrets.example.yaml          # Secret template (not committed)
│   ├── pvc.yaml                      # 50Gi persistent volume claim
│   ├── deployment.yaml               # API + DB + Redis deployments
│   ├── service.yaml                  # ClusterIP services
│   ├── hpa.yaml                      # Horizontal Pod Autoscaler
│   └── ingress.yaml                  # TLS ingress
│
├── monitoring/
│   ├── prometheus-alerts.yml         # 7 alert rules
│   └── grafana-dashboard.json        # 8-panel dashboard
│
├── tests/                            # pytest test suite
│   ├── conftest.py                   # Fixtures + test configuration
│   ├── test_api.py                   # API endpoint tests
│   ├── test_agents.py                # Agent lifecycle + execution
│   ├── test_agent_graph.py           # DAG executor
│   ├── test_agent_schema.py          # Data model validation
│   ├── test_auth_logging.py          # Authentication + logging
│   ├── test_cli.py                   # CLI command tests
│   ├── test_config.py                # Configuration tests
│   ├── test_llm_redis_multi_tenant.py # LLM, Redis, multi-tenant integration
│   ├── test_memory_checkpoint_retry.py # Memory, checkpoints, retries
│   ├── test_observability.py         # Metrics, tracing, audit
│   ├── test_rate_limiter_cost.py     # Rate limiting + cost tracking
│   ├── test_sessions.py              # Session CRUD
│   └── test_workflows.py             # Workflow template + execution
│
├── scripts/
│   └── backup-db.sh                  # Database backup script
│
├── .github/workflows/
│   └── ci-cd.yml                     # lint → test → build → staging → prod
│
├── pyproject.toml                    # Dependencies, ruff, mypy, pytest config
├── Makefile                          # dev, lint, test, docker, deploy targets
├── Dockerfile                        # Python 3.12-slim
├── sandbox.Dockerfile                # Restricted command-execution image
├── docker-compose.yml                # API + pgvector/pg16 + Redis 7
├── alembic.ini                       # Database migration config
├── .pre-commit-config.yaml           # Pre-commit hook definitions
└── .env.example                      # Environment variable reference
```

---

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Full system architecture, component design, data flow, technology decisions |
| [API.md](./API.md) | Complete API reference — endpoints, request/response schemas, auth, error codes |
| [Repository Intelligence](./docs/REPOSITORY_INTELLIGENCE.md) | Current GitHub provider scope, limits, and security boundaries |
| [Sandbox Runtime](./docs/SANDBOX_RUNTIME.md) | Internal Docker execution boundary, resource policy, and smoke test |
| [SECURITY.md](./SECURITY.md) | Security model, authentication, multi-tenant isolation, compliance guidance |
| [DEPLOYMENT.md](./DEPLOYMENT.md) | Deployment guide — Docker, Kubernetes, CI/CD, rollback procedures |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | Contribution guidelines, development setup, coding standards, PR process |
| [PLAN.md](./PLAN.md) | Original project plan — requirements, architecture decisions, task breakdown |

---

## License

MIT License — see [LICENSE](./LICENSE) for details.

---

<div align="center">

**Solvyn** — *Your self-hosted AI engineering team.*

</div>
