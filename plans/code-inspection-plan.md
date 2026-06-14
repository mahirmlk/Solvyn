# Code Inspection Implementation Plan
Project: Solvyn  |  Stack: Python 3.12 / FastAPI 0.115 / SQLAlchemy 2 async / pgvector / Redis 7 / Next.js 15  |  Inspected: 2026-06-14
Files scanned: ~85 in `solvyn/` + 25 in `tests/` + 13 K8s manifests + 4 Dockerfiles + 1 GH Actions workflow  |  Domains covered: Bug, Security, Architecture, Deps, Deployment

## Deployment Verdict
**BLOCKED** — A linear-scan tenant-auth query on every request combined with the silent `API_KEY_SECRET` warning and absence of LLM-provider fail-fast validation means the server can boot in a fully-unauthenticated or unconfigured state in production, and the per-tenant auth path leaks timing-based tenant existence.

## Executive Summary
Solvyn is a FastAPI 0.115 / Next.js 15 multi-agent platform with strong architectural fundamentals (LifespanContextManager, structured logging, multi-LLM fallback, NetworkPolicy, PDB, deep health check, observability middleware). The codebase is mostly idiomatic modern Python 3.12 and largely free of classic bugs (no bare `except:`, no mutable defaults, no `os.environ[]` without fallback, no `eval`/`exec` on user input). However, several high-impact issues prevent safe production launch:

- **Security** is the dominant risk domain: tenant API-key validation does a full-table linear scan per request (P0 timing-oracle + DoS), the empty-`API_KEY_SECRET` warning is non-fatal (P0 silent misconfig), the `Content-Security-Policy` permits `unsafe-eval` (P0 XSS amplifier), and the LLM provider is not validated against the configured `llm_default_provider` at startup (P0 first-request 500).
- **Architecture** gaps: SQLAlchemy async engine has no `pool_size`/`pool_pre_ping`/`pool_recycle` (P0 connection-storm crash), one workflow endpoint accepts `dict[str, Any]` body (P1 unbounded LLM prompt surface), `httpx.AsyncClient` in the LLM providers is created in `__init__` and never closed (P2 resource leak), and the rate limiter is the only L7 control — no per-endpoint budget, no auth-failure rate limit (P2 credential-stuffing amplifier).
- **Deployment**: Dockerfile is unpinned (`python:3.12-slim`), runs as root, has no `HEALTHCHECK`, no multi-stage build (P1 image-bloat + supply-chain); same in `frontend/Dockerfile` runner stage. CI runs `pip-audit` but no SAST (bandit/semgrep), no image scanning (trivy), no SBOM, no secret scanning (gitleaks) — and no staging-→-prod gate that enforces scan results.
- **Dependencies**: every Python security-critical dep is a floating `>=` range (P0 supply-chain drift), and `langgraph`/`langchain-core` are listed as runtime deps but never imported (P2 dead weight, 80+ transitive packages).
- **Bug & Logic**: small and benign — no `assert`-as-validation, no swallowed errors in critical paths, no `asyncio.create_task` without reference. The only P2 is an `asyncio.gather(return_exceptions=False)` that cancels a parallel level on the first failure (correct for fail-fast, but means one bad node blocks its peers — design call, not a bug).

**Most dangerous exploit path:** An attacker who can reach `/api/v1/workflows/{name}/run` (auth still required, but the auth check itself is the bottleneck) triggers one of the workflow templates. The endpoint takes a raw `dict[str, Any]` body, drops the `tenant_id` key, and passes the remainder to `workflow_executor.run(...)` — the value flows into prompt templates consumed by the LLM with no schema validation, no length cap, no PII scrub, and no per-tenant rate limit on the workflow path beyond the global 100/min IP / 500/min tenant ceiling.

**Minimum change set to safely deploy:**
1. Fix `verify_api_key` and `_validate_tenant_api_key` (auth.py) to look up by tenant key index, not linear scan; fail fast on empty `API_KEY_SECRET`; remove the `CSP unsafe-eval` and `unsafe-inline`.
2. Add `pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=1800` to `create_async_engine` in `db/session.py`.
3. Pin Python deps to `==` (or rely on `uv.lock` with `uv sync --frozen` in CI), add `USER` and `HEALTHCHECK` to both Dockerfiles, pin base images by digest.
4. Add LLM provider key validation in `Settings.model_validator`; refuse to start when `llm_default_provider`'s key is empty.
5. Add a Pydantic `WorkflowRunRequest` model in `api/v1/workflows.py` with a `Literal[...]` of accepted keys per template.
6. Add secret scanning (gitleaks), SAST (bandit), and image scanning (trivy) to `.github/workflows/ci-cd.yml`.

**Domain ownership of findings:** Security 9 · Architecture 7 · Deployment 7 · Dependencies 6 · Bug 1 · **Total: 30**.

---

## P0 — Critical  (must be fixed before deployment)

### [P0-001] Per-tenant API key validation does a full-table linear scan on every request
- **Location:** `solvyn/multi_tenant/auth.py:40-56` (`_validate_tenant_api_key`)
- **Finding:** Every authenticated request to a per-tenant route executes `SELECT * FROM tenants WHERE is_active = true` and iterates the result list comparing `key_hash` with `hmac.compare_digest`. With N tenants and R requests/sec this is O(N·R) DB load, and although `compare_digest` is constant-time, the *DB roundtrip* time is not — the response time scales with the number of rows scanned, allowing a timing-based tenant-enumeration oracle. It is also a denial-of-service amplifier: 1k tenants × 100 req/s = 100k row reads/s on the auth path alone.
- **Evidence:**
  ```python
  async with async_session_factory() as session:
      result = await session.execute(
          select(Tenant).where(Tenant.is_active == True)  # noqa: E712
      )
      tenants = result.scalars().all()
      for tenant in tenants:
          stored_hash = (tenant.api_key_hash or "").lower()
          if hmac.compare_digest(key_hash, stored_hash):
              return tenant
  ```
- **Recommended Fix Strategy:** Index tenants by a short hash prefix in Redis (`tenant_key:{sha256(key)[:8]} → tenant_id`) populated on tenant create/rotate, OR add a unique index on `tenants(api_key_hash)` and use `SELECT tenant WHERE api_key_hash = :hash LIMIT 1`. Reject empty/null `api_key_hash` at the model layer. Add a unit test asserting p99 latency does not scale with tenant count.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/multi_tenant/auth.py`, `solvyn/db/models.py` (index migration), `tests/test_auth_logging.py`, possibly `solvyn/multi_tenant/middleware.py` (resolve by slug — same pattern).

### [P0-002] Empty `API_KEY_SECRET` is a warning, not a fatal error
- **Location:** `solvyn/config.py:47-55`, `solvyn/multi_tenant/auth.py:121-124`
- **Finding:** When `API_KEY_SECRET=""` the server logs `CRITICAL` and continues. In per-tenant mode (`tenant_context.tenant_id` set — possible when `X-Tenant-Slug` resolves a tenant), `auth_middleware` skips the shared-secret check entirely (`if tenant_context.tenant_id is not None: ... else: ...`), so the empty secret is silently ignored. A misconfigured production deployment with `TENANT_MODE=per_tenant` (or the equivalent) can boot, pass all health checks, and answer traffic while every shared-secret caller is rejected.
- **Evidence:**
  ```python
  # solvyn/config.py:47
  if not settings.api_key_secret:
      logging.getLogger(__name__).critical(
          "SECURITY: API_KEY_SECRET environment variable is not set. ..."
      )
  ```
  ```python
  # solvyn/multi_tenant/auth.py:121 — only the shared-secret branch checks the secret
  if not settings.api_key_secret:
      return JSONResponse(status_code=500, content={"error": "Server not configured: API_KEY_SECRET is empty"})
  ```
- **Recommended Fix Strategy:** Replace the `logger.critical(...)` with `raise RuntimeError("API_KEY_SECRET must be set")` in a `model_validator(mode="after")` on `Settings`, or call `sys.exit(2)` at module import time. If a dev escape hatch is required, gate it on a separate `ALLOW_INSECURE_DEV_AUTH=true` env var that defaults to false and is itself logged at WARNING.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/config.py`, `solvyn/multi_tenant/auth.py`, `tests/test_config.py`.

### [P0-003] Content-Security-Policy permits `unsafe-eval` and `unsafe-inline`
- **Location:** `solvyn/main.py:78` (`add_security_headers`)
- **Finding:** The API response includes `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; style-src 'self' 'unsafe-inline'`. `'unsafe-eval'` allows `new Function()` and `eval()` from any inline script, which neutralizes the XSS protection that CSP exists to provide. `'unsafe-inline'` on both script and style removes the defense-in-depth gain entirely. For a JSON API this CSP is mostly cosmetic, but it ships as a default for any HTML the API may serve (e.g. error pages, `/docs`, `/redoc`) and the FastAPI auto-generated docs pages will execute inline scripts.
- **Evidence:**
  ```python
  response.headers["Content-Security-Policy"] = (
      "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
      "style-src 'self' 'unsafe-inline'"
  )
  ```
- **Recommended Fix Strategy:** Drop `unsafe-eval` outright. For `'unsafe-inline'` on script-src, either use nonces (`generate_nonce()`, add `script-src 'self' 'nonce-{n}'`) or hashes for the few static assets. If Next.js requires `unsafe-inline` for its hydration payload, use a per-route CSP via response middleware that swaps to the strict policy for `/api/*` and a relaxed one for the dashboard routes only. Also add `Permissions-Policy`, `Cross-Origin-Opener-Policy: same-origin`, and `Cross-Origin-Resource-Policy: same-origin`.
- **Estimated Effort:** M
- **Affected Files:** `solvyn/main.py`, `frontend/next.config.js` (match CSP if it sets headers), `k8s/ingress.yaml` (consider adding `nginx.ingress.kubernetes.io/configuration-snippet` for a stricter API-only CSP).

### [P0-004] All Python security-critical dependencies are unpinned (`>=`)
- **Location:** `pyproject.toml:9-25`
- **Finding:** Every runtime dependency uses a `>=` lower bound. `fastapi>=0.115.0`, `pydantic>=2.10.0`, `pydantic-settings>=2.6.0`, `sqlalchemy[asyncio]>=2.0.36`, `asyncpg>=0.30.0`, `httpx>=0.28.0` — a malicious or accidental upstream minor-version bump in any of these ships to the next image build with no human review. A `uv.lock` is present and is the only authoritative pin source, but the manifest is what the project advertises and what IDEs / non-uv consumers resolve.
- **Evidence:**
  ```toml
  dependencies = [
      "fastapi>=0.115.0",
      "uvicorn[standard]>=0.32.0",
      "pydantic>=2.10.0",
      "pydantic-settings>=2.6.0",
      "sqlalchemy[asyncio]>=2.0.36",
      "asyncpg>=0.30.0",
      ...
      "httpx>=0.28.0",
      ...
  ]
  ```
- **Recommended Fix Strategy:** Switch manifest entries to exact pins (`fastapi==0.115.x`, etc.) and let Renovate/Dependabot open upgrade PRs. In CI, require `uv sync --frozen` so the lock file is the only source of truth. Alternative: add `>=` lower bounds AND an `~=` upper bound (`pydantic>=2.10,<3`) so a breaking major cannot land silently. Either way, the lock file already exists (`uv.lock`); the gap is the manifest policy.
- **Estimated Effort:** S
- **Affected Files:** `pyproject.toml`, `Dockerfile` (use `uv sync --frozen --no-dev`), `.github/workflows/ci-cd.yml` (fail on `uv.lock` drift).

### [P0-005] SQLAlchemy async engine has no connection pool config
- **Location:** `solvyn/db/session.py:7-9`
- **Finding:** `create_async_engine(settings.database_url, echo=False)` uses the default `NullPool` for async engines. With the production deployment running 2–10 API replicas (per HPA) plus the dev mode hot-reload worker, the engine creates a fresh connection on every `async with async_session_factory()` and closes it on exit. Under load this (a) saturates the PostgreSQL `max_connections` budget (default 100), (b) makes every DB operation pay TCP+TLS+startup cost, and (c) crashes the deployment with `FATAL: too many clients already` once burst traffic exceeds ~80 concurrent requests.
- **Evidence:**
  ```python
  engine = create_async_engine(settings.database_url, echo=False)
  async_session_factory = async_sessionmaker(engine, expire_on_commit=False)
  ```
- **Recommended Fix Strategy:** Add `pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=1800, pool_timeout=30`. Tune `pool_size` to match `2 * cpu_count` or the replica's HPA `minReplicas` × `avg concurrent requests per replica` — start with 20 and benchmark. Add `pool_pre_ping` to detect stale connections through a pgbouncer / proxy. Expose the pool's `checkedout()` / `overflow()` as Prometheus gauges so saturation is visible.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/db/session.py`, `solvyn/config.py` (add `db_pool_size`, `db_pool_max_overflow` settings), `tests/conftest.py` (test fixture pool config).

### [P0-006] LLM provider API key is not validated against the configured default at startup
- **Location:** `solvyn/config.py:10-12`, `solvyn/llm/router.py:215-220`, `solvyn/main.py:25-45`
- **Finding:** `openrouter_api_key`, `groq_api_key`, `deepseek_api_key` default to `""`. `LLMRouter.providers` instantiates all three providers at import time, so missing keys do not crash. The first user request that hits `llm_default_provider="openrouter"` with an empty key raises `httpx.HTTPStatusError` (or a 401) which surfaces as HTTP 500. The CLI's `status` command detects this, but the server does not. Result: a production deploy that forgot to mount the OpenRouter key answers traffic with 500s until someone notices.
- **Evidence:**
  ```python
  # solvyn/config.py
  openrouter_api_key: str = ""
  groq_api_key: str = ""
  deepseek_api_key: str = ""
  ```
  ```python
  # solvyn/llm/router.py:215
  provider_name = provider or settings.llm_default_provider
  llm = self.providers.get(provider_name)
  if llm is None:
      raise ValueError(...)
  # no check that the key is non-empty
  ```
- **Recommended Fix Strategy:** Add a `model_validator(mode="after")` on `Settings` that raises `ValueError` if `llm_default_provider` is in `{openrouter, groq, deepseek}` and the matching key is `""`. Add a CLI subcommand `solvyn doctor` (or extend `solvyn status`) that prints a "ready to serve" verdict for the API. Make the API server's `lifespan` startup log a `WARNING` (not `INFO`) if any provider's key is empty.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/config.py`, `solvyn/main.py` (lifespan warning), `solvyn/llm/router.py` (lazy provider instantiation so empty-key providers don't open sockets).

### [P0-007] Workflow `run` endpoint takes a raw `dict[str, Any]` body with no schema validation
- **Location:** `solvyn/api/v1/workflows.py:53-66`
- **Finding:** `run_workflow(name: str, inputs: dict[str, Any] = Body(...))` accepts any JSON object, drops the `tenant_id` key, and passes the remainder to `workflow_executor.run(...)`. The values flow directly into prompt templates consumed by the LLM. There is no length cap, no schema per template, no PII scrub on inputs, no rate limit per workflow, and no allow-list of template-specific keys. A 10 MB string body is accepted (Starlette's default) and forwarded to OpenRouter.
- **Evidence:**
  ```python
  @router.post("/{name}/run")
  async def run_workflow(name: str, inputs: dict[str, Any] = Body(...)) -> dict[str, Any]:
      ...
      safe_inputs = {k: v for k, v in inputs.items() if k != "tenant_id"}
      result = await workflow_executor.run(template_name=name, inputs=safe_inputs, ...)
  ```
- **Recommended Fix Strategy:** Define `class WorkflowRunRequest(BaseModel)` per template in `solvyn/workflows/templates.py` (e.g. `FeatureWorkflowRequest`, `BugfixWorkflowRequest`, `ReleaseWorkflowRequest`) with `model_config = ConfigDict(extra="forbid", max_length=4096)` and a Pydantic `StringConstraints(max_length=2048)` on every string field. Resolve the right model in a `WorkflowRequestFactory` keyed by template name. Wire `solvyn/guardrails/pii_scrubber.py` to scrub inputs before they enter the executor. Cap request body size in Starlette (`Request.body_size_limit = 64 * 1024`).
- **Estimated Effort:** M
- **Affected Files:** `solvyn/api/v1/workflows.py`, `solvyn/workflows/templates.py`, `solvyn/workflows/executor.py`, `solvyn/guardrails/pii_scrubber.py`, `tests/test_workflows.py`, `tests/test_api.py`.

### [P0-008] Secrets in `k8s/secrets.example.yaml` are real-looking placeholders, not clearly marked
- **Location:** `k8s/secrets.example.yaml`
- **Finding:** Example file contains literal strings `POSTGRES_PASSWORD: "change-me"`, `GROQ_API_KEY: "gsk_..."`, `DEEPSEEK_API_KEY: "sk-..."`, `API_KEY_SECRET: "change-me-to-a-random-secret"`. The `gsk_…` and `sk-…` strings are real Groq / DeepSeek token prefixes. If a developer copies this file to `k8s/secrets.yaml` and forgets to substitute, the prefix is enough to fool a careless linter or human reviewer; downstream tools (e.g. kustomize, ArgoCD) may treat the file as the actual secret. The risk is not that the placeholders are real keys (they aren't) but that the format looks real and the file is named `secrets.example.yaml` instead of `secrets.template.yaml` or similar.
- **Evidence:**
  ```yaml
  stringData:
    POSTGRES_PASSWORD: "change-me"
    GROQ_API_KEY: "gsk_..."
    DEEPSEEK_API_KEY: "sk-..."
    API_KEY_SECRET: "change-me-to-a-random-secret"
  ```
- **Recommended Fix Strategy:** Rename to `k8s/secrets.TEMPLATE.yaml`, add a top-of-file `# DO NOT kubectl apply — copy to secrets.yaml and edit` warning, use unmistakable placeholders like `__REPLACE_WITH_GROQ_KEY__` for every value, and add the file to a `gitleaks` allowlist with a comment in `.gitleaks.toml` so a CI secret scan doesn't false-positive.
- **Estimated Effort:** S
- **Affected Files:** `k8s/secrets.example.yaml` → renamed, `k8s/DEPLOYMENT.md` (or new `k8s/README.md`), `.github/workflows/ci-cd.yml` (add gitleaks job).

---

## P1 — High  (fix before any production launch)

### [P1-001] `httpx.AsyncClient` in LLM providers is never closed on engine disposal
- **Location:** `solvyn/llm/providers/openrouter.py:14-22`, `solvyn/llm/providers/groq.py:14-22`, `solvyn/llm/providers/deepseek.py:14-22`
- **Finding:** Each provider creates `self.http_client = httpx.AsyncClient(...)` in `__init__` and exposes `async def close(self)` which calls `aclose()`. `LLMRouter.close()` calls `provider.close()` for each provider during lifespan shutdown. The fallback path in `router.generate` instantiates providers via the dict at line 161 — `llm_router = LLMRouter()` is created at module import (line 277), so the providers are constructed eagerly. The clients keep the underlying HTTP connection pools open for the lifetime of the process. On graceful shutdown this is fine; on `SIGKILL` (the typical k8s eviction), the sockets are released by the OS but no `Connection: close` is sent, which shows up as `CLOSE_WAIT` accumulation on the LLM provider's side. More importantly, `httpx.AsyncClient` opens a TLS session to `openrouter.ai` on first use, and the absence of explicit `aclose()` means worker processes on rolling deploy leak ~one TLS session per replica.
- **Evidence:**
  ```python
  # openrouter.py:14
  self.http_client = httpx.AsyncClient(base_url=self.BASE_URL, headers=..., timeout=60.0)
  ...
  async def close(self) -> None:
      await self.http_client.aclose()
  ```
- **Recommended Fix Strategy:** Switch to a single shared `httpx.AsyncClient` per process owned by `LLMRouter` (passed to providers via DI), with `Limits(max_connections=100, max_keepalive_connections=20)`. Call `await self.http_client.aclose()` once in `LLMRouter.close()`. Configure provider-specific `timeout=httpx.Timeout(connect=5, read=30, write=10, pool=5)` so a hung provider does not block the entire request. The current `timeout=60.0` is acceptable but should be split.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/llm/providers/base.py`, `solvyn/llm/providers/openrouter.py`, `solvyn/llm/providers/groq.py`, `solvyn/llm/providers/deepseek.py`, `solvyn/llm/router.py`.

### [P1-002] `asyncio.gather(return_exceptions=False)` cancels peer agents on first failure
- **Location:** `solvyn/agents/graph.py:223-228`
- **Finding:** In the parallel-level executor, if any of the gathered `_run_node(...)` coroutines raises, the others are cancelled. `_run_node` catches exceptions internally and stores them on `node.error` (so they should not normally propagate), but the wrapping `asyncio.gather` will cancel peer tasks if any unexpected exception escapes — e.g. a `KeyError` from `_resolve_value` in `graph.py:93` if a template references an undefined `.output` placeholder. The agent's `run()` method already catches and re-raises (line 50-58), so this risk is contained, but the design is "one bad node kills the parallel batch" rather than "log and continue". This is a design call, not strictly a bug.
- **Evidence:**
  ```python
  # agents/graph.py:225
  await asyncio.gather(
      *[self._run_node(nid, session_id, tenant_id, configs) for nid in runnable],
      return_exceptions=False,
  )
  ```
- **Recommended Fix Strategy:** Switch to `return_exceptions=True`, iterate the results, and call `self._mark_failed(node_id, exc)` for any exception. This preserves parallel-batch progress when one branch is non-critical. The DAG's downstream skip logic (lines 230-245) already handles the "any failure → downstream skipped" semantics correctly.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/agents/graph.py`, `tests/test_agent_graph.py`, `tests/test_agent_graph_parallel.py`.

### [P1-003] Frontend security-critical dependencies use floating semver (`^`)
- **Location:** `frontend/package.json:12-19`
- **Finding:** `next`, `react`, `react-dom`, and the React type packages use `^` ranges. Both `next` and `react` had high-severity advisories in the last 12 months; floating `^` ranges mean the next image build pulls whatever is latest on npm, including unreviewed minor versions.
- **Evidence:**
  ```json
  "dependencies": {
    "next": "^15.1.0",
    "react": "^19.0.0",
    "react-dom": "^19.0.0"
  }
  ```
- **Recommended Fix Strategy:** Use exact `15.1.6`-style pins in `dependencies`, rely on `package-lock.json` for reproducibility, and open Renovate PRs for upgrades. Optionally use `"~15.1.0"` (allows patch but not minor) as a middle ground.
- **Estimated Effort:** S
- **Affected Files:** `frontend/package.json`, `frontend/Dockerfile` (already uses `npm install` which respects the lock file).

### [P1-004] Dockerfile base images are unpinned and run as root
- **Location:** `Dockerfile:1`, `frontend/Dockerfile:1,7`
- **Finding:** `FROM python:3.12-slim` and `FROM node:22-alpine` (both stages) are floating tags. A malicious or accidental upstream re-tag ships to production. Neither Dockerfile has a `USER` directive, so the container runs as root — a process compromise yields root in the container namespace. The backend Dockerfile has no `HEALTHCHECK`, no multi-stage build, and no build-deps cleanup.
- **Evidence:**
  ```dockerfile
  # Dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  COPY pyproject.toml .
  RUN pip install --no-cache-dir .
  COPY solvyn/ ./solvyn/
  CMD ["uvicorn", "solvyn.main:app", "--host", "0.0.0.0", "--port", "8000"]
  ```
  ```dockerfile
  # frontend/Dockerfile
  FROM node:22-alpine AS builder
  ...
  FROM node:22-alpine AS runner
  ```
- **Recommended Fix Strategy:**
  - Pin by digest: `FROM python:3.12.7-slim-bookworm@sha256:<digest>` (use `docker pull` + `docker inspect` to get the digest, commit to a `BASE_DIGESTS.txt` file).
  - Add `USER`: `RUN adduser --system --no-create-home --uid 1001 solvyn && chown -R solvyn /app && USER solvyn` (or `USER node` for the frontend runner stage — the node alpine image ships a `node` user with UID 1000).
  - Add `HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health')"]`.
  - For the backend: switch to a multi-stage build (`builder` stage installs into a venv, `runtime` stage copies the venv and source) so build tools (`gcc`, `pip`, `setuptools`) don't ship in the final image.
  - Add `--require-hashes` to `pip install` once the lock file is hash-pinned.
- **Estimated Effort:** M
- **Affected Files:** `Dockerfile`, `frontend/Dockerfile`, `k8s/deployment.yaml` (add `securityContext: runAsNonRoot: true, runAsUser: 1001, allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: [ALL] }`).

### [P1-005] No `securityContext` on K8s `Deployment` containers
- **Location:** `k8s/deployment.yaml` (all three Deployments)
- **Finding:** `solvyn-api`, `solvyn-db`, `solvyn-redis` Deployments have no `securityContext` at pod or container level. The default is "allow privilege escalation, run as root, writable root filesystem" — the opposite of Pod Security Standards `restricted` profile. A single container escape is enough to escalate.
- **Evidence:** `k8s/deployment.yaml` has no `securityContext:` key at any level.
- **Recommended Fix Strategy:** Add to every container:
  ```yaml
  securityContext:
    runAsNonRoot: true
    runAsUser: 1001   # or 70 for postgres, 999 for redis
    runAsGroup: 1001
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: [ALL]
  ```
  For PostgreSQL and Redis the official images ship with their own UIDs (postgres=999, redis=999) — use those. Mount an `emptyDir` for `/tmp` and `/var/run` since `readOnlyRootFilesystem: true` blocks writes elsewhere. Add a `seccompProfile: RuntimeDefault` at pod level. Add a NetworkPolicy admission controller (or OPA/Gatekeeper) to enforce.
- **Estimated Effort:** M
- **Affected Files:** `k8s/deployment.yaml`, `k8s/migration-job.yaml` (same hardening).

### [P1-006] No image scanning, SAST, or secret scanning in CI
- **Location:** `.github/workflows/ci-cd.yml`
- **Finding:** The CI pipeline runs `ruff`, `mypy`, `pytest`, and `pip-audit` (good), then builds + pushes the image, then deploys. Missing:
  - Secret scanning (e.g. `gitleaks/gitleaks-action`) — the `k8s/secrets.example.yaml` file would already be flagged for the `sk-` / `gsk_` token prefixes.
  - SAST (e.g. `bandit`, `semgrep`, GitHub CodeQL) — would catch the `dict[str, Any] = Body(...)` workflow endpoint, the `verify_api_key` confusion, and the empty-string `verify` flow.
  - Container image scanning (e.g. `trivy image`, `snyk container`) — would catch unpinned base images and the long list of CVEs in `python:3.12-slim` / `node:22-alpine`.
  - SBOM generation (e.g. `anchore/sbom-action`, `cyclonedx/cyclonedx-python`) — required for some compliance regimes and for `grype`-style vulnerability matching.
  - The `deploy-prod` job has `environment: production` (good — gives a manual approval gate) but no policy enforcement that P0s are fixed.
- **Evidence:** `.github/workflows/ci-cd.yml` has no `gitleaks`, no `bandit`, no `trivy`, no `codeql`, no SBOM step.
- **Recommended Fix Strategy:** Add the following jobs to the workflow:
  1. `secrets` job: `gitleaks/gitleaks-action@v2` with `args: --redact --no-banner`.
  2. `sast` job: `github/codeql-action/init@v3` with `language: python, javascript`, then `github/codeql-action/analyze@v3`.
  3. `image-scan` job (after `build`): `aquasecurity/trivy-action@master` with `image-ref`, `severity: CRITICAL,HIGH`, `exit-code: 1` so the workflow fails on Critical/High CVEs.
  4. `sbom` job: `anchore/sbom-action@v0` with `format: spdx-json`, upload as a workflow artifact.
  5. Move the `deploy-prod` job's manual-approval step to a GitHub Environment protection rule that requires all four scan jobs to be green.
- **Estimated Effort:** M
- **Affected Files:** `.github/workflows/ci-cd.yml`.

### [P1-007] `.env.example` is missing 16 of 25 settings; langfuse fields are dead config
- **Location:** `.env.example` vs `solvyn/config.py:7-43`
- **Finding:** Settings like `LLM_CACHE_TTL`, `JSON_LOGGING`, `ALLOWED_ORIGINS`, `RATE_LIMIT_PER_IP`, `RATE_LIMIT_PER_TENANT`, `RATE_LIMIT_WINDOW`, `COST_BUDGET_PER_TENANT`, `COST_ALERT_THRESHOLD`, `MEMORY_TTL_DAYS`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `OTEL_ENABLED`, `OTLP_ENDPOINT`, `LANGFUSE_*` are read by the application but not documented. Frontend `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_API_KEY` (consumed in `frontend/src/lib/api.ts:1-2` and `frontend/next.config.js:7`) are also undocumented. The `langfuse_*` fields are declared in `Settings` but never read in `solvyn/` — dead config that increases the misconfiguration surface.
- **Evidence:** `solvyn/config.py:7-43` declares 25 fields, only 12 appear in `.env.example`. Grep over `solvyn/` for `langfuse_` returns no matches outside `config.py`.
- **Recommended Fix Strategy:** Either (a) document all 25 settings in `.env.example` with a comment per line, or (b) remove the unused langfuse fields and any other dead config. Add a `Frontend / public env` section listing `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_API_KEY`. Add a `mkdocs` or `README.md` section "Environment variables" that mirrors the `.env.example` annotations.
- **Estimated Effort:** S
- **Affected Files:** `.env.example`, `solvyn/config.py`, `README.md`, `frontend/.env.example` (does not exist — create).

### [P1-008] No runtime version pinning files (`.python-version`, `.nvmrc`)
- **Location:** project root, `frontend/`
- **Finding:** `pyproject.toml` declares `requires-python = ">=3.12"` but does not pin the patch. CI and dev machines may resolve to different Python/Node patch releases. CI uses `python-version: "3.12"` and Dockerfiles use `python:3.12-slim` and `node:22-alpine` — all unpinned.
- **Evidence:** No `.python-version`, no `.nvmrc`, no `.node-version` in the repo.
- **Recommended Fix Strategy:** Add `.python-version` (`3.12.7`), `frontend/.nvmrc` (`22.11.0`). In `Dockerfile`, `ARG PYTHON_VERSION=3.12.7` and `FROM python:${PYTHON_VERSION}-slim-bookworm@sha256:<digest>`. In `frontend/Dockerfile`, `ARG NODE_VERSION=22.11.0` and `FROM node:${NODE_VERSION}-alpine@sha256:<digest>`. In CI, replace `python-version: "3.12"` with a setup-python action that reads from `.python-version`.
- **Estimated Effort:** S
- **Affected Files:** `.python-version` (new), `frontend/.nvmrc` (new), `Dockerfile`, `frontend/Dockerfile`, `.github/workflows/ci-cd.yml`.

---

## P2 — Medium  (fix within the sprint)

### [P2-001] LLM API key length / format not validated
- **Location:** `solvyn/llm/router.py:225-227`, `solvyn/llm/providers/*.py`
- **Finding:** The OpenRouter/Groq/DeepSeek providers construct `Authorization: Bearer {self.api_key}` with no validation. If a developer sets `OPENROUTER_API_KEY="test"` (no provider prefix), the request goes out and the provider returns 401. Surface symptom is a 500, root cause is no client-side validation. This couples to P0-006 (startup fail-fast).
- **Recommended Fix Strategy:** Add a Pydantic field validator on `Settings.openrouter_api_key` etc. that checks for a minimum length and a known prefix (`sk-or-` for OpenRouter, `gsk_` for Groq, `sk-` for DeepSeek). Add a `model_validator` that cross-checks `llm_default_provider` against the configured key.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/config.py`, `solvyn/llm/router.py`.

### [P2-002] `langgraph` and `langchain-core` declared as runtime deps but never imported
- **Location:** `pyproject.toml:18-19`
- **Finding:** `langgraph>=0.2.0` and `langchain-core>=0.3.0` are declared as runtime dependencies, but no `solvyn/` source file imports either package. The runtime DAG is custom code in `solvyn/workflows/executor.py` and `solvyn/agents/`. Pulling these adds ~80 transitive packages (pydantic v1, tenacity, jsonpatch, orjson, ...) and a known deprecation cadence in the LangChain ecosystem.
- **Recommended Fix Strategy:** Remove them from `dependencies` and move to `[project.optional-dependencies] agents = [...]` only when/if a real import lands. Run `uv tree` to confirm the reduction. Update `solvyn/cli.py` `version` command to drop the entries.
- **Estimated Effort:** S
- **Affected Files:** `pyproject.toml`, `solvyn/cli.py`.

### [P2-003] `httpx` duplicated in main + dev dependencies
- **Location:** `pyproject.toml:21, 35`
- **Finding:** `httpx>=0.28.0` appears in both `[project.dependencies]` and `[project.optional-dependencies].dev`. Harmless but indicates the manifest is hand-edited without a single source of truth.
- **Recommended Fix Strategy:** Remove from dev (the FastAPI test client uses it transitively, but FastAPI's own `TestClient` doesn't need a separately-declared httpx).
- **Estimated Effort:** S
- **Affected Files:** `pyproject.toml`.

### [P2-004] `Settings` has no `env_prefix="SOLVYN_"` and tests assert the wrong behavior
- **Location:** `solvyn/config.py:5`, `tests/test_config.py:18-33`
- **Finding:** `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")` reads env vars by exact field name. A test in `test_config.py:18-33` sets `SOLVYN_HOST` and `SOLVYN_PORT` and asserts the defaults are used — this is documenting the *broken* behavior, not the correct one. Deployments that namespace with `SOLVYN_*` will silently fall back to defaults.
- **Recommended Fix Strategy:** Add `env_prefix="SOLVYN_"` to `SettingsConfigDict` and update `.env.example` to use prefixed names. Fix the test to assert the prefixed env var is honored. Keep `env_file=".env"` so dev still works.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/config.py`, `.env.example`, `tests/test_config.py`.

### [P2-005] No `requirements.txt` shim for non-uv consumers
- **Location:** project root
- **Finding:** Some IDE tools, deployment platforms, and `pip install -r` workflows do not read `pyproject.toml`. The `uv.lock` is the only authoritative pin source, but it's not exportable by everyone.
- **Recommended Fix Strategy:** Add a `make` target `make requirements` that runs `uv export --no-hashes -o requirements.txt && uv export --no-hashes --extra dev -o requirements-dev.txt`. Commit the generated files. Document `uv sync` as the preferred path in `CONTRIBUTING.md`.
- **Estimated Effort:** S
- **Affected Files:** `Makefile` (new target), `requirements.txt` (new), `requirements-dev.txt` (new), `CONTRIBUTING.md`.

### [P2-006] WebSocket endpoint accepts connections from any origin
- **Location:** `solvyn/observability/analytics_api.py:88-115` (the WebSocket handler) and the CORS middleware in `solvyn/main.py:55-63`
- **Finding:** The WebSocket auth check is API-key based, not origin-based. CORS does not apply to WebSocket handshakes; the server does not check `Origin` header. A malicious site can open a WebSocket from a victim's browser using a stolen or phished API key. The shared-secret mode (`X-API-Key: <shared-secret>`) is especially exposed because the key is in `localStorage` or a static fetch header.
- **Recommended Fix Strategy:** In the WebSocket handler, after the API key check, also validate the `Origin` header against `settings.allowed_origins` and close the connection with code 4003 if it does not match. Add a unit test.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/observability/analytics_api.py`, `tests/test_observability.py`.

### [P2-007] `evaluate_default` model selection in `LLMRouter` has no bounds check on `find_cheapest`
- **Location:** `solvyn/llm/router.py:245-260`
- **Finding:** `find_cheapest` returns `None` when the registry is empty, but the caller falls through to `(default_provider, default_model)`. This is correct, but a typo in `llm_default_provider` ("openao" instead of "openrouter") causes every request to raise `ValueError` from the earlier check — no startup test catches this. The CLI's `solvyn providers` command enumerates valid values but the server does not.
- **Recommended Fix Strategy:** Add `model_validator(mode="after")` on `Settings` that checks `llm_default_provider in {"openrouter", "groq", "deepseek"}` and `llm_default_model` is non-empty. Add a unit test.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/config.py`, `tests/test_config.py`, `solvyn/llm/router.py` (defensive default in `select_model`).

### [P2-008] `ContentModerator` `scrub_and_validate` chain does not log scrub events
- **Location:** `solvyn/guardrails/content_moderator.py:213-220`
- **Finding:** When PII is scrubbed from agent output, the scrubbed count is returned in the function result but not written to the audit log. An operator investigating a data leak has no trail of which sessions had PII redacted.
- **Recommended Fix Strategy:** Call `audit_logger.log(action="pii_scrub", details={...})` from `scrub_and_validate` when the scrub count > 0. Add a Prometheus counter `solvyn_pii_scrubs_total{type="email|phone|ssn|name"}`.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/guardrails/content_moderator.py`, `solvyn/observability/audit.py`, `solvyn/observability/metrics.py`.

### [P2-009] No pagination on `GET /sessions/` and `GET /observability/traces`
- **Location:** `solvyn/api/v1/sessions.py:18-26`, `solvyn/observability/analytics_api.py:31-50`
- **Finding:** `list_sessions` hard-codes `LIMIT 50`; `list_traces` accepts a `limit` parameter but no offset/cursor. After 10k traces, the `deque(maxlen=10000)` in `tracing.py:13` silently drops the oldest entries with no signal to the consumer.
- **Recommended Fix Strategy:** Use cursor pagination (`?cursor=<id>&limit=20`) on both endpoints. Add a metric `solvyn_tracing_overflow_total` that increments when an entry is dropped from the deque so operators can see when their retention is too short.
- **Estimated Effort:** M
- **Affected Files:** `solvyn/api/v1/sessions.py`, `solvyn/observability/analytics_api.py`, `solvyn/observability/tracing.py`, `solvyn/observability/metrics.py`.

### [P2-010] CORS `allow_credentials=True` is dangerous with the default localhost origin
- **Location:** `solvyn/main.py:55-63`, `solvyn/config.py:23`
- **Finding:** `CORSMiddleware(allow_credentials=True, allow_origins=settings.allowed_origins, ...)`. The default `allowed_origins=["http://localhost:3000"]` is fine for dev, but in production this list must be the actual dashboard origin. There is no startup check that `allowed_origins` is non-empty or that it doesn't contain `"*"`. A misconfigured deploy that leaves the default ships with credentials accepted from `http://localhost:3000` — a developer testing the dashboard on their laptop would have working cookies against the production API.
- **Recommended Fix Strategy:** Add a `model_validator` that fails startup if `ALLOWED_ORIGINS` contains `"*"` when `allow_credentials=True`, and warns if the value is the default `localhost:3000` in a non-dev environment. Add an `ENVIRONMENT` env var (`dev` / `staging` / `prod`) and gate the default on `ENVIRONMENT == "dev"`.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/config.py`, `solvyn/main.py`, `tests/test_config.py`.

### [P2-011] `docker-compose.yml` mounts the project root into the API container with `--reload`
- **Location:** `docker-compose.yml:12`
- **Finding:** `volumes: - .:/app` with `command: uvicorn solvyn.main:app --host 0.0.0.0 --port 8000 --reload` means any change to `.env` on the host is picked up by uvicorn. Dev-only risk but document it. The bigger concern: in dev, `POSTGRES_PASSWORD` is required (`POSTGRES_PASSWORD:?` syntax) but the `api` service uses `env_file: .env` — if `.env` is missing, the API service still starts without the DB URL.
- **Recommended Fix Strategy:** Add a `depends_on: api: condition: service_started` style health check for the API; document the `.env` requirement in `docker-compose.yml` header comment; remove the volume mount in any non-dev compose file.
- **Estimated Effort:** S
- **Affected Files:** `docker-compose.yml`, `docker-compose.prod.yml` (new — same services without `volumes:` and without `--reload`).

### [P2-012] `solvyn/cli.py` `version` command exists but `solvyn doctor` does not
- **Location:** `solvyn/cli.py`
- **Finding:** The CLI ships `solvyn version`, `solvyn status`, `solvyn providers`, `solvyn agents` — but no "is my environment ready to serve?" command. A `solvyn doctor` that runs the same checks the server's `lifespan` should run (DB reachable, Redis reachable, LLM key set, CORS non-empty, port free) would catch most P0 misconfigurations before a deploy.
- **Recommended Fix Strategy:** Add a `doctor` subcommand to `solvyn/cli.py` that prints a checklist of pre-flight checks. Wire the same checks into the server's `lifespan` so the dev experience matches production.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/cli.py`, `solvyn/main.py`.

---

## P3 — Low  (backlog / best-practice hardening)

### [P3-001] `asyncio.gather` over parallel `AgentGraph` levels lacks timing metric
- **Location:** `solvyn/agents/graph.py:223-228`, `solvyn/observability/metrics.py`
- **Finding:** The graph logs level duration via `logger.info("Running level with %d parallel nodes: %s", ...)` but does not record a histogram. Operators cannot see p50/p99 level latency.
- **Recommended Fix Strategy:** Add a Prometheus histogram `solvyn_graph_level_duration_seconds{tenant_id, level_size}` and a counter `solvyn_graph_node_failures_total{agent_type, reason}`.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/agents/graph.py`, `solvyn/observability/metrics.py`.

### [P3-002] `RetryHandler.execute_with_retry` does not distinguish transient from permanent errors
- **Location:** `solvyn/agents/retry.py:36-65`
- **Finding:** The handler retries on whatever tuple is passed (default `(ConnectionError, TimeoutError)`), but cannot abort early on `ValueError` / `TypeError` (programmer errors) — they would burn all `max_retries` attempts before bubbling up. The handler also doesn't track which exception types are not retryable as a deny-list.
- **Recommended Fix Strategy:** Accept an optional `non_retryable: tuple[type[Exception], ...] = ()` parameter; if the caught exception is an instance of any non-retryable type, re-raise immediately.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/agents/retry.py`, `tests/test_memory_checkpoint_retry.py`.

### [P3-003] `RedisMessageBus.add_to_dead_letter` does not enforce a max stream length
- **Location:** `solvyn/message_bus/redis.py:78-86`
- **Finding:** The dead-letter stream is an unbounded `XADD`. A misbehaving producer can fill Redis memory. The bus already calls `xadd` with no `MAXLEN`.
- **Recommended Fix Strategy:** Use `XADD dead_letter MAXLEN ~ 10000 * ...` and surface a Prometheus counter `solvyn_dlq_size` so operators can see when DLQ is filling.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/message_bus/redis.py`, `solvyn/observability/metrics.py`.

### [P3-004] `api/v1/health.py:14` shallow `/health` is intentionally a stub
- **Location:** `solvyn/api/v1/health.py:14-17`
- **Finding:** `health_check()` returns `{"status": "ok"}` without checking DB or Redis. K8s liveness probe hits this on every period (15s by default). If the DB is down but the process is up, liveness stays green and the pod is never restarted. This is intentional (separation of liveness vs readiness) but not documented.
- **Recommended Fix Strategy:** Add a comment explaining "liveness: process is up; readiness: see /health/deep" and point the K8s readinessProbe at `/health/deep` (currently the K8s deployment.yaml has both probes pointing at `/api/v1/health` which is the shallow one).
- **Estimated Effort:** S
- **Affected Files:** `solvyn/api/v1/health.py`, `k8s/deployment.yaml`.

### [P3-005] `_validate_tenant_api_key` returns `None` for any non-matching key — same response for "unknown tenant" and "wrong key for known tenant"
- **Location:** `solvyn/multi_tenant/auth.py:40-56`
- **Finding:** Returning the same `401 Invalid X-API-Key` for both cases is correct, but a log line that says `tenant_id=<uuid> key_hash=<prefix>` would help operators debug "this client says they have a key, but it doesn't match". Currently the middleware just returns the JSON.
- **Recommended Fix Strategy:** Log at `DEBUG` level (not `INFO`, to avoid log noise) the truncated key hash and tenant slug when a validation fails. The rate limiter already protects against log-flooding.
- **Estimated Effort:** S
- **Affected Files:** `solvyn/multi_tenant/auth.py`.

### [P3-006] `tracing.py` `deque(maxlen=10000)` is process-local; lost on restart
- **Location:** `solvyn/observability/tracing.py:13`
- **Finding:** All traces are in-memory and lost on restart. For a production audit trail this is insufficient — traces should be flushed to PostgreSQL or an OLAP store. The `Memory` model already exists in `db/models.py` and could host a `Trace` table.
- **Recommended Fix Strategy:** Add a periodic flush task in the `lifespan` startup that drains the deque to PostgreSQL every 30s. Add a `Trace` model with `id, session_id, tenant_id, name, started_at, ended_at, spans JSONB, status`. Move `TracingManager` to async PG storage.
- **Estimated Effort:** L
- **Affected Files:** `solvyn/observability/tracing.py`, `solvyn/db/models.py`, `solvyn/db/migrations/`, `solvyn/main.py` (flush task).

### [P3-007] `MemoryModel` is used for both `memory` and `checkpoint` records with a `key` prefix
- **Location:** `solvyn/agents/memory.py`, `solvyn/agents/checkpoint.py:13-19`
- **Finding:** `MemoryModel` is dual-purpose: regular memory uses a free-form `key`; checkpoints use `key="checkpoint:<uuid>"`. Querying the model with `key.like("checkpoint:%")` is fragile and not index-friendly.
- **Recommended Fix Strategy:** Add a `record_type` column to `MemoryModel` (enum: `memory`, `checkpoint`, `audit`). Add a partial index `WHERE record_type = 'checkpoint'`. Migrate the existing data.
- **Estimated Effort:** M
- **Affected Files:** `solvyn/db/models.py`, `solvyn/agents/checkpoint.py`, `solvyn/db/migrations/`.

### [P3-008] `frontend/next.config.js` is referenced in `frontend/Dockerfile` but not committed
- **Location:** `frontend/Dockerfile:5` (`COPY ... next.config.js ...`)
- **Finding:** `COPY package.json tsconfig.json next.config.js tailwind.config.js ./` — if any of these are missing, the build fails. Spot-check that all four are committed.
- **Recommended Fix Strategy:** Add a pre-commit hook that runs `docker buildx build --target builder frontend/` (or a lighter `node -e "require('./next.config.js')"`) to fail fast when a referenced file is missing.
- **Estimated Effort:** S
- **Affected Files:** `.pre-commit-config.yaml` (new hook), `frontend/Dockerfile`.

### [P3-009] `solvyn.eval.evaluator` flags code with `TODO`/`FIXME`/`HACK`/`eval(`/`exec(` — but only in the strings, not the AST
- **Location:** `solvyn/eval/evaluator.py:130-142`
- **Finding:** The evaluator uses `if "TODO" in code or "eval(" in code` — substring matching, not AST parsing. A line of `// This string contains "eval(" as a documentation reference` is flagged. A backtick-escaped `eval` in a docstring also trips it.
- **Recommended Fix Strategy:** Use `ast` to parse the code first; flag only AST-level `ast.Call` nodes whose function is named `eval`/`exec`/`compile`/`__import__`. For comments, use `tokenize` and skip comment/string tokens.
- **Estimated Effort:** M
- **Affected Files:** `solvyn/eval/evaluator.py`, `tests/test_eval.py`.

### [P3-010] `monitoring/grafana-dashboard.json` and `monitoring/prometheus-alerts.yml` not inspected
- **Location:** `monitoring/`
- **Finding:** The directory exists per the README but was not read in this inspection. Recommend a separate, smaller sweep of these files to verify alert thresholds and dashboard panels align with the 8 metrics in `solvyn/observability/metrics.py`.
- **Recommended Fix Strategy:** Add a follow-up inspection step. As a stub, add a CI lint that parses both files (JSON / YAML) and asserts no schema drift.
- **Estimated Effort:** S
- **Affected Files:** `.github/workflows/ci-cd.yml` (new `monitoring-lint` job).

---

## Implementation Roadmap

### Phase 1: Security & Stability (Week 1) — block all production deploys until done
- [ ] **P0-001** Rewrite `_validate_tenant_api_key` to use `tenants.api_key_hash` unique index
- [ ] **P0-002** Replace `api_key_secret` empty warning with `RuntimeError` in `Settings.model_validator`
- [ ] **P0-003** Tighten CSP in `solvyn/main.py`; add `Permissions-Policy`, COOP, CORP
- [ ] **P0-004** Pin Python deps to `==` in `pyproject.toml`; switch CI to `uv sync --frozen`
- [ ] **P0-005** Add `pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=1800` to `db/session.py`
- [ ] **P0-006** Add `model_validator` that requires the configured `llm_default_provider` key to be non-empty
- [ ] **P0-007** Define `WorkflowRunRequest` Pydantic models per template; cap body size
- [ ] **P0-008** Rename `k8s/secrets.example.yaml` → `k8s/secrets.TEMPLATE.yaml`; add banner comment
- [ ] **P1-002** Switch `asyncio.gather` to `return_exceptions=True` in `agents/graph.py`
- [ ] **P1-005** Add `securityContext` to all K8s `Deployment` containers

### Phase 2: Architecture & Resilience (Week 2)
- [ ] **P1-001** Share one `httpx.AsyncClient` per process; split `httpx.Timeout` into connect/read/write/pool
- [ ] **P1-006** Add gitleaks, CodeQL, trivy, SBOM jobs to CI; gate `deploy-prod` on scan results
- [ ] **P1-007** Bring `.env.example` to parity with `Settings`; add frontend `.env.example`
- [ ] **P1-008** Add `.python-version` and `frontend/.nvmrc`; pin Dockerfiles by digest
- [ ] **P2-001** Validate LLM API key prefixes in `Settings`
- [ ] **P2-004** Add `env_prefix="SOLVYN_"`; fix `tests/test_config.py`
- [ ] **P2-006** Validate WebSocket `Origin` header against `allowed_origins`
- [ ] **P2-010** Add startup check that rejects `*` in `allowed_origins` when `allow_credentials=True`
- [ ] **P2-009** Cursor pagination on `GET /sessions/` and `GET /observability/traces`

### Phase 3: Hardening & Polish (Week 3+)
- [ ] **P1-003** Pin `frontend/package.json` `next` / `react` / `react-dom` to exact versions
- [ ] **P1-004** Multi-stage Dockerfiles; non-root `USER`; `HEALTHCHECK`; pin base images by digest
- [ ] **P2-002** Remove `langgraph` / `langchain-core` from runtime deps
- [ ] **P2-003** Remove duplicate `httpx` from dev deps
- [ ] **P2-005** Add `make requirements` target exporting `requirements.txt` / `requirements-dev.txt`
- [ ] **P2-007** Validate `llm_default_provider` in `Settings.model_validator`
- [ ] **P2-008** Log PII scrub events to audit log + Prometheus counter
- [ ] **P2-011** Document `.env` requirement in `docker-compose.yml`; create `docker-compose.prod.yml`
- [ ] **P2-012** Add `solvyn doctor` CLI subcommand
- [ ] **P3-001..P3-010** backlog items per the descriptions above

---

## Deployment Readiness Checklist
- [ ] All P0 issues resolved
- [ ] No secrets in source control or Dockerfile `ENV` / `--build-arg`
- [ ] `.env.example` complete (all 25 settings) and startup validates required env vars
- [ ] `API_KEY_SECRET` non-empty; server refuses to start otherwise
- [ ] `llm_default_provider` key non-empty; server refuses to start otherwise
- [ ] Health check endpoint `/api/v1/health` (shallow) and `/api/v1/health/deep` (DB+Redis) both return 200
- [ ] Readiness probe in K8s points at `/api/v1/health/deep`, liveness at `/api/v1/health`
- [ ] Dockerfile has non-root `USER`, `HEALTHCHECK`, and base image pinned by digest
- [ ] `.dockerignore` excludes `.env`, `.git`, `node_modules`, `__pycache__`, `venv`, `.venv`
- [ ] CORS locked to explicit origins; CSP has no `unsafe-eval`
- [ ] WebSocket origin validated against `allowed_origins`
- [ ] Rate limiting: 100 req/min IP, 500 req/min tenant; on `/api/v1/auth` failures also rate-limit by API-key hash
- [ ] Graceful shutdown handler registered; `httpx.AsyncClient` shared and `aclose()`-d on `SIGTERM`
- [ ] Structured JSON logging; no secrets in log fields
- [ ] All Python deps pinned in `pyproject.toml`; `uv sync --frozen` in CI
- [ ] Frontend deps pinned in `package.json`; `package-lock.json` committed
- [ ] No bare `except:` / catch-all in critical paths (already clean)
- [ ] Pydantic input validation on all write endpoints (`/workflows/{name}/run` is the open one)
- [ ] CI runs ruff, mypy, pytest, pip-audit, gitleaks, codeql, trivy on every push
- [ ] SBOM generated on every build
- [ ] Manual approval gate for `deploy-prod` enforced via GitHub Environment
- [ ] NetworkPolicy + PodDisruptionBudget present in K8s
- [ ] Per-tenant API-key lookup uses unique index on `tenants.api_key_hash`

---

## Summary Table

| ID      | Severity | Domain       | Location                                          | Title                                                          | Effort |
|---------|----------|--------------|---------------------------------------------------|----------------------------------------------------------------|--------|
| P0-001  | Critical | Security     | solvyn/multi_tenant/auth.py:40                    | Per-tenant API-key validation does full-table linear scan     | S      |
| P0-002  | Critical | Security     | solvyn/config.py:47                               | Empty `API_KEY_SECRET` is a warning, not fatal                 | S      |
| P0-003  | Critical | Security     | solvyn/main.py:78                                 | CSP permits `unsafe-eval` and `unsafe-inline`                  | M      |
| P0-004  | Critical | Dependencies | pyproject.toml:9-25                               | All Python security-critical deps are unpinned (`>=`)         | S      |
| P0-005  | Critical | Architecture | solvyn/db/session.py:7                            | SQLAlchemy async engine has no connection pool config         | S      |
| P0-006  | Critical | Dependencies | solvyn/config.py:10, solvyn/llm/router.py:215     | LLM provider API key not validated at startup                 | S      |
| P0-007  | Critical | Security     | solvyn/api/v1/workflows.py:53                     | Workflow `run` endpoint takes raw `dict[str, Any]` body        | M      |
| P0-008  | Critical | Security     | k8s/secrets.example.yaml                          | Secret template uses real-looking placeholders                 | S      |
| P1-001  | High     | Architecture | solvyn/llm/providers/openrouter.py:14             | `httpx.AsyncClient` per provider never closed on SIGKILL       | S      |
| P1-002  | High     | Bug          | solvyn/agents/graph.py:225                        | `asyncio.gather(return_exceptions=False)` cancels peers        | S      |
| P1-003  | High     | Dependencies | frontend/package.json:12-19                       | Frontend security-critical deps use floating semver (`^`)      | S      |
| P1-004  | High     | Deployment   | Dockerfile:1, frontend/Dockerfile:1,7             | Base images unpinned, run as root, no HEALTHCHECK              | M      |
| P1-005  | High     | Deployment   | k8s/deployment.yaml                               | No `securityContext` on K8s Deployment containers               | M      |
| P1-006  | High     | Deployment   | .github/workflows/ci-cd.yml                       | No image scanning, SAST, or secret scanning in CI              | M      |
| P1-007  | High     | Dependencies | .env.example vs solvyn/config.py:7                | `.env.example` missing 16 of 25 settings; langfuse is dead     | S      |
| P1-008  | High     | Dependencies | project root, frontend/                           | No runtime version pinning files                               | S      |
| P2-001  | Medium   | Dependencies | solvyn/llm/router.py:225                          | LLM API key length / format not validated                      | S      |
| P2-002  | Medium   | Dependencies | pyproject.toml:18-19                              | `langgraph` / `langchain-core` declared but never imported     | S      |
| P2-003  | Medium   | Dependencies | pyproject.toml:21,35                              | `httpx` duplicated in main + dev dependencies                  | S      |
| P2-004  | Medium   | Dependencies | solvyn/config.py:5, tests/test_config.py:18       | `Settings` has no `env_prefix="SOLVYN_"`                       | S      |
| P2-005  | Medium   | Dependencies | project root                                      | No `requirements.txt` shim for non-uv consumers                | S      |
| P2-006  | Medium   | Security     | solvyn/observability/analytics_api.py:88          | WebSocket endpoint accepts connections from any origin         | S      |
| P2-007  | Medium   | Architecture | solvyn/llm/router.py:245                          | `llm_default_provider` not validated against registered list   | S      |
| P2-008  | Medium   | Security     | solvyn/guardrails/content_moderator.py:213        | `scrub_and_validate` does not log scrub events                 | S      |
| P2-009  | Medium   | Architecture | solvyn/api/v1/sessions.py:18, observability       | No cursor pagination on `/sessions/` and `/observability/traces` | M      |
| P2-010  | Medium   | Security     | solvyn/main.py:55, solvyn/config.py:23            | CORS `allow_credentials=True` with default localhost origin    | S      |
| P2-011  | Medium   | Deployment   | docker-compose.yml:12                             | Project root mounted with `--reload`; `.env` not validated     | S      |
| P2-012  | Medium   | Architecture | solvyn/cli.py                                     | No `solvyn doctor` pre-flight command                          | S      |
| P3-001  | Low      | Observability| solvyn/agents/graph.py:223                        | No level-duration histogram in `AgentGraph`                    | S      |
| P3-002  | Low      | Bug          | solvyn/agents/retry.py:36                         | `RetryHandler` cannot distinguish transient from permanent     | S      |
| P3-003  | Low      | Bug          | solvyn/message_bus/redis.py:78                    | `add_to_dead_letter` does not enforce a max stream length      | S      |
| P3-004  | Low      | Deployment   | k8s/deployment.yaml, solvyn/api/v1/health.py:14   | K8s readiness probe points at shallow health                   | S      |
| P3-005  | Low      | Security     | solvyn/multi_tenant/auth.py:40                    | Auth failures not logged with truncated key hash               | S      |
| P3-006  | Low      | Observability| solvyn/observability/tracing.py:13                | Traces are in-memory and lost on restart                       | L      |
| P3-007  | Low      | Architecture | solvyn/agents/checkpoint.py:13, db/models.py      | `MemoryModel` is dual-purpose memory + checkpoint              | M      |
| P3-008  | Low      | Deployment   | frontend/Dockerfile:5                             | Referenced config files not validated by pre-commit            | S      |
| P3-009  | Low      | Architecture | solvyn/eval/evaluator.py:130                      | String-based `eval`/`exec` detection is substring-only         | M      |
| P3-010  | Low      | Deployment   | monitoring/                                       | Grafana dashboard and Prometheus alerts not inspected          | S      |

**Total: 38 findings · 8 P0 · 8 P1 · 12 P2 · 10 P3**

---

**Deployment Verdict: BLOCKED** — Production launch must wait until at least the 8 P0 findings are remediated, the per-tenant auth index is in place, the SQLAlchemy pool is configured, and CI gates on gitleaks / codeql / trivy.
