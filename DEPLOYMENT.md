# Solvyn Deployment Guide

> Deploying Solvyn — from local development to production Kubernetes

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Local Development Setup](#local-development-setup)
3. [Docker Deployment](#docker-deployment)
4. [Kubernetes Deployment](#kubernetes-deployment)
5. [Environment Configuration](#environment-configuration)
6. [Database Migrations](#database-migrations)
7. [Health Checks & Monitoring](#health-checks--monitoring)
8. [CI/CD Pipeline](#cicd-pipeline)
9. [Staging vs Production](#staging-vs-production)
10. [Rolling Updates & Rollback](#rolling-updates--rollback)
11. [Horizontal Scaling](#horizontal-scaling)
12. [Persistent Storage](#persistent-storage)
13. [SSL/TLS Termination](#ssltls-termination)
14. [Backup & Restore](#backup--restore)

---

## Prerequisites

| Component | Version | Purpose |
|-----------|---------|---------|
| **Python** | 3.12+ | API runtime |
| **Node.js** | 20+ | Frontend build |
| **Docker** | 24+ | Container runtime |
| **Docker Compose** | 2.x | Local multi-service orchestration |
| **kubectl** | 1.29+ | Kubernetes cluster management |
| **PostgreSQL** | 16 (with pgvector) | Persistent storage |
| **Redis** | 7 | Message bus |
| **Git** | 2.x | Source control |

---

## Local Development Setup

### Quick Start

```bash
# 1. Clone the repository
git clone <repository-url>
cd solvyn

# 2. Copy and configure environment
cp .env.example .env
# Edit .env with your API keys

# 3. Install dependencies
make install

# 4. Start development server
make dev
```

### Manual Setup

```bash
# Python dependencies
pip install -e ".[dev]"

# Frontend dependencies
cd frontend && npm install

# Start API (hot reload enabled)
uvicorn solvyn.main:app --reload --host 0.0.0.0 --port 8000

# Start frontend (separate terminal)
make dev-frontend
```

### Makefile Targets

```makefile
dev           # Start API with hot reload
dev-frontend  # Start Next.js dev server
install       # Install all dependencies (Python + Node)
lint          # Ruff check + format check
format        # Ruff auto-fix + format
typecheck     # MyPy static type checking
test          # Run pytest
test-cov      # Run pytest with coverage report
pre-commit    # lint + typecheck + test (CI gate)
docker-build  # Build Docker images
docker-up     # Start all services with Docker Compose
docker-down   # Stop all services
docker-logs   # Tail service logs
deploy        # Apply all Kubernetes manifests
deploy-secrets# Apply K8s secrets (do not commit this file)
clean         # Remove build artifacts and caches
```

### Local Services

When running `make dev`, you need PostgreSQL and Redis running. Use Docker Compose for just the services:

```bash
# Start only PostgreSQL + Redis
docker compose up -d db redis

# Verify services are running
docker compose ps
```

---

## Docker Deployment

### Development with Docker Compose

The `docker-compose.yml` defines three services:

```yaml
services:
  api:      # FastAPI app, port 8000, depends on db (healthy) + redis
  db:       # pgvector/pgvector:pg16, port 5432, healthcheck: pg_isready
  redis:    # redis:7-alpine, port 6379
```

Volumes are used for persistent data:
- `pgdata:/var/lib/postgresql/data` — PostgreSQL data
- `redisdata:/data` — Redis RDB/AOF

```bash
# Start everything
docker compose up -d

# View logs
docker compose logs -f

# Rebuild after code changes
docker compose build
docker compose up -d

# Stop and remove
docker compose down

# Remove volumes (fresh start)
docker compose down -v
```

### Production Dockerfile

**Source**: `Dockerfile` (API)

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"
COPY . .
CMD ["uvicorn", "solvyn.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Frontend Dockerfile**: `frontend/Dockerfile` — multi-stage build with `next build` → optimized production image.

### Building for Production

```bash
# Build API image
docker build -t solvyn/api:latest .

# Build frontend image
docker build -t solvyn/frontend:latest ./frontend
```

---

## Kubernetes Deployment

### Namespace

```bash
kubectl apply -f k8s/namespace.yaml
```

Creates the `solvyn` namespace. All subsequent resources are deployed into it.

### Step-by-Step Deployment

```bash
# 1. Namespace
kubectl apply -f k8s/namespace.yaml

# 2. ConfigMap (non-secret environment)
kubectl apply -f k8s/configmap.yaml

# 3. Secrets
# FIRST: Copy secrets.example.yaml → secrets.yaml and fill real values
# DO NOT commit secrets.yaml
cp k8s/secrets.example.yaml k8s/secrets.yaml
# Edit k8s/secrets.yaml with real values
kubectl apply -f k8s/secrets.yaml

# 4. Persistent Volume Claim
kubectl apply -f k8s/pvc.yaml

# 5. Deployments (API, PostgreSQL, Redis)
kubectl apply -f k8s/deployment.yaml

# 6. Services
kubectl apply -f k8s/service.yaml

# 7. Horizontal Pod Autoscaler
kubectl apply -f k8s/hpa.yaml

# 8. Ingress (TLS)
kubectl apply -f k8s/ingress.yaml
```

Or use the Makefile shortcut:

```bash
make deploy
```

### Kubernetes Resources Summary

| Resource | Name | Details |
|----------|------|---------|
| Namespace | `solvyn` | Isolates all Solvyn resources |
| ConfigMap | `solvyn-config` | DATABASE_URL, REDIS_URL, LLM_DEFAULT_PROVIDER, LOG_LEVEL, HOST, PORT |
| Secret | `solvyn-secrets` | POSTGRES_PASSWORD, OPENROUTER_API_KEY, GROQ_API_KEY, DEEPSEEK_API_KEY, API_KEY_SECRET |
| PVC | `solvyn-db-pvc` | 50Gi, ReadWriteOnce, standard storage class |
| Deployment | `solvyn-api` | 2 replicas, 500m-2000m CPU, 512Mi-2Gi memory |
| Deployment | `solvyn-db` | 1 replica, pgvector/pgvector:pg16, 500m-2000m CPU, 1Gi-4Gi memory |
| Deployment | `solvyn-redis` | 1 replica, redis:7-alpine, 200m-500m CPU, 256Mi-512Mi memory |
| Service | `solvyn-api` | ClusterIP, port 80 → 8000 |
| Service | `solvyn-db` | ClusterIP, port 5432 → 5432 |
| Service | `solvyn-redis` | ClusterIP, port 6379 → 6379 |
| HPA | `solvyn-api-hpa` | 2 min / 10 max replicas, CPU 70%, Memory 80% |
| Ingress | `solvyn-ingress` | nginx, TLS via cert-manager, routes /api and / |

### Health Probes

```yaml
livenessProbe:
  httpGet:
    path: /api/v1/health
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 15

readinessProbe:
  httpGet:
    path: /api/v1/health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
```

### Prometheus Annotations

The API deployment includes Prometheus scrape annotations:

```yaml
annotations:
  prometheus.io/scrape: "true"
  prometheus.io/path: "/metrics"
  prometheus.io/port: "8000"
```

---

## Environment Configuration

### All Environment Variables

**Source**: `solvyn/config.py` and `.env.example`

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn` | Yes | PostgreSQL connection string with asyncpg driver |
| `REDIS_URL` | `redis://localhost:6379/0` | Yes | Redis connection string |
| `OPENROUTER_API_KEY` | `""` | For OpenRouter | OpenRouter API key (format: `sk-or-v1-...`) |
| `GROQ_API_KEY` | `""` | For Groq | Groq API key (format: `gsk_...`) |
| `DEEPSEEK_API_KEY` | `""` | For DeepSeek | DeepSeek API key (format: `sk-...`) |
| `LLM_DEFAULT_PROVIDER` | `openrouter` | No | Default LLM provider (`openrouter`, `groq`, `deepseek`) |
| `LLM_DEFAULT_MODEL` | `openai/gpt-4o` | No | Default model for the chosen provider |
| `DEFAULT_TENANT_ID` | `default` | No | Fallback tenant ID when not specified |
| `HOST` | `0.0.0.0` | No | Server bind address |
| `PORT` | `8000` | No | Server port |
| `LOG_LEVEL` | `INFO` | No | Logging level (DEBUG, INFO, WARNING, ERROR) |
| `JSON_LOGGING` | `true` | No | Enable structured JSON log output |
| `API_KEY_SECRET` | `change-me-to-a-random-secret` | Yes | Master API key for auth |
| `ALLOWED_ORIGINS` | `["http://localhost:3000"]` | No | CORS allowed origins (JSON list) |

### Kubernetes Configuration

Non-secret values go in the ConfigMap (`k8s/configmap.yaml`):

```yaml
data:
  DATABASE_URL: "postgresql+asyncpg://solvyn:$(POSTGRES_PASSWORD)@solvyn-db:5432/solvyn"
  REDIS_URL: "redis://solvyn-redis:6379/0"
  LLM_DEFAULT_PROVIDER: "openrouter"
  LLM_DEFAULT_MODEL: "openai/gpt-4o"
  LOG_LEVEL: "INFO"
  HOST: "0.0.0.0"
  PORT: "8000"
```

Sensitive values go in the Secret (`k8s/secrets.yaml`):

```yaml
stringData:
  POSTGRES_PASSWORD: "<strong-password>"
  OPENROUTER_API_KEY: "sk-or-v1-..."
  GROQ_API_KEY: "gsk_..."
  DEEPSEEK_API_KEY: "sk-..."
  API_KEY_SECRET: "<random-secret>"
```

---

## Database Migrations

### Running Migrations

Solvyn uses Alembic with asyncpg driver for PostgreSQL migrations:

```bash
# Ensure DATABASE_URL is set
export DATABASE_URL=postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn

# Apply all pending migrations
alembic upgrade head

# Create a new migration
alembic revision --autogenerate -m "description"

# Rollback one migration
alembic downgrade -1

# View migration history
alembic history

# Show current revision
alembic current
```

### Migration Configuration

**Source**: `alembic.ini`

```ini
[alembic]
script_location = solvyn/db/migrations
sqlalchemy.url = postgresql+asyncpg://solvyn:solvyn@localhost:5432/solvyn
```

> **⚠️ HUMAN INPUT NEEDED**: No migration is run automatically at startup. For production, add a pre-start script or init container that runs `alembic upgrade head`.

---

## Health Checks & Monitoring

### Health Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/v1/health` | GET | Basic liveness check (returns `{"status": "ok"}`) |
| `/api/v1/health/deep` | GET | Deep check: verifies DB connection (`SELECT 1`) + Redis ping |
| `/metrics` | GET | Prometheus metrics endpoint (separate mount, bypasses auth) |

### Deep Health Check Response

```json
{
    "status": "ok",
    "checks": {
        "database": { "status": "ok" },
        "redis": { "status": "ok" }
    }
}
```

Returns `"degraded"` if any check fails. DB failure does not crash the server — it returns `{"status": "error", "detail": "..."}`.

### Grafana Dashboard

A pre-built dashboard is available at `monitoring/grafana-dashboard.json`:

```bash
# Import into Grafana
# Dashboards → Import → Upload JSON file
```

The dashboard includes panels for: HTTP requests, error rate, LLM calls, agent executions, token usage, active agents, workflow runs, and memory operations.

### Prometheus Alert Configuration

Copy `monitoring/prometheus-alerts.yml` to your Prometheus server's rules directory:

```yaml
# prometheus.yml
rule_files:
  - "/etc/prometheus/solvyn-alerts.yml"
```

Reload Prometheus:
```bash
curl -X POST http://prometheus:9090/-/reload
```

---

## CI/CD Pipeline

### GitHub Actions Workflow

**Source**: `.github/workflows/ci-cd.yml`

### Pipeline Stages

```
Push/PR to main
    │
    ├──► 1. lint (parallel)
    │       ├── ruff check solvyn/
    │       └── mypy solvyn/ --ignore-missing-imports
    │
    ├──► 2. test (parallel with lint, needs lint to pass)
    │       ├── PostgreSQL service container (pgvector/pgvector:pg16)
    │       ├── Redis service container (redis:7-alpine)
    │       └── pytest tests/ -v --cov=solvyn
    │
    ├──► 3. build (main branch only)
    │       ├── Docker build + push API image → ghcr.io
    │       └── Docker build + push Frontend image → ghcr.io
    │
    ├──► 4. deploy-staging (main branch only)
    │       ├── Environment: staging
    │       ├── kubectl set image (API + Frontend)
    │       └── kubectl rollout status (5min timeout)
    │
    └──► 5. deploy-prod (manual approval gate)
            ├── Environment: production
            ├── kubectl set image (API + Frontend, prod namespace)
            └── kubectl rollout status (5min timeout)
```

### Image Tagging

Images are tagged with both `latest` and the Git commit SHA:

```
ghcr.io/<org>/<repo>:latest
ghcr.io/<org>/<repo>:<sha>
```

### Required GitHub Secrets

| Secret | Used In |
|--------|---------|
| `GITHUB_TOKEN` | Docker login to ghcr.io |
| `KUBECONFIG` (or cloud auth) | kubectl deploy steps |

---

## Staging vs Production

| Aspect | Staging | Production |
|--------|---------|------------|
| **Trigger** | Automatic on main push | Manual approval gate |
| **Environment** | `staging` | `production` |
| **Namespace** | `solvyn` | `solvyn-prod` |
| **Deployment** | `kubectl set image` + `rollout status` | `kubectl set image` + `rollout status` |
| **Approval** | None (auto-deploy) | Required before execution |

> **⚠️ HUMAN INPUT NEEDED**: The CI/CD file assumes separate staging and production Kubernetes namespaces (`solvyn` for staging, `solvyn-prod` for production). Ensure both namespaces exist and have the correct RBAC configured.

---

## Rolling Updates & Rollback

### Rolling Update (Default)

Kubernetes Deployments use `RollingUpdate` strategy (default). When a new image is pushed:

```bash
kubectl set image deployment/solvyn-api api=ghcr.io/org/solvyn:abc123 -n solvyn
kubectl rollout status deployment/solvyn-api -n solvyn --timeout=5m
```

Kubernetes will:
1. Start a new pod with the new image
2. Wait for readiness probe to pass
3. Terminate one old pod
4. Repeat until all pods are updated

### Rollback

If a deployment fails or the new version is problematic:

```bash
# View rollout history
kubectl rollout history deployment/solvyn-api -n solvyn

# Rollback to previous version
kubectl rollout undo deployment/solvyn-api -n solvyn

# Rollback to specific revision
kubectl rollout undo deployment/solvyn-api -n solvyn --to-revision=2
```

### CI/CD Rollback

The CI/CD pipeline checks `kubectl rollout status` with a 5-minute timeout. If the rollout does not complete within 5 minutes, the step fails, and the pipeline stops. You can then manually roll back.

---

## Horizontal Scaling

### HPA Configuration

**Source**: `k8s/hpa.yaml`

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: solvyn-api-hpa
spec:
  scaleTargetRef:
    name: solvyn-api
  minReplicas: 2
  maxReplicas: 10
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          averageUtilization: 70
    - type: Resource
      resource:
        name: memory
        target:
          averageUtilization: 80
```

### Scaling Behavior

- **Scale up**: When average CPU > 70% or memory > 80% across all pods
- **Scale down**: When resource usage drops below thresholds
- **Range**: 2 (minimum) to 10 (maximum) replicas

### Verify HPA

```bash
kubectl get hpa -n solvyn
# NAME              REFERENCE              TARGETS   MINPODS   MAXPODS   REPLICAS
# solvyn-api-hpa   Deployment/solvyn-api   45%/70%   2         10        3

kubectl describe hpa solvyn-api-hpa -n solvyn
```

---

## Persistent Storage

### PVC Configuration

**Source**: `k8s/pvc.yaml`

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: solvyn-db-pvc
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 50Gi
  storageClassName: standard
```

### Usage in Deployment

The PVC is mounted to the PostgreSQL container:

```yaml
volumes:
  - name: pgdata
    persistentVolumeClaim:
      claimName: solvyn-db-pvc

volumeMounts:
  - name: pgdata
    mountPath: /var/lib/postgresql/data
```

### Redis Data Persistence

Redis data is stored in a Docker volume for local development (`docker-compose.yml`). For Kubernetes, Redis is deployed without a PVC (ephemeral). For production Redis persistence, add a PVC and configure Redis AOF/RDB:

```yaml
# Add to solvyn-redis deployment
volumeMounts:
  - name: redisdata
    mountPath: /data
volumes:
  - name: redisdata
    persistentVolumeClaim:
      claimName: solvyn-redis-pvc
```

---

## SSL/TLS Termination

### Ingress Configuration

**Source**: `k8s/ingress.yaml`

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  annotations:
    kubernetes.io/ingress.class: "nginx"
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
spec:
  tls:
    - hosts:
        - solvyn.example.com
      secretName: solvyn-tls
  rules:
    - host: solvyn.example.com
      http:
        paths:
          - path: /api
            backend:
              service: solvyn-api (port 80)
          - path: /
            backend:
              service: solvyn-frontend (port 3000)
```

### Prerequisites for TLS

1. **cert-manager** installed in the cluster:
   ```bash
   kubectl apply -f https://github.com/cert-manager/cert-manager/releases/latest/download/cert-manager.yaml
   ```

2. **ClusterIssuer** configured for Let's Encrypt:
   ```yaml
   apiVersion: cert-manager.io/v1
   kind: ClusterIssuer
   metadata:
     name: letsencrypt-prod
   spec:
     acme:
       server: https://acme-v02.api.letsencrypt.org/directory
       email: admin@example.com
       privateKeySecretRef:
         name: letsencrypt-prod
       solvers:
         - http01:
             ingress:
               class: nginx
   ```

3. **DNS** pointing `solvyn.example.com` to the ingress controller's external IP

### Manual TLS

For environments without cert-manager, create a TLS secret manually:

```bash
kubectl create secret tls solvyn-tls \
  --cert=path/to/cert.pem \
  --key=path/to/key.pem \
  -n solvyn
```

---

## Backup & Restore

### PostgreSQL Backup

```bash
# Backup the database
kubectl exec -n solvyn deployment/solvyn-db -- \
  pg_dump -U solvyn -d solvyn -Fc > solvyn_backup_$(date +%Y%m%d).dump

# Or via port-forward
kubectl port-forward -n solvyn svc/solvyn-db 5432:5432 &
pg_dump -h localhost -U solvyn -d solvyn -Fc > solvyn_backup.dump
```

### Restore

```bash
# Restore from backup
kubectl exec -i -n solvyn deployment/solvyn-db -- \
  pg_restore -U solvyn -d solvyn --clean < solvyn_backup.dump
```

### Automated Backup (CronJob)

> **⚠️ HUMAN INPUT NEEDED**: No automated backup CronJob is included. Recommended approach:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: solvyn-db-backup
  namespace: solvyn
spec:
  schedule: "0 2 * * *"  # Daily at 2 AM
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: backup
              image: postgres:16
              command:
                - pg_dump
                - -h
                - solvyn-db
                - -U
                - solvyn
                - -d
                - solvyn
                - -Fc
                - -f
                - /backups/solvyn_$(date +%Y%m%d_%H%M%S).dump
              env:
                - name: PGPASSWORD
                  valueFrom:
                    secretKeyRef:
                      name: solvyn-secrets
                      key: POSTGRES_PASSWORD
              volumeMounts:
                - name: backups
                  mountPath: /backups
          volumes:
            - name: backups
              persistentVolumeClaim:
                claimName: solvyn-backup-pvc
          restartPolicy: OnFailure
```
