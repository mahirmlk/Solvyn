# Solvyn

<div align="center">

<p><img src="docs/assets/solvyn-wordmark.svg" width="210" height="52" alt="Solvyn"></p>

<h2>Your self-hosted AI engineering team</h2>

<p>Connect a GitHub project, assign engineering work to a configured teammate, and review the workspace changes and verification results before opening a draft pull request.</p>

<p>
  <a href="#features">Features</a> |
  <a href="#architecture">Architecture</a> |
  <a href="#quick-start">Quick Start</a> |
  <a href="./API.md">API</a> |
  <a href="#documentation">Docs</a>
</p>

[![CI](https://github.com/mahirmlk/Solvyn/actions/workflows/ci-cd.yml/badge.svg?branch=main)](https://github.com/mahirmlk/Solvyn/actions/workflows/ci-cd.yml)
![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)
![FastAPI 0.115+](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white)
![Next.js 15](https://img.shields.io/badge/Next.js-15-000000?logo=next.js&logoColor=white)
![React 19](https://img.shields.io/badge/React-19-149ECA?logo=react&logoColor=white)
![PostgreSQL 16](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![Redis 7](https://img.shields.io/badge/Redis-7-DC382D?logo=redis&logoColor=white)

</div>

Solvyn gives engineering teams a self-hosted place to configure AI teammates, connect GitHub projects, assign scoped work, and review task history. GitHub-seeded task workspaces use selected repository files pinned to a commit; other workspaces can start from supplied UTF-8 files. Teammate tools follow project permissions, and verification requests require approval before running in an isolated container. An owner or admin can inspect a task diff and request a draft pull request. A person decides whether to merge it; production deployment follows a separate operator-controlled workflow.

> Solvyn is under active development. The repository, task, workspace, approval, and delivery components are present. The full product workflow and production deployment still need release validation. See the [shipment readiness assessment](./docs/SHIPMENT_READINESS.md).

## Features

### Repository context and task assignment

Connect public GitHub repositories or private repositories authorized through an encrypted organization integration. Solvyn inspects bounded repository trees and manifests, then lets an authorized user seed a task workspace with selected files at one pinned commit. Tasks keep their dependencies, handoffs, status, and activity in PostgreSQL.

Repository inspection extracts static direct dependency names from supported manifests at the root and one directory below. Discovery is bounded and deterministic; it does not resolve versions, scan for vulnerabilities, or report available updates.

### Permission-scoped teammate work

Teammates receive file tools according to the grants on their task. Those tools can list, read, search, write, or delete workspace files. The runtime records tool activity without copying file contents into the audit record.

### Verification and delivery

Teammates can request test, build, lint, type-check, or security verification. An owner or admin approves verification before the networkless, resource-limited container runs it. Solvyn saves bounded output and the workspace digest with each result. An owner or admin can inspect the diff and create a draft pull request from the pinned source revision.

Verification requests use these defaults; a request may supply a different command, which remains visible for owner or admin approval:

| Kind | Default command |
|---|---|
| `test` | `pytest -q` |
| `build` | `python -m compileall -q .` |
| `lint` | `ruff check .` |
| `typecheck` | `mypy .` |
| `security` | `ruff check --select S .` |

### Persistent teammate conversations

Users can save and resume teammate conversations scoped to an assigned project. Recent messages are bounded before they are passed as context; direct conversations do not grant workspace tools.

### Operations and hosting

Task history includes approvals, verification attempts, delivery records, and recorded usage. Configured GitHub events and recurring maintenance checks appear for human review. Use the development Compose stack locally or follow the production Compose guide. The Kubernetes workflow needs operator configuration; it has not been validated in a controlled deployment.

### Agent roles and specializations

Solvyn provisions four persistent teammate roles. Each role is bound to a specific agent implementation:

| Teammate role | Agent type | Primary responsibility |
|---|---|---|
| Tech Lead | `system-designer` | Technical direction and architecture work |
| Software Engineer | `backend-engineer` | Implementing assigned engineering changes |
| QA Engineer | `qa-engineer` | Test and verification work |
| Security Engineer | `security-auditor` | Security-focused review |

The workflow registry also exposes nine agent types. These are agent implementations available to workflow graphs; registry presence does not make every type a persistent teammate.

| Registry agent | Specialization |
|---|---|
| `backend-engineer` | FastAPI endpoints, models, and business logic |
| `code-reviewer` | Static analysis, style, and correctness review |
| `devops-engineer` | Docker, Kubernetes, CI/CD, and Terraform |
| `documentation-writer` | README files, API documentation, and release notes |
| `frontend-engineer` | React components, hooks, and state management |
| `product-manager` | PRD analysis, epics, and user stories |
| `qa-engineer` | Unit, integration, and end-to-end tests |
| `security-auditor` | Vulnerability, OWASP, and secret checks |
| `system-designer` | Architecture, technology choices, and schema design |

Three workflow templates are present: **feature** (9 agent steps), **bugfix** (4), and **release** (3). The feature graph begins with product analysis and system design, runs backend and frontend work in parallel, then schedules QA, review, security, DevOps, and documentation steps. Templates describe available workflow graphs; they do not claim every task runs every step.

## Architecture

```mermaid
flowchart LR
    User[Developer] --> UI[Next.js dashboard]
    UI --> API[FastAPI API]
    API --> DB[(PostgreSQL with pgvector)]
    API --> Redis[(Redis)]
    API --> GitHub[GitHub REST API]
    API --> LLM[Configured model provider]
    API --> Supervisor[Private sandbox supervisor]
    Supervisor --> Job[Networkless job container]
    Worker[Scheduler and verification worker] --> DB
    Worker --> Supervisor
```

The API stores durable task and approval state in PostgreSQL. Redis supports messaging and coordination. In the production Compose setup, only the trusted sandbox supervisor receives Docker Engine access; job containers do not receive host credentials or network access.

## Quick Start

### Requirements

- Python 3.12 or newer
- Node.js 20 or newer
- Docker Engine with Docker Compose v2
- An LLM provider key for model-backed tasks

PostgreSQL with pgvector and Redis run in the development Compose stack.

### 1. Clone and install

```bash
git clone https://github.com/mahirmlk/Solvyn.git
cd Solvyn
python -m pip install -e ".[dev]"
cd frontend
npm ci
cd ..
```

On Windows PowerShell, activate a virtual environment with `.venv\Scripts\Activate.ps1` if you created one. On macOS or Linux, use `source .venv/bin/activate`.

### 2. Configure the environment

Copy `.env.example` to `.env`. Set unique local values for `POSTGRES_PASSWORD`, `JWT_SECRET`, and `API_KEY_SECRET`, and add a key for the LLM provider you use. Solvyn supports OpenRouter, Groq, and DeepSeek provider configuration. Keep `.env` out of source control.

Public registration is disabled by default. For a local-only setup, set `ALLOW_PUBLIC_REGISTRATION=true`. For a networked deployment, keep it disabled and use the one-time bootstrap flow described in `.env.example` and [DEPLOYMENT.md](./DEPLOYMENT.md).

### 3. Start the database and Redis

```bash
docker compose up -d db redis
alembic upgrade head
```

### 4. Start the API and dashboard

In one terminal, from the repository root:

```bash
uvicorn solvyn.main:app --reload --host 0.0.0.0 --port 8000
```

In another terminal:

```bash
cd frontend
npm run dev
```

Open [http://localhost:3000](http://localhost:3000). The API reference is at [http://localhost:8000/docs](http://localhost:8000/docs), and Prometheus metrics are exposed at [http://localhost:8000/metrics](http://localhost:8000/metrics). The frontend uses `http://localhost:8000/api/v1` by default; set `SOLVYN_API_URL` on the frontend server to change it.

### CLI

Install the Python package, then run `solvyn --help`. The CLI includes status and provider checks, agent and workflow listings, task runs, workflow pipelines, and observability commands.

| Command | Purpose |
|---|---|
| `solvyn status` | Show API, database, Redis, and provider status |
| `solvyn version` | Show Solvyn and runtime version information |
| `solvyn providers` | List configured model providers |
| `solvyn agents` | List registered agent types and capabilities |
| `solvyn chat [agent]` | Start an interactive agent session |
| `solvyn run` | Run one non-interactive agent task |
| `solvyn workflows` | List available workflow templates |
| `solvyn pipeline [workflow]` | Execute a workflow template |
| `solvyn generate-tasks` | Generate proposed tasks from a specification |
| `solvyn traces` | Show recent execution traces |
| `solvyn audit` | Search audit entries by action or agent |
| `solvyn list-sessions` | List recent sessions through the API |

For example, `solvyn run --agent code-reviewer --task "Review the authentication changes" --output review.md` runs a focused review and writes its response to a file. Use `solvyn COMMAND --help` for command-specific options.

## How a task workspace works

1. An organization owner or admin connects a repository to a project.
2. An authorized user assigns work to a teammate with explicit permissions.
3. An authorized user creates a workspace from selected repository files or supplied UTF-8 files. Repository files are read at one pinned commit.
4. The teammate uses only the file tools allowed by its grants. Verification requests use default commands or a requested command and require owner or admin approval.
5. An owner or admin inspects the diff and digest, then can create a draft pull request on GitHub.

Solvyn treats repository content as untrusted input. The organization GitHub token is encrypted at rest and used by the API to access linked repositories; it is not sent to the model or sandbox. Tenant-owned records use PostgreSQL row-level security.

## Current limits

- Repository workspaces contain selected files; the current task flow does not create a full clone or Git worktree.
- Teammates cannot run arbitrary shell commands or install project dependencies. Approved verification commands run in a networkless sandbox.
- Before a task workspace exists, repository search matches file paths. Content search is available inside an assigned workspace.
- Delivery opens a draft pull request. A person reviews and merges it.
- Dependency review schedules currently create reminders; Solvyn does not bundle a dependency vulnerability scanner.
- The combined conversation-to-task-to-verified-delivery workflow, controlled staging deployment, and deployment-specific restore drill remain unverified. See [shipment readiness](./docs/SHIPMENT_READINESS.md) for the current release assessment.

## Development and checks

Start PostgreSQL and Redis as shown above, then run:

```bash
make lint
make typecheck
make test
```

The test suite includes PostgreSQL-backed integration coverage. CI also builds the API and frontend images and runs dependency vulnerability checks. Sandbox container integration tests require a running Linux Docker Engine and are opt-in; see [the sandbox runtime guide](./docs/SANDBOX_RUNTIME.md).

## Evaluations and test coverage

The repository includes a deterministic heuristic evaluator in `solvyn/eval/evaluator.py`. `tests/test_eval.py` contains 13 unit test methods that exercise the result format, the evaluator checks, summary calculation, and the global evaluator instance.

| Check | What it measures |
|---|---|
| JSON structure | Whether valid JSON contains the required keys; score reflects the fraction found |
| Output length | Whether the output character count falls within configured bounds |
| Forbidden phrases | Case-insensitive search for caller-supplied phrases; this is string matching, not factuality detection |
| Code heuristics | Flags empty or long output, TODO markers, possible hard-coded passwords, broad exception handling, `eval`/`exec`, and a simple SQL formatting pattern |

`AgentEvaluator` supports test suites registered by callers, but no application code currently registers a suite. The evaluator tests use small fixture strings; they do not run live model evaluations. There is no standardized benchmark dataset or published agent-quality score. These checks are narrow regression guards for output shape and selected code patterns, not measures of task success, factual accuracy, or real-world code quality.

The broader test suite covers API behavior, permissions, agent graphs, repository access, sandbox execution, task delivery, scheduled maintenance, and observability. CI collects coverage, but the project does not publish a fixed test total or minimum coverage target here.

## Runtime metrics

The API exposes Prometheus metrics at `/metrics`. HTTP middleware records request counts and request duration. UUID path segments are normalized to `{id}` so per-resource identifiers do not create separate endpoint labels.

| Metric | Type | Labels / units | Current status |
|---|---|---|---|
| `solvyn_http_requests_total` | Counter | method, endpoint, status | Updated by HTTP middleware |
| `solvyn_http_request_duration_seconds` | Histogram | method, endpoint; seconds | Updated by HTTP middleware |
| `solvyn_llm_calls_total` | Counter | provider, model, status | Defined; helper has no application call sites |
| `solvyn_agent_executions_total` | Counter | agent type, status | Defined; helper has no application call sites |
| `solvyn_workflow_runs_total` | Counter | workflow name, status | Defined; helper has no application call sites |
| `solvyn_memory_operations_total` | Counter | operation, agent type | Defined; helper has no application call sites |
| `solvyn_token_usage_total` | Counter | provider, model; tokens | Defined; helper has no application call sites |
| `solvyn_active_agents` | Gauge | none; active agent count | Defined; helper has no application call sites |

Only the HTTP metrics are currently updated by middleware. The remaining metric families have helper methods but no application call sites, so they should not be read as measured model or agent activity. Values depend on process traffic and lifetime; counters reset when the API process restarts unless an external monitoring system retains history. No production metrics snapshot, throughput claim, latency target, or service-level objective is published.

### Source-level project snapshot

These counts describe code and test definitions in this repository, not runtime success rates.

| Measure | Count | Source |
|---|---:|---|
| Registered agent types | 9 | `solvyn/agents/__init__.py` |
| Persistent teammate roles | 4 | `solvyn/agents/teammate_runtime.py` |
| Workflow templates | 3 | `solvyn/workflows/templates.py` |
| Evaluator unit test methods | 13 | `tests/test_eval.py` |
| Evaluator check categories | 4 | `solvyn/eval/evaluator.py` |

## Documentation

| Guide | What it covers |
|---|---|
| [Architecture](./ARCHITECTURE.md) | Services, data flow, and design decisions |
| [API reference](./API.md) | Routes, authentication, schemas, and errors |
| [Deployment](./DEPLOYMENT.md) | Local setup, self-hosted Compose, Kubernetes workflow, backup, and recovery |
| [Security](./SECURITY.md) | Authentication, tenant isolation, integrations, and execution boundaries |
| [Repository intelligence](./docs/REPOSITORY_INTELLIGENCE.md) | GitHub connection, repository inspection, and access limits |
| [Sandbox runtime](./docs/SANDBOX_RUNTIME.md) | Workspace tools, verification profiles, and container isolation |
| [Shipment readiness](./docs/SHIPMENT_READINESS.md) | Implemented scope and release blockers |
| [Contributing](./CONTRIBUTING.md) | Development setup and contribution workflow |

## License

This repository does not currently include a `LICENSE` file, so no license is declared here.
