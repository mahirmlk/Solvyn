# Solvyn Security

> Security policies, practices, and reporting for Solvyn v0.1.0

---

## Table of Contents

1. [Security Model](#security-model)
2. [Authentication & Authorization](#authentication--authorization)
3. [Multi-Tenant Isolation](#multi-tenant-isolation)
4. [Rate Limiting](#rate-limiting)
5. [Secret Management](#secret-management)
6. [Input Validation & Sanitization](#input-validation--sanitization)
7. [LLM Security](#llm-security)
8. [Audit Logging & Monitoring](#audit-logging--monitoring)
9. [Dependency Security](#dependency-security)
10. [OWASP Top 10 Mitigations](#owasp-top-10-mitigations)
11. [Compliance Considerations](#compliance-considerations)
12. [Security Reporting](#security-reporting)
13. [Production Hardening](#production-hardening)

---

## Security Model

Solvyn follows a defense-in-depth approach with multiple security layers:

```
┌──────────────────────────────────────────┐
│              Transport Security           │
│    TLS (ingress) + HTTPS termination     │
├──────────────────────────────────────────┤
│            Authentication                 │
│    X-API-Key header with SHA-256 HMAC    │
├──────────────────────────────────────────┤
│            Authorization                  │
│    Multi-tenant (contextvars scoping)    │
├──────────────────────────────────────────┤
│            Rate Limiting                  │
│    Per-IP (100/min) + Per-tenant (500/min)│
├──────────────────────────────────────────┤
│            Input Validation               │
│    FastAPI + Pydantic schemas            │
├──────────────────────────────────────────┤
│            Audit Trail                    │
│    All agent actions logged to DB        │
├──────────────────────────────────────────┤
│            Monitoring                     │
│    Prometheus metrics + 7 alert rules    │
└──────────────────────────────────────────┘
```

---

## Authentication & Authorization

### API Key Authentication

All non-public API endpoints require the `X-API-Key` header:

| Component | Detail |
|-----------|--------|
| **Header** | `X-API-Key: <your-api-key>` |
| **Public paths** | `/api/v1/health`, `/metrics`, `/docs`, `/openapi.json` |
| **Hash algorithm** | SHA-256 |
| **Comparison** | `hmac.compare_digest()` (constant-time) |
| **Config field** | `API_KEY_SECRET` (env var) |

**Source**: `solvyn/multi_tenant/auth.py`

```python
# How API key verification works:
# 1. Client sends X-API-Key header
# 2. Server hashes the key with SHA-256
# 3. If API_KEY_SECRET is a 64-char hex string, compare directly
# 4. Otherwise, hash API_KEY_SECRET and compare both hashes with hmac.compare_digest
```

### Startup Security Warning

If `API_KEY_SECRET` is set to the default value (`change-me-to-a-random-secret`), the application logs a warning at startup:

```
INSECURE: api_key_secret is using the default value. Set a custom secret before production deployment.
```

> **⚠️ HUMAN INPUT NEEDED**: The current auth model uses a single shared secret for all tenants. The `Tenant.api_key_hash` column exists but is unused. Per-tenant API keys should be implemented for production multi-tenant deployments.

---

## Multi-Tenant Isolation

### Tenant Resolution

Tenant context is established per-request via the `X-Tenant-Slug` header:

```python
# solvyn/multi_tenant/middleware.py
async def tenant_middleware(request, call_next):
    tenant_context.clear()           # Reset before request
    await resolve_tenant(request)     # Lookup by X-Tenant-Slug → set contextvars
    response = await call_next(request)
    tenant_context.clear()           # Reset after request
    return response
```

### Context Variable Scoping

`contextvars.ContextVar` is used for tenant isolation — it automatically propagates through the async call stack without explicit parameter passing:

```python
# Read tenant anywhere in the request lifecycle
tenant_id = tenant_context.tenant_id  # UUID | None
tenant_slug = tenant_context.tenant_slug  # str | None
```

### Database-Level Isolation

All models that store tenant-scoped data include a `tenant_id` foreign key to the `tenants` table:

- `sessions.tenant_id` → `tenants.id`
- `agent_instances.tenant_id` → `tenants.id`
- `memory.tenant_id` → `tenants.id`
- `artifacts.tenant_id` → `tenants.id`
- `tasks.tenant_id` → `tenants.id`
- `token_usage.tenant_id` → `tenants.id`

The `Session` creation endpoint uses `tenant_context.tenant_id`:

```python
session = Session(tenant_id=tenant_context.tenant_id, ...)
```

### Active Tenant Check

The `resolve_tenant()` function only sets the tenant context if `tenant.is_active == True`:

```python
if tenant and tenant.is_active:
    tenant_context.set(tenant.id, tenant.slug)
```

### WebSocket Tenant Isolation

WebSocket connections are keyed by `tenant_id:session_id`, preventing cross-tenant message leakage:

```python
async def connect(websocket, tenant_id, session_id=None):
    key = f"{tenant_id}:{session_id or '*'}"
    self._connections[key].add(websocket)
```

---

## Rate Limiting

Two layers of rate limiting protect the API:

### HTTP Rate Limiting (Middleware)

**Source**: `solvyn/multi_tenant/auth.py` — `RateLimitMiddleware`

| Scope | Limit | Window | Default |
|-------|-------|--------|---------|
| Per IP | 100 requests | 60 seconds | `max_per_ip = 100` |
| Per tenant | 500 requests | 60 seconds | `max_per_tenant = 500` |

Response on limit breach:
```json
{
    "error": "Rate limit exceeded",
    "retry_after": 60,
    "scope": "ip"
}
```

### Agent/LLM Rate Limiting (Token Bucket)

**Source**: `solvyn/agents/rate_limiter.py` — `RateLimiter`

| Scope | Rate | Burst |
|-------|------|-------|
| Per-tenant LLM calls | 10/sec | 20 |
| Per-agent execution | 5/sec | 10 |

> **⚠️ HUMAN INPUT NEEDED**: Both rate limiters are in-memory only. In multi-worker deployments (multiple uvicorn workers, multiple pods), each process maintains its own counter, allowing bypass. A Redis-backed rate limiter is needed for production at scale.

---

## Secret Management

### Principles

1. **Never hardcode secrets** — all secrets come from environment variables or Kubernetes Secrets
2. **Never commit secrets** — `.env` is gitignored; `k8s/secrets.example.yaml` is a template
3. **Pre-commit protection** — the `detect-private-key` hook catches accidental commits

### Configuration Secrets

| Secret | Source | Purpose |
|--------|--------|---------|
| `DATABASE_URL` | Env / K8s ConfigMap | PostgreSQL connection (contains password) |
| `REDIS_URL` | Env / K8s ConfigMap | Redis connection |
| `OPENROUTER_API_KEY` | Env / K8s Secret | LLM provider API key |
| `GROQ_API_KEY` | Env / K8s Secret | LLM provider API key |
| `DEEPSEEK_API_KEY` | Env / K8s Secret | LLM provider API key |
| `API_KEY_SECRET` | Env / K8s Secret | Master API key for auth |

### Kubernetes Secrets

Sensitive values are stored as Kubernetes Secrets (not ConfigMaps):

```yaml
# k8s/secrets.example.yaml
apiVersion: v1
kind: Secret
metadata:
  name: solvyn-secrets
  namespace: solvyn
type: Opaque
stringData:
  POSTGRES_PASSWORD: "change-me"
  OPENROUTER_API_KEY: "sk-or-v1-..."
  GROQ_API_KEY: "gsk_..."
  DEEPSEEK_API_KEY: "sk-..."
  API_KEY_SECRET: "change-me-to-a-random-secret"
```

Deployed via `kubectl apply -f k8s/secrets.yaml` (file is gitignored; `.example.yaml` is committed).

---

## Input Validation & Sanitization

### API Level

FastAPI + Pydantic provide automatic request validation:
- Type coercion and validation on all route parameters
- Request body is parsed and validated against type hints
- Invalid requests return 422 with structured error details

### LLM Output Handling

All agent outputs are treated as untrusted strings. The system:
- Parses LLM JSON output with `json.loads()` wrapped in try/except
- Falls back to line-by-line parsing if JSON is malformed
- Never `eval()` or `exec()` LLM-generated code without sandboxing

### Database Queries

SQLAlchemy ORM with parameterized queries prevents SQL injection:
```python
# Parameterized — safe
await db.execute(select(Memory).where(Memory.key == prefixed_key))

# No raw string interpolation used in application queries
```

---

## LLM Security

### Prompt Injection Mitigations

1. **System prompts are separate** — system instructions are passed as `system_prompt` (not interleaved with user input), reducing injection surface
2. **Output validation** — LLM responses are parsed as JSON where expected; parsing failures trigger fallback handling
3. **No direct execution** — LLM-generated code is stored in artifacts, not auto-executed
4. **Agent isolation** — agents run in independent execution contexts with tenant scoping

### Data Leakage Prevention

1. **Tenant-isolated memory** — `AgentMemory` queries are scoped by tenant
2. **No cross-tenant LLM context** — each agent instance is bound to a single tenant
3. **Audit trail** — all LLM interactions are tracked with `token_usage` records

### Provider Security

1. **API keys in environment variables** — never in code or committed config
2. **HTTPS only** — all provider API calls use HTTPS (hardcoded in provider clients)
3. **Timeout enforcement** — all HTTP clients have 60-second timeouts

### Rate Limiting on LLM Calls

Per-tenant rate limiting prevents abuse and unexpected cost spikes:
```
TokenBucket: 10 calls/sec, burst 20 per tenant
```

---

## Audit Logging & Monitoring

### Audit Logger

**Source**: `solvyn/observability/audit.py`

All agent actions are persisted to the `Memory` table with key prefix `audit:`:

```json
{
    "action": "workflow_run",
    "agent_type": "product-manager",
    "session_id": "uuid",
    "tenant_id": "default",
    "timestamp": "2024-01-01T00:00:00+00:00",
    "details": { "input_length": 500, "success": true }
}
```

Queryable via:
- CLI: `solvyn audit --action workflow_run --agent product-manager --limit 10`
- API: `GET /api/v1/observability/audit?action=workflow_run&agent_type=product-manager&limit=50`

### Prometheus Alert Rules

| Alert | Severity | Trigger |
|-------|----------|---------|
| SolvynHighErrorRate | **Critical** | HTTP 5xx rate > 5% over 5 minutes |
| SolvynHighLLMFailures | Warning | Any LLM API failures in 2 minutes |
| SolvynAgentExecutionFailure | Warning | Any agent failures in 5 minutes |
| SolvynHighTokenUsage | Info | >100K tokens/sec over 5 minutes |
| SolvynNoActiveAgents | Info | Zero active agents for 10 minutes |
| SolvynWorkflowFailures | Warning | Workflow failures in 5 minutes |
| SolvynMemoryOperationsSlow | Warning | P95 memory operation > 1 second |

### Structured Logging

All logs are JSON-formatted (configurable) with timestamp, level, logger name, message, and exception traceback. This enables log aggregation systems (ELK, Loki) to parse and index them.

---

## Dependency Security

### Pre-Commit Hooks

**Source**: `.pre-commit-config.yaml`

| Hook | Purpose |
|------|---------|
| `ruff` | Lint + auto-fix |
| `ruff-format` | Code formatting |
| `mypy` | Static type checking |
| `trailing-whitespace` | Remove trailing whitespace |
| `end-of-file-fixer` | Ensure files end with newline |
| `check-yaml` | Validate YAML syntax |
| `check-toml` | Validate TOML syntax |
| `check-json` | Validate JSON syntax |
| `check-added-large-files` | Block commits > 500KB |
| `detect-private-key` | **Block commits containing private keys** |

### CI/CD Pipeline Checks

1. **Lint**: `ruff check` — catches code issues before merge
2. **Type check**: `mypy` with `strict = true` — catches type errors
3. **Tests**: `pytest` with coverage — runs against real PostgreSQL + Redis service containers

### Dependency Scanning

> **⚠️ HUMAN INPUT NEEDED**: No automated dependency vulnerability scanning (e.g., Dependabot, Snyk, pip-audit) is currently configured. Consider adding a `pip-audit` step to the CI pipeline.

---

## OWASP Top 10 Mitigations

| # | Vulnerability | Mitigation in Solvyn |
|---|---------------|---------------------|
| **A01: Broken Access Control** | Multi-tenant contextvars isolation; active tenant check; auth middleware on all non-public routes |
| **A02: Cryptographic Failures** | SHA-256 hashing with constant-time comparison; TLS via ingress; secrets in env vars/K8s Secrets |
| **A03: Injection** | SQLAlchemy ORM (parameterized queries); JSON parsing with try/except; no raw SQL in application code |
| **A04: Insecure Design** | Rate limiting per-IP and per-tenant; defense-in-depth middleware stack |
| **A05: Security Misconfiguration** | Pre-commit hooks check YAML/JSON/TOML; `.env.example` documents all config; default `API_KEY_SECRET` triggers warning |
| **A06: Vulnerable Components** | Pre-commit hooks; CI lint/test; Python 3.12+ with modern dependency versions |
| **A07: Auth Failures** | API key auth on all non-public routes; constant-time hash comparison; `is_active` check on tenants |
| **A08: Software & Data Integrity** | CI builds from source with dependency pinning; Docker images built from locked `pyproject.toml` |
| **A09: Logging & Monitoring Failures** | Structured JSON logging; 7 Prometheus alert rules; audit trail persisted to DB |
| **A10: SSRF** | LLM provider calls use fixed base URLs (no user-controlled URLs); httpx with 60s timeout |

---

## Compliance Considerations

### GDPR

| Area | Implementation |
|------|---------------|
| **Data minimization** | Only `tenant_id`, `session_id`, `agent_type`, and agent I/O stored |
| **Right to access** | Audit log queryable by `tenant_id` |
| **Right to deletion** | `Tenant` cascades delete to `Session` and `AgentInstance` |
| **Data location** | All data in PostgreSQL/Redis (configurable host/region) |

> **⚠️ HUMAN INPUT NEEDED**: No DPA (Data Processing Agreement) framework, cookie consent, or explicit GDPR deletion endpoints are implemented. These must be added for EU deployments.

### SOC 2

> **⚠️ HUMAN INPUT NEEDED**: The audit log provides a foundation for SOC 2, but formal access review, change management, and incident response procedures are not yet documented.

---

## Security Reporting

### Reporting a Vulnerability

If you discover a security vulnerability in Solvyn, please **do not** open a public GitHub issue.

Send details to the project maintainers via a private channel. Include:

1. Description of the vulnerability
2. Steps to reproduce
3. Potential impact
4. Suggested fix (if available)

We aim to acknowledge reports within 48 hours and publish fixes within 7 days for critical issues.

---

## Production Hardening

### Before Deploying to Production

1. **Change all default secrets**:
   - `API_KEY_SECRET` — must not be `change-me-to-a-random-secret`
   - All LLM API keys — set to real values
   - `POSTGRES_PASSWORD` — use a strong, randomly generated password

2. **Enable TLS**:
   - Use the ingress TLS configuration (`k8s/ingress.yaml`) with cert-manager
   - Set `solvyn.example.com` to your actual domain

3. **Implement Redis-backed rate limiting**:
   - Replace in-memory `RateLimitMiddleware` and `RateLimiter` with Redis-backed equivalents for multi-worker deployments

4. **Add dependency scanning**:
   - Integrate `pip-audit` or Dependabot for automated vulnerability detection

5. **Configure backup strategy**:
   - Schedule regular PostgreSQL backups (pg_dump or cloud provider snapshots)
   - Test restoration procedures

6. **Set up monitoring alerts**:
   - Configure Prometheus alertmanager with real notification channels (Slack, PagerDuty, email)
   - Review and tune alert thresholds for your usage patterns

7. **Restrict CORS origins**:
   - Set `allowed_origins` to specific domains rather than `["*"]` (default allows `localhost:3000` only)

8. **Implement per-tenant API keys**:
   - Use the existing `Tenant.api_key_hash` column for per-tenant authentication
   - Deprecate the single shared `API_KEY_SECRET` for multi-tenant deployments

9. **Enable tracing for production observability**:
   - Call `tracing_manager.enable()` with a compatible backend (currently in-memory only)

10. **Review LLM prompt safety**:
    - Audit all agent system prompts for unintended behaviors
    - Implement output filtering for sensitive data patterns
