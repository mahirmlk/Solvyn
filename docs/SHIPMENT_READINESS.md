# Shipment readiness assessment

This status compares the repository with `UPDATE.md`, including its primary engineering workflow and Phase 10 self-hosting requirements. It is a code review snapshot, not a claim that every product requirement is complete.

## Phase 10: self-hosted deployment

| Requirement | Status | Evidence / remaining work |
|---|---|---|
| Docker Compose deployment | Implemented for a self-hosted baseline | `docker-compose.prod.yml` separates API, UI, database, Redis, migrations, and one scheduler worker. Requires an operator-managed TLS reverse proxy and host security controls. |
| Database migrations | Implemented in deployment path | The one-shot `migrate` service runs `alembic upgrade head`; API and worker wait for successful completion. |
| Persistent volumes | Implemented | PostgreSQL and Redis use named volumes. Off-host backup and recovery remain operator responsibilities. |
| Health checks | Implemented | API liveness/deep-readiness, frontend, PostgreSQL, and Redis health checks. The worker relies on restart policy and logs; it has no dedicated heartbeat endpoint. |
| Backup and restore | Scripts and procedure implemented | Backup omits credentials from logs, supports asyncpg URLs, and uploads to S3 only when configured. Restore requires exact target confirmation and does not drop the database. No automated schedule, retention service, or restore drill is supplied. |
| Secure configuration | Baseline implemented | Production example requires separate auth, encryption, webhook, and database secrets; API fails to start when core auth secrets are absent. Secret storage/rotation remains deployment-specific. |
| Observability | Partial | Structured logs and Prometheus metrics exist. Compose does not include a metrics collector, durable trace backend, alert routing, or worker heartbeat. |
| Worker management | Partial | Scheduler is a separate, restartable singleton service. Durable scheduled workflow state and leases exist; queue dashboard, concurrency controls, dead-letter operations, and a worker health endpoint remain follow-up work. |
| Resource controls | Implemented at container level | Compose configures CPU and memory limits. Sandbox resource enforcement exists in its internal runner, but the product workflow does not yet dispatch repository tasks into it. |
| Production documentation | Implemented for Compose baseline | `DEPLOYMENT.md` covers setup, migrations, proxy placement, backups, restore, and limits. It is not a substitute for a deployment-specific incident and recovery runbook. |

## Product shipment blockers

The central `UPDATE.md` workflow is not complete. Repository integration supports bounded public-repository inspection and selected GitHub operations, but the agent runtime does not receive repository files/context as task inputs. The verification workspace accepts caller-supplied files, and the internal Docker sandbox runner is not connected to a deployable sandbox service. Private repository reads are also not implemented. Therefore Solvyn cannot yet reliably inspect an existing repository, implement a requested change in an isolated worktree, run its checks, present an evidence-backed diff, and create a pull request end to end.

Other significant gaps include PostgreSQL row-level security, durable product execution history for every tool invocation, proactive maintenance that performs checks (current schedules create triage inbox events), and comprehensive independent verification. These require separate product workstreams and migrations; this shipment-hardening branch does not claim to close them.

## Release decision

The Phase 10 Compose path is ready for a reviewed PR and deployment validation in a controlled environment. The overall product is **not ready to ship as the AI engineering teammate described in `UPDATE.md`** until the repository-to-agent-to-sandbox workflow is implemented and verified, and the remaining tenant-isolation and operational gaps are addressed. The exact release scope should not be represented as a complete implementation of the product brief.
