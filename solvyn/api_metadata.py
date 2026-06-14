"""Shared OpenAPI metadata used across the API routers.

The constants in this module are imported by both ``solvyn.main`` (for the
top-level schema and the FastAPI app metadata) and the individual routers
under ``solvyn.api.v1`` and ``solvyn.observability``. Keeping them in a
dedicated module avoids the circular import that would otherwise result
from the routers importing from ``solvyn.main``.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Tag catalogue
# ---------------------------------------------------------------------------

TAGS_METADATA: list[dict[str, str]] = [
    {
        "name": "health",
        "description": "Liveness and readiness probes used by Kubernetes and load balancers. "
        "Endpoints in this group are public and require no authentication.",
    },
    {
        "name": "sessions",
        "description": "Manage chat / agent sessions scoped to the calling tenant. "
        "A session is the durable container for messages, agent traces, and "
        "workflow executions.",
    },
    {
        "name": "workflows",
        "description": "List, inspect, and execute declarative workflow templates. "
        "Workflows are composed of ordered agent steps with explicit "
        "dependency and parallelism declarations.",
    },
    {
        "name": "observability",
        "description": "Read-only access to metrics, traces, audit logs, and "
        "per-session / per-tenant cost analytics. Useful for dashboards, "
        "post-mortems, and chargeback reports.",
    },
    {
        "name": "admin",
        "description": "Administrative operations. Reserved for tenant operators "
        "with elevated roles; subject to rate limits and audit logging.",
    },
]


# ---------------------------------------------------------------------------
# Common response examples
# ---------------------------------------------------------------------------

# Standard JSON examples for the error responses we surface across the API.
# Centralised so individual routers can reuse them without duplication.
COMMON_RESPONSES: dict[int | str, dict] = {
    401: {
        "description": "Authentication failed — missing or invalid `X-API-Key`.",
        "content": {
            "application/json": {
                "example": {"error": "Invalid X-API-Key"},
            }
        },
    },
    403: {
        "description": "Caller is authenticated but lacks the required role or tenant scope.",
        "content": {
            "application/json": {
                "example": {"error": "Access denied: tenant mismatch"},
            }
        },
    },
    422: {
        "description": "Request body or query parameters failed Pydantic validation.",
        "content": {
            "application/json": {
                "example": {
                    "detail": [
                        {
                            "loc": ["body", "specification"],
                            "msg": "field required",
                            "type": "value_error.missing",
                        }
                    ]
                }
            }
        },
    },
    429: {
        "description": "Rate limit exceeded. Back off and retry after the window resets.",
        "content": {
            "application/json": {
                "example": {"error": "Rate limit exceeded", "retry_after": 42, "scope": "tenant"},
            }
        },
    },
}


# ---------------------------------------------------------------------------
# Top-level API description
# ---------------------------------------------------------------------------

API_DESCRIPTION = """
# Solvyn API

**Solvyn** is the AI Operating Center for engineering teams — a multi-tenant
control plane that orchestrates specialised LLM agents, executes declarative
workflows, and ships an opinionated observability layer (metrics, traces,
audit logs, real-time cost analytics).

## Authentication

All non-public endpoints require an `X-API-Key` header. The key is validated
in one of two modes:

* **Per-tenant mode** — the key is matched against the `api_key_hash` stored
  on the active `Tenant` row that matches the `X-Tenant-Slug` header.
* **Shared-secret mode** — the key is checked against the `API_KEY_SECRET`
  environment variable (used for single-tenant deployments and CI).

Click the **Authorize** button at the top of this page to set the
`X-API-Key` value once for the entire session.

## Versioning

The API lives under `/api/v1` and follows [semantic versioning](https://semver.org/).
Breaking changes will always be introduced via a new prefix (`/api/v2`).

## Multi-tenancy

Every authenticated request resolves a tenant context. Tenant boundaries are
enforced at the data layer — sessions, audit logs, and cost analytics are
scoped to the calling tenant.

## Error model

Errors are returned as JSON with the shape:

```json
{ "error": "human-readable message" }
```

The HTTP status code follows standard REST conventions:

| Code | Meaning |
| ---- | ------- |
| 400  | Malformed request or missing required header |
| 401  | Missing or invalid `X-API-Key` |
| 403  | Tenant mismatch / insufficient role |
| 404  | Resource not found |
| 422  | Validation error (Pydantic) |
| 429  | Rate limit exceeded |
| 500  | Unexpected server error |

## Observability

Prometheus metrics are served at `/metrics`. Distributed traces are exported
via OpenTelemetry when `OTEL_ENABLED=true`. The audit log and trace store
can be queried through the `/observability` endpoints.
"""
