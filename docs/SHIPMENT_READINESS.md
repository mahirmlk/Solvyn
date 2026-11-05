# Shipment readiness assessment

This status compares the repository with `UPDATE.md`, including its primary engineering workflow and Phase 10 self-hosting requirements. It is a code review snapshot, not a claim that every product requirement is complete.

## Phase 10: self-hosted deployment

| Requirement | Status | Evidence / remaining work |
|---|---|---|
| Docker Compose deployment | Implemented for a self-hosted baseline | `docker-compose.prod.yml` separates API, UI, database, Redis, migrations, scheduler worker, and sandbox supervisor. Requires an operator-managed TLS reverse proxy and host security controls. |
| Database migrations | Implemented in deployment path | The one-shot `migrate` service runs `alembic upgrade head`; API and worker wait for successful completion. |
| Persistent volumes | Implemented | PostgreSQL and Redis use named volumes. Off-host backup and recovery remain operator responsibilities. |
| Health checks | Implemented | API liveness/deep-readiness, frontend, PostgreSQL, and Redis health checks. The worker relies on restart policy and logs; it has no dedicated heartbeat endpoint. |
| Backup and restore | Scripts and procedure implemented | Backup omits credentials from logs, supports asyncpg URLs, and uploads to S3 only when configured. Restore requires exact target confirmation and does not drop the database. No automated schedule, retention service, or restore drill is supplied. |
| Secure configuration | Baseline implemented | Production example requires separate auth, encryption, webhook, and database secrets; API fails to start when core auth secrets are absent. Secret storage/rotation remains deployment-specific. |
| Observability | Partial | Structured logs and Prometheus metrics exist. Compose does not include a metrics collector, durable trace backend, alert routing, or worker heartbeat. |
| Worker management | Partial | Scheduler is a separate, restartable singleton service. Durable scheduled workflow state and leases exist; queue dashboard, concurrency controls, dead-letter operations, and a worker health endpoint remain follow-up work. |
| Resource controls | Implemented at container level | Compose configures CPU and memory limits. Production verification dispatches from the API to a private sandbox supervisor, which creates networkless bounded job containers. Delegated task workspaces can be populated from caller-selected public GitHub files pinned to one commit; full repository clones remain unsupported. |
| Teammate workspace tools | Partial | Active handoffs can request list/read/write with `READ_ONLY`/`MODIFY_FILES` and fixed verification profiles with `EXECUTE_COMMANDS`, with task/workspace/lease checks and durable metadata. Separate human approval for verification and arbitrary teammate shell execution remain unavailable. |
| Sandbox control boundary | Implemented for the Compose baseline | Only the trusted supervisor receives `/var/run/docker.sock`; untrusted job containers, API, worker, and frontend do not. The supervisor has Docker host control authority. Current Compose is co-located and needs a shared volume; a separate physical runtime host is not supported yet. |
| Production documentation | Implemented for Compose baseline | `DEPLOYMENT.md` covers setup, migrations, proxy placement, backups, restore, and limits. It is not a substitute for a deployment-specific incident and recovery runbook. |

## Product shipment blockers

The central `UPDATE.md` workflow is not complete. The sandbox supports seeding a task workspace with caller-selected public GitHub files pinned to one commit SHA, scoped through the delegated task's project handoff, with source provenance persisted. This is a selected-file snapshot, not a clone or Git worktree. During an active delegated handoff, teammates can list/read files under `READ_ONLY`, write files under `MODIFY_FILES`, and run fixed test/build/lint/typecheck/security profiles under `EXECUTE_COMMANDS`; each dispatch checks the active lease, exact workspace/task scope, and current permission. Tool invocation metadata and verification outputs are persisted.

For repository-backed workspaces, an owner/admin can request a diff preview and then submit the returned digest to create a Solvyn branch from the exact pinned source commit, commit up to 50 UTF-8 text files / 500 KB, and open a draft PR. The preview binds the current GitHub base branch as well as the workspace; a changed preview invalidates the delivery request. The endpoint will not merge. File deletions, changed executable files, and edits to unselected existing paths are rejected. If the PR request times out after the commit, the endpoint reports an unknown PR outcome and recovery guidance rather than claiming no PR was created. Existing repository APIs can synchronize and inspect pull requests, but task delivery/review state is not yet unified into a task timeline or approval inbox.

Shipment blockers remain: full repository/worktree support; private repository workspace creation; a frontend flow for workspace review and delivery; human approval for verification runs; deletion support; PostgreSQL row-level security; unified teammate execution and PR history; proactive maintenance that runs checks rather than only creating triage events; worker heartbeat and recovery/operations controls; and deployment validation, backup scheduling, retention, and a restore drill. These gaps mean Solvyn cannot yet provide the complete engineering-teammate workflow in `UPDATE.md` as a production-ready product.

Other significant gaps include PostgreSQL row-level security, a unified teammate execution history, proactive maintenance that performs checks (current schedules create triage inbox events), and comprehensive independent verification. These require separate product workstreams and migrations; the current teammate verification slice does not claim to close them.

## Release decision

The Phase 10 Compose path is implemented, but still needs deployment validation in a controlled environment. The overall product is **not ready to ship as the AI engineering teammate described in `UPDATE.md`** until the repository-to-agent-to-sandbox workflow is completed, tenant isolation and operational gaps are addressed, and a deployment validation is recorded. The exact release scope should not be represented as a complete implementation of the product brief.
