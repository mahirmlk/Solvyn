# Contributing to Solvyn

> Developer guide for setting up, developing, and contributing to Solvyn

---

## Table of Contents

1. [Getting Started](#getting-started)
2. [Development Workflow](#development-workflow)
3. [Project Structure](#project-structure)
4. [Code Style & Conventions](#code-style--conventions)
5. [How to Add a New Agent](#how-to-add-a-new-agent)
6. [How to Add a New Workflow Template](#how-to-add-a-new-workflow-template)
7. [How to Add a New LLM Provider](#how-to-add-a-new-llm-provider)
8. [Testing Guidelines](#testing-guidelines)
9. [Pull Request Process](#pull-request-process)
10. [Commit Conventions](#commit-conventions)
11. [Issue Reporting](#issue-reporting)
12. [Documentation Standards](#documentation-standards)

---

## Getting Started

### Prerequisites

- Python 3.12+
- Node.js 20+
- Docker (for local PostgreSQL + Redis)
- Git

### Setup

```bash
# 1. Clone the repository
git clone <repository-url>
cd solvyn

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1

# 3. Install dependencies
pip install -e ".[dev]"

# 4. Install pre-commit hooks
pre-commit install

# 5. Copy and configure environment
cp .env.example .env
# Edit .env — add at least one LLM API key

# 6. Set up frontend
cd frontend && npm install && cd ..

# 7. Start development services
docker compose up -d db redis

# 8. Verify setup
make test
```

---

## Development Workflow

### Makefile Targets

The `Makefile` provides the standard development workflow:

```bash
make dev          # Start API server with hot reload (uvicorn --reload)
make dev-frontend # Start Next.js dev server
make lint         # Run ruff check + format check
make format       # Auto-fix lint issues + format code
make typecheck    # Run mypy static type checking
make test         # Run test suite (pytest -v)
make test-cov     # Run tests with coverage report (terminal + HTML)
make pre-commit   # Run lint + typecheck + test (full CI check)
```

### Recommended Daily Workflow

```bash
# 1. Pull latest changes
git pull origin main

# 2. Create a feature branch
git checkout -b feat/your-feature-name

# 3. Make changes, then:
make format       # Auto-fix and format
make typecheck    # Check types
make test         # Run tests

# 4. Run full pre-commit check before pushing
make pre-commit

# 5. Commit and push
git add .
git commit -m "feat: add feature description"
git push origin feat/your-feature-name
```

### Pre-Commit Hooks

When you run `pre-commit install`, the following hooks run automatically on every commit:

| Hook | What it does |
|------|-------------|
| `ruff` | Lint + auto-fix Python code |
| `ruff-format` | Format Python code |
| `mypy` | Type check (excludes tests/) |
| `trailing-whitespace` | Remove trailing whitespace |
| `end-of-file-fixer` | Ensure files end with a newline |
| `check-yaml` | Validate YAML syntax |
| `check-toml` | Validate TOML syntax |
| `check-json` | Validate JSON syntax |
| `check-added-large-files` | Block files > 500KB |
| `detect-private-key` | Block commits containing private keys |

To skip pre-commit hooks (emergency only):
```bash
git commit -m "message" --no-verify
```

---

## Project Structure

```
solvyn/
├── solvyn/                     # Main Python package
│   ├── __init__.py             # Version (0.1.0)
│   ├── main.py                 # FastAPI app factory
│   ├── config.py               # Pydantic settings
│   ├── cli.py                  # CLI commands (13)
│   ├── logging_config.py       # JSON logging setup
│   │
│   ├── agents/                 # Agent system (9 agents)
│   │   ├── base.py             # BaseAgent abstract class
│   │   ├── registry.py         # AgentRegistry
│   │   ├── graph.py            # AgentGraph DAG executor
│   │   ├── schema.py           # Data models + enums
│   │   ├── checkpoint.py       # Human-in-the-loop checkpoints
│   │   ├── memory.py           # pgvector long-term memory
│   │   ├── retry.py            # Retry handler + dead letter
│   │   ├── communication.py    # Inter-agent messaging
│   │   ├── rate_limiter.py     # Token bucket rate limiter
│   │   ├── cost_tracker.py     # Token usage + cost tracking
│   │   └── *.py                # Individual agent implementations
│   │
│   ├── api/v1/                 # REST API
│   │   ├── health.py           # /health, /health/deep
│   │   ├── sessions.py         # Session CRUD
│   │   └── workflows.py        # Workflow execution
│   │
│   ├── db/                     # Database
│   │   ├── models.py           # 9 SQLAlchemy models
│   │   ├── session.py          # Async engine + session
│   │   └── migrations/         # Alembic migrations
│   │
│   ├── llm/                    # LLM gateway
│   │   ├── router.py           # Multi-provider router
│   │   └── providers/          # Provider implementations
│   │
│   ├── message_bus/
│   │   └── redis.py            # Redis pub/sub + streams
│   │
│   ├── multi_tenant/
│   │   ├── auth.py             # Auth + rate limiting middleware
│   │   └── middleware.py        # Tenant context middleware
│   │
│   ├── observability/
│   │   ├── metrics.py          # Prometheus metrics
│   │   ├── tracing.py          # Trace manager
│   │   ├── audit.py            # Audit logger
│   │   ├── websocket.py        # WebSocket manager
│   │   └── analytics_api.py    # Analytics endpoints
│   │
│   └── workflows/
│       ├── templates.py        # 3 workflow templates
│       ├── executor.py         # Workflow executor
│       └── task_generator.py   # Spec → tasks generator
│
├── tests/                      # Test suite
│   ├── conftest.py             # Shared fixtures
│   ├── test_agents.py          # Agent tests
│   ├── test_workflows.py       # Workflow tests
│   ├── test_api.py             # API endpoint tests
│   ├── test_config.py          # Config tests
│   ├── test_observability.py   # Metrics/traces/audit tests
│   └── ...                     # Other test modules
│
├── frontend/                   # Next.js 15 dashboard
├── k8s/                        # Kubernetes manifests
├── monitoring/                 # Prometheus + Grafana configs
├── .github/workflows/          # CI/CD pipeline
├── pyproject.toml              # Python project config
├── Makefile                    # Dev workflow commands
├── Dockerfile                  # API Docker image
├── docker-compose.yml          # Local services
└── .pre-commit-config.yaml     # Pre-commit hooks
```

---

## Code Style & Conventions

### Ruff Configuration

**Source**: `pyproject.toml` → `[tool.ruff]`

| Setting | Value |
|---------|-------|
| Line length | 160 characters |
| Target version | Python 3.12 |
| Lint rules | `E` (pycodestyle), `F` (pyflakes), `I` (isort), `N` (pep8-naming), `W` (warnings), `UP` (pyupgrade) |

```bash
# Check style
make lint

# Auto-fix
make format
```

### MyPy Configuration

**Source**: `pyproject.toml` → `[tool.mypy]`

| Setting | Value |
|---------|-------|
| Python version | 3.12 |
| Strict mode | `true` |
| Ignore missing imports | `true` |

```bash
# Run type checking
make typecheck
```

### Naming Conventions

| Entity | Convention | Example |
|--------|-----------|---------|
| Modules | `snake_case.py` | `product_manager.py` |
| Classes | `PascalCase` | `ProductManagerAgent` |
| Functions/methods | `snake_case` | `async def run()` |
| Variables | `snake_case` | `session_id` |
| Constants | `UPPER_SNAKE_CASE` | `SYSTEM_PROMPT` |
| Private members | `_prefix` | `self._buckets` |
| Type variables | `PascalCase` or `_T` | `Any` |

### Import Order

Imports follow the `isort` convention enforced by ruff:
1. Standard library imports
2. Third-party imports
3. First-party (`solvyn.*`) imports

### Async Patterns

- All database operations use `async_session_factory()` via async context managers
- LLM calls use `httpx.AsyncClient` for async HTTP
- Agent execution is `async def execute(self, input_data)`
- Tests use `pytest-asyncio` with `asyncio_mode = "auto"`

### Error Handling

- Agent `run()` wraps `execute()` in try/except, sets status to `FAILED` on exception
- LLM router tries all providers before raising `RuntimeError("All LLM providers failed")`
- Workflow executor catches per-node exceptions and propagates failure downstream
- Audit logger wraps persistence in try/except — logging failures don't crash the system

---

## How to Add a New Agent

### Step-by-Step

**1. Create the agent class** in `solvyn/agents/`:

```python
# solvyn/agents/my_agent.py
from typing import Any
from solvyn.agents.base import BaseAgent
from solvyn.agents.schema import AgentCapability, AgentMetadata

SYSTEM_PROMPT = """You are a MyAgent. Your role is to:
- Do something useful
- Follow specific instructions
"""

class MyAgent(BaseAgent):
    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            agent_type="my-agent",               # Unique identifier (kebab-case)
            name="My Agent",                     # Display name
            description="Does something useful", # One-line description
            capabilities=[AgentCapability.CODE_GENERATION],  # Pick from enum or add new
        )

    async def execute(self, input_data: dict[str, Any]) -> dict[str, Any]:
        # Extract relevant inputs
        task = input_data.get("task", input_data.get("text", ""))

        # Call LLM
        response = await self.llm_generate(
            prompt=f"Process this task: {task}",
            system_prompt=SYSTEM_PROMPT,
        )

        # Return structured output
        return {
            "task": task,
            "result": response.content,
            "provider": response.provider,
            "model": response.model,
            "cost": response.cost,
        }
```

**2. Register the agent** in `solvyn/agents/__init__.py`:

```python
from solvyn.agents.my_agent import MyAgent

agent_registry.register(MyAgent)

__all__ = [
    # ... existing agents ...
    "MyAgent",
]
```

**3. Add a capability** (if needed) in `solvyn/agents/schema.py`:

```python
class AgentCapability(StrEnum):
    # ... existing capabilities ...
    MY_CAPABILITY = "my_capability"
```

**4. Write tests** in `tests/`:

```python
# tests/test_my_agent.py
import pytest
from solvyn.agents.my_agent import MyAgent

@pytest.mark.asyncio
async def test_my_agent_metadata():
    agent = MyAgent()
    assert agent.metadata.agent_type == "my-agent"

@pytest.mark.asyncio
async def test_my_agent_execute():
    agent = MyAgent()
    result = await agent.run({"task": "test"})
    assert "result" in result
```

**5. Verify**:

```bash
make typecheck
make test
solvyn agents          # Should show your new agent
solvyn chat my-agent   # Should start interactive chat
```

---

## How to Add a New Workflow Template

**1. Define the template** in `solvyn/workflows/templates.py`:

```python
my_workflow = WorkflowTemplate(
    name="my-workflow",
    description="Description of what this workflow does",
    steps=[
        WorkflowStep(
            agent_type="product-manager",
            input_map={"requirement": "requirement"},
            description="Step 1",
        ),
        WorkflowStep(
            agent_type="backend-engineer",
            input_map={"specification": "product-manager.output"},
            depends_on=["product-manager"],
            description="Step 2 (depends on step 1)",
        ),
    ],
    default_inputs={"requirement": "Enter your input here"},
)
```

**2. Register the template** in the `WORKFLOW_TEMPLATES` dict (same file):

```python
WORKFLOW_TEMPLATES: dict[str, WorkflowTemplate] = {
    "feature": feature_workflow,
    "bugfix": bugfix_workflow,
    "release": release_workflow,
    "my-workflow": my_workflow,  # Add here
}
```

**3. Verify**:

```bash
solvyn workflows                    # Should show your new workflow
solvyn pipeline my-workflow -i "..." # Should run it
```

---

## How to Add a New LLM Provider

**1. Create the provider class** in `solvyn/llm/providers/`:

```python
# solvyn/llm/providers/my_provider.py
from typing import Any
import httpx
from solvyn.config import settings
from solvyn.llm.providers.base import LLMProvider, LLMResponse

class MyProvider(LLMProvider):
    BASE_URL = "https://api.my-provider.com/v1"

    def __init__(self):
        self.api_key = settings.my_provider_api_key
        self.http_client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )

    async def generate(self, prompt, system_prompt=None, model=None, **kwargs):
        # Implement the generate method
        ...

    async def close(self):
        await self.http_client.aclose()

    def get_cost_per_token(self, model: str) -> tuple[float, float]:
        # Return (prompt_cost_per_million, completion_cost_per_million)
        return (0.50, 1.00)
```

**2. Add the API key to config** in `solvyn/config.py`:

```python
class Settings(BaseSettings):
    # ... existing fields ...
    my_provider_api_key: str = ""
```

**3. Add the env var** to `.env.example`:

```
MY_PROVIDER_API_KEY=
```

**4. Register in the router** in `solvyn/llm/router.py`:

```python
from solvyn.llm.providers.my_provider import MyProvider

class LLMRouter:
    def __init__(self):
        self.providers = {
            "openrouter": OpenRouterProvider(),
            "groq": GroqProvider(),
            "deepseek": DeepSeekProvider(),
            "my-provider": MyProvider(),  # Add here
        }
```

Add to the fallback order inside `generate()`:

```python
fallback_order = [
    p for p in ["openrouter", "groq", "deepseek", "my-provider"]
    if p != provider_name and p in self.providers
]
```

**5. Update CLI** in `solvyn/cli.py` — add to `_get_providers_info()`.

**6. Add K8s secret** entry to `k8s/secrets.example.yaml`:

```yaml
MY_PROVIDER_API_KEY: "..."
```

---

## Testing Guidelines

### Test Configuration

**Source**: `pyproject.toml` → `[tool.pytest.ini_options]`

```toml
asyncio_mode = "auto"     # Automatically runs async tests
testpaths = ["tests"]     # Test directory
```

### Test Structure

```
tests/
├── conftest.py                     # Shared fixtures (DB, Redis, test client)
├── __init__.py
├── test_config.py                  # Config validation
├── test_agents.py                  # Agent lifecycle tests
├── test_workflows.py               # Workflow execution tests
├── test_api.py                     # HTTP endpoint tests
├── test_agent_graph.py             # DAG execution tests
├── test_agent_schema.py            # Schema validation
├── test_memory_checkpoint_retry.py # Memory, checkpoint, retry tests
├── test_rate_limiter_cost.py       # Rate limiter + cost tracking
├── test_llm_redis_multi_tenant.py  # LLM, Redis, tenant tests
├── test_observability.py           # Metrics, traces, audit tests
├── test_sessions.py                # Session API tests
├── test_auth_logging.py            # Auth + logging tests
└── test_cli.py                     # CLI command tests
```

### Running Tests

```bash
# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=solvyn --cov-report=term --cov-report=html

# Run specific test file
pytest tests/test_agents.py -v

# Run specific test
pytest tests/test_agents.py::test_agent_metadata -v

# Run with output on failures only
pytest tests/ -v --tb=short
```

### Writing Tests

Use `pytest-asyncio` for async tests (auto-detected):

```python
import pytest
from solvyn.agents.product_manager import ProductManagerAgent

@pytest.mark.asyncio
async def test_product_manager_metadata():
    agent = ProductManagerAgent()
    assert agent.metadata.agent_type == "product-manager"
    assert agent.metadata.name == "Product Manager"
```

Use `conftest.py` for shared fixtures — database connections, test clients, etc.

### Coverage Requirements

- Target: >80% for new code
- Focus on: agent execution, workflow DAG resolution, API endpoints, middleware chain

---

## Pull Request Process

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/my-feature
   ```

2. **Make your changes**, following the code conventions in this guide.

3. **Run the full pre-commit check**:
   ```bash
   make pre-commit
   ```
   This runs lint, typecheck, and tests — the same checks CI will run.

4. **Ensure all tests pass** and coverage does not decrease significantly.

5. **Push your branch** and create a PR against `main`.

6. **Fill out the PR template** with:
   - What the change does
   - Why it is needed
   - How to test it
   - Any breaking changes

7. **Request review** from maintainers.

8. **CI will run automatically** — check that all jobs pass (lint, test, typecheck).

9. **Address review feedback** with additional commits.

10. **Merge** when approved and CI is green.

### PR Size Guidelines

- Prefer small, focused PRs (one feature/fix per PR)
- If the PR is large, include a summary of why it needs to be large
- Split unrelated changes into separate PRs

---

## Commit Conventions

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <description>

[optional body]

[optional footer]
```

### Types

| Type | Use for |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation changes |
| `style` | Code style/formatting (no logic change) |
| `refactor` | Code restructuring (no feature/fix) |
| `test` | Adding or updating tests |
| `chore` | Build process, dependencies, tooling |
| `perf` | Performance improvement |
| `ci` | CI/CD changes |
| `security` | Security fixes |

### Examples

```
feat: add my-agent implementation with code generation capability
fix: resolve .output reference resolution in workflow executor
docs: add API reference for sessions endpoint
test: add edge case coverage for retry handler
chore: update pyproject.toml dependencies
security: fix API key comparison using constant-time hmac
```

---

## Issue Reporting

### Bug Reports

Include:
1. **Solvyn version** (`solvyn version`)
2. **Steps to reproduce**
3. **Expected behavior**
4. **Actual behavior** (include error messages/logs)
5. **Environment** (OS, Python version, Docker/K8s)

### Feature Requests

Include:
1. **Problem statement** — what problem does this solve?
2. **Proposed solution** — how should it work?
3. **Alternatives considered** — other approaches you've thought about
4. **Impact** — which components are affected?

---

## Documentation Standards

- Every new agent must include a `description` in its `AgentMetadata`
- Every new API endpoint must be documented with its route, method, request/response format
- Every new workflow template must include a `description` and `default_inputs`
- Code comments should explain **why**, not **what** (the code shows what)
- Python docstrings follow the project pattern (no specific style enforced — be consistent with existing code)

### Documentation Files

| File | Audience |
|------|----------|
| `README.md` | New users (overview + quick start) |
| `ARCHITECTURE.md` | Developers (system design + components) |
| `SECURITY.md` | Security-conscious users + auditors |
| `DEPLOYMENT.md` | DevOps / operators |
| `CONTRIBUTING.md` | Contributors (this file) |
| `API.md` | API consumers + frontend developers |
