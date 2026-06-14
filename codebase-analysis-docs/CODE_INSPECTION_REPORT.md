# Solvyn Code Inspection Report — Bugs & Gaps

> Consolidated findings from 3 parallel deep-inspections of 60+ source files
> Generated: 2026-06-11

---

## Executive Summary

| Severity | Count |
|----------|-------|
| **CRITICAL** | 3 |
| **HIGH** | 10 |
| **MEDIUM** | 25 |
| **LOW** | 22 |
| **Total** | **60** |

---

## CRITICAL (3) — Must fix before any production use

### C1. Tenant Impersonation via Middleware Ordering Conflict

**Files:** `solvyn/multi_tenant/auth.py:117-121`, `solvyn/multi_tenant/middleware.py:64-65`, `solvyn/main.py:64-66`

`auth_middleware` and `tenant_middleware` independently resolve tenant identity and conflict. Due to ASGI middleware LIFO ordering:
1. `auth_middleware` (registered 4th, runs 1st) validates the API key and sets tenant context to the key-owning tenant.
2. `tenant_middleware` (registered 3rd, runs 3rd) calls `tenant_context.clear()`, then re-resolves from `X-Tenant-Slug` header.

**Attack vector:** Send a valid API key for Tenant A with `X-Tenant-Slug: tenant-b`. After auth sets context to Tenant A, tenant_middleware wipes it and sets Tenant B. The request proceeds fully authorized as Tenant B — cross-tenant data access and privilege escalation.

### C2. `/agents` Endpoint Completely Unreachable (Route Shadowing)

**File:** `solvyn/api/v1/workflows.py:21 vs 80`

`GET /{name}` (line 21) is registered before `GET /agents` (line 80). FastAPI matches routes in registration order, so `GET /api/v1/workflows/agents` is captured by the `/{name}` parameter route. Since "agents" is not a valid workflow name, it returns 404 instead of the agents list. **The agents endpoint is dead.**

### C3. Rate Limiter Reads Wrong Tenant Context

**Files:** `solvyn/multi_tenant/auth.py:156`, `solvyn/main.py:64-65`

`RateLimitMiddleware` reads `tenant_context.tenant_id` at line 156 to look up per-tenant rate limits. But `tenant_middleware` (which sets the "real" tenant context) runs AFTER the rate limiter due to LIFO ordering. The rate limiter applies limits to whatever tenant auth_middleware set — which may differ from the tenant the request is actually authorized as. In shared-secret mode (no `X-Tenant-Slug`), no tenant context is set, so ALL requests share a single "unknown" rate limit bucket.

---

## HIGH (10) — Fix before production deployment

### H1. LLM Fallback Misses Most Common Failure Modes

**File:** `solvyn/llm/router.py:226`

Fallback catches only `(ConnectionError, TimeoutError, httpx.TimeoutException)`. Missing:
- `httpx.ConnectError` (connection refused, DNS failure) — does NOT inherit from Python's `ConnectionError`
- `httpx.HTTPStatusError` (4xx/5xx) — raised by ALL three providers when `response.status_code >= 400`

A rate-limited (429) or server-error (500) response from the primary provider propagates as unhandled exception — **no fallback triggered**.

### H2. Cache Poisoning on Fallback

**File:** `solvyn/llm/router.py:236-238`

When primary fails and fallback succeeds, the fallback response is cached under the **original model's** cache key. Requesting `openai/gpt-4o` via OpenRouter but getting `llama-3.3-70b-versatile` from Groq — the Groq response is cached as if it were GPT-4o. Subsequent requests receive the wrong model's output, corrupting cost accounting and quality expectations.

### H3. `_validate_tenant_api_key` O(n) Full-Scan Per Request

**File:** `solvyn/multi_tenant/auth.py:49-58`

Every authenticated request with `X-Tenant-Slug` triggers a query that loads ALL active tenants into memory, then iterates to find a matching hash. No index on `api_key_hash`. With hundreds of tenants, this is a severe performance bottleneck.

### H4. WebSocket Tenant ID Not Validated Against API Key

**File:** `solvyn/observability/analytics_api.py:94-99`

WebSocket endpoint at `/ws/{tenant_id}` authenticates the API key but does NOT validate that the URL's `tenant_id` matches the key-owning tenant. A user with Tenant A's key can connect to Tenant B's WebSocket channel and receive Tenant B's real-time events.

### H5. Empty `api_key_secret` Weakens Authentication

**Files:** `solvyn/config.py:23`, `solvyn/multi_tenant/auth.py:122-127`

`api_key_secret` defaults to `""`. The server starts and runs with this default (only a log warning). Any key that happens to hash to the same SHA-256 as the empty string would pass verification.

### H6. Prometheus Metrics Exposed Without Authentication

**Files:** `solvyn/main.py:73-74`, `solvyn/multi_tenant/auth.py:20`

The `/metrics` endpoint is in `PUBLIC_PATHS` and mounted at app level — accessible without auth. Exposes detailed operational data: HTTP rates, LLM volumes, token consumption, agent counts, workflow statistics. Reconnaissance goldmine.

### H7. WebSocket Skips Per-Tenant API Key Validation

**File:** `solvyn/observability/analytics_api.py:97`

WebSocket auth calls `verify_api_key(api_key, settings.api_key_secret)` — shared-secret only. Does NOT call `_validate_tenant_api_key()`. Per-tenant authentication is completely bypassed for WebSocket clients.

### H8. Prometheus High-Cardinality Label Explosion

**File:** `solvyn/observability/metrics.py:23-25`

`endpoint` label uses raw URL path (`/api/v1/sessions/abc-123-def`). Every unique path segment creates a new time series. Routes with dynamic IDs generate unbounded label combinations → exponential memory growth in Prometheus → potential OOM.

### H9. `cost_tracker` Crashes on Empty Result Sets

**File:** `solvyn/agents/cost_tracker.py:60, 76`

`result.one()` raises `NoResultFound` when no rows match. For a new session or tenant with zero token usage, these methods crash instead of returning zeros. Should use `one_or_none()` with fallback.

### H10. Checkpoint `save()` Never Stores `session_id`

**File:** `solvyn/agents/checkpoint.py:27-40`

`CheckpointManager.save()` accepts `session_id` as first parameter but never writes it to the `MemoryModel` record. Meanwhile `list_for_session()` filters by `session_id`. The query always returns empty. **Session-scoped checkpoint listing is completely broken.**

---

## MEDIUM (25)

| # | File | Lines | Issue |
|---|------|-------|-------|
| M1 | `agents/graph.py` | 96-102 | `.replace(".output", "")` replaces ALL occurrences, not just suffix. `"system.output.data.output"` → `"system.data"` |
| M2 | `agents/graph.py` | 96-102 | When predecessor output not found, raw string `"agent_type.output"` is returned as data — silent data corruption |
| M3 | `agents/graph.py` | 239 | Skipped nodes added to `failed` set — `overall_status="failed"` even when failures are all cascading skips |
| M4 | `agents/memory.py` | 45-46 | `uuid.UUID(self.tenant_id)` crashes on invalid UUID strings with no validation |
| M5 | `agents/memory.py` | 156 | LIKE wildcard abuse: `query="%"` matches ALL records |
| M6 | `agents/checkpoint.py` | 57 | `uuid.UUID(session_id)` crashes on non-UUID strings |
| M7 | `agents/rate_limiter.py` | 62-63 | `check()` leaks LLM token when agent check fails after LLM check succeeds |
| M8 | `agents/communication.py` | 151-161 | Broadcast messages (`to_agent=None`) silently dropped — no code path delivers them |
| M9 | `agents/communication.py` | 156 | `handle_message()` return value discarded — agents can't respond to messages |
| M10 | `workflows/task_generator.py` | 44-78 | `build_workflow_graph` never appends to `edges` — graph always has zero edges |
| M11 | `workflows/executor.py` | 140-141 | Graph persistence errors silently swallowed — caller thinks workflow succeeded |
| M12 | `workflows/templates.py` + `executor.py` | 8, 39, 45 | `parallel_with` field defined and populated but never read — dead code |
| M13 | `api/v1/sessions.py` | 28-35 | Session created with `_DEFAULT_UUID` when no tenant context — violates FK constraint |
| M14 | `api/v1/analytics_api.py` | 40-44 | Trace not found returns HTTP 200 instead of 404 |
| M15 | `db/session.py` | 7 | No connection pool config — defaults (pool_size=5) too small for production |
| M16 | `multi_tenant/rate_limiter.py` | 45-55 | Redis connection never reset to `None` on failure — stale connection reused |
| M17 | `main.py` | 77-85 | Missing `Content-Security-Policy` header |
| M18 | `llm/embeddings.py` | 64, 91 | New `httpx.AsyncClient` per call — no connection pooling, TLS handshake overhead |
| M19 | `observability/tracing.py` | 39, 60 | Spans list grows without bound per trace — memory leak |
| M20 | `observability/audit.py` | 80-99 | `action`/`session_id` filters applied in Python, not SQL — performance + incorrect limit |
| M21 | `message_bus/redis.py` | 25-32 | Redis connection closed before PubSub — wrong teardown order, may hang |
| M22 | `message_bus/redis.py` | 20-23 | `connect()` doesn't close existing connection — connection pool leak on reconnect |
| M23 | `prompts/registry.py` | 10 | Jinja2 `autoescape=False`, no sandbox — SSTI risk if templates are user-influenced |
| M24 | `guardrails/pii_scrubber.py` | 67-85 | Non-string dict values bypass PII scrubbing |
| M25 | `mcp/tool.py` | 27, 54-78 | `timeout_seconds` defined but never enforced — tools can hang indefinitely |

---

## LOW (22)

| # | File | Lines | Issue |
|---|------|-------|-------|
| L1 | `agents/base.py` | 95-103 | `resume_from_checkpoint` doesn't validate agent is in `WAITING_APPROVAL` status |
| L2 | `agents/base.py` | 37-38 | `run()` creates new `ExecutionNode` each call — previous node data lost |
| L3 | `agents/base.py` | 85 | `request_approval` shares mutable `self.state` dict with checkpoint — post-approval mutations affect checkpoint |
| L4 | `agents/registry.py` | 16 | Instantiates agent class just to read metadata — side effects risk |
| L5 | `agents/retry.py` | 30-41 | `max_retries=0` → `raise None` → `TypeError` |
| L6 | `agents/rate_limiter.py` | 62-63 | Token bucket not thread-safe (documented warning, safe in single-process async) |
| L7 | `agents/memory.py` | 198-207 | `compact()` only processes 500 records per call — incomplete |
| L8 | `agents/communication.py` | 15, 19-20 | `_handlers` dict populated but never read — dead code |
| L9 | `agents/communication.py` | 186-191 | `_find_agent_by_type` returns first match only — no load balancing |
| L10 | `agents/communication.py` | 17, 23-25 | No cleanup of stale `_active_agents` — memory leak on crash |
| L11 | `agents/__init__.py` + checkpoint/cost_tracker/memory/audit | various | `_to_uuid()` and `_DEFAULT_UUID` duplicated across 5 files |
| L12 | `guardrails/content_moderator.py` | 89-105 | Toxicity patterns ASCII-only — bypassed by Unicode homoglyphs |
| L13 | `guardrails/pii_scrubber.py` | 9-22 | PII patterns ASCII-only — bypassed by Unicode, URL encoding, zero-width chars |
| L14 | `guardrails/pii_scrubber.py` | 51-64 | Sequential redaction causes cross-type misclassification |
| L15 | `observability/websocket.py` | 36-50 | Empty connection sets not pruned after broadcast cleanup |
| L16 | `observability/analytics_api.py` | 22-27 | Duplicate metrics endpoint (both `/metrics` and `/observability/metrics`) |
| L17 | `llm/router.py` | 114, 143 | Cache exceptions silently swallowed — corrupted Redis entries persist forever |
| L18 | `llm/providers/openrouter.py` | 51 | Empty `choices` array → `IndexError` (bypasses fallback) |
| L19 | `a2a/protocol.py` | 56-68 | `from_dict` doesn't coerce enum/int types from raw JSON |
| L20 | `eval/evaluator.py` | 79 | `ZeroDivisionError` when `min_length == max_length` |
| L21 | `prompts/registry.py` | 63-71 | Missing prompt version silently returns wrong version |
| L22 | `api/v1/health.py` | 43-44 | Degraded health returns HTTP 200 — monitoring tools miss outage |

---

## Top 5 Fixes by Impact

1. **Fix middleware ordering** (C1, C2, C3) — The tenant impersonation and rate limiter context bugs are security-critical. The middleware must ensure `tenant_middleware` runs BEFORE `auth_middleware` so auth sees the correct tenant context, and route registration order must put parameterized routes AFTER static routes.

2. **Fix LLM fallback exception types** (H1) — Add `httpx.ConnectError` and `httpx.HTTPStatusError` to the fallback catch list. Without this, the multi-provider resilience strategy is broken for the most common failure modes.

3. **Fix Prometheus metric label normalization** (H8) — Replace raw URL path with route template (e.g., use FastAPI's `request.scope["route"].path` or a path normalizer). This prevents OOM in production monitoring.

4. **Fix cache poisoning on fallback** (H2) — When a fallback provider is used, cache the response under the fallback model's key, or don't cache at all.

5. **Fix cost_tracker crash on empty results** (H9) — Replace `result.one()` with `one_or_none()` and return zero-defaults when no rows exist.
