#!/usr/bin/env bash
set -euo pipefail

# CI-only backup/restore smoke check against the PostgreSQL service database.
# Credentials are disposable and must never be reused outside CI.
BACKUP_DIR="$(mktemp -d)"
RESTORE_DATABASE="solvyn_restore_smoke"
TENANT_B_ID="10000000-0000-4000-8000-000000000002"
TASK_A_ID="20000000-0000-4000-8000-000000000001"
TASK_B_ID="20000000-0000-4000-8000-000000000002"
trap 'rm -rf "$BACKUP_DIR"' EXIT

export PGPASSWORD=solvyn
psql --host localhost --port 5432 --username solvyn --dbname solvyn --no-password \
  --set=ON_ERROR_STOP=1 <<'SQL'
CREATE ROLE solvyn_backup_smoke LOGIN PASSWORD 'backup-smoke' NOSUPERUSER BYPASSRLS;
CREATE ROLE solvyn_restore_smoke LOGIN PASSWORD 'restore-smoke' NOSUPERUSER BYPASSRLS;
GRANT solvyn_migrator TO solvyn_restore_smoke;
GRANT CONNECT ON DATABASE solvyn TO solvyn_backup_smoke;
GRANT USAGE ON SCHEMA public TO solvyn_backup_smoke;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO solvyn_backup_smoke;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO solvyn_backup_smoke;
CREATE DATABASE solvyn_restore_smoke OWNER solvyn_migrator;
GRANT CONNECT ON DATABASE solvyn_restore_smoke TO solvyn_restore_smoke;
GRANT CONNECT ON DATABASE solvyn_restore_smoke TO solvyn_runtime;
SQL

TENANT_A_ID="$(psql --host localhost --port 5432 --username solvyn --dbname solvyn --no-password \
  --tuples-only --no-align --command "SELECT id FROM tenants WHERE slug = 'test-tenant'")"
psql --host localhost --port 5432 --username solvyn --dbname solvyn --no-password \
  --set=ON_ERROR_STOP=1 --command "INSERT INTO tenants (id, name, slug, api_key_hash, role, is_active) VALUES ('${TENANT_B_ID}', 'Backup smoke tenant', 'backup-smoke-tenant', 'smoke-key', 'user', true)"

PGPASSWORD=solvyn_migrator psql --host localhost --port 5432 --username solvyn_migrator --dbname solvyn --no-password \
  --set=ON_ERROR_STOP=1 <<SQL
SELECT set_config('app.tenant_id', '${TENANT_A_ID}', false);
INSERT INTO tasks (id, tenant_id, title, status) VALUES ('${TASK_A_ID}', '${TENANT_A_ID}', 'backup smoke tenant A', 'pending');
SELECT set_config('app.tenant_id', '${TENANT_B_ID}', false);
INSERT INTO tasks (id, tenant_id, title, status) VALUES ('${TASK_B_ID}', '${TENANT_B_ID}', 'backup smoke tenant B', 'pending');
SQL

BACKUP_DATABASE_URL='postgresql://solvyn_backup_smoke:backup-smoke@localhost:5432/solvyn'
RESTORE_DATABASE_URL="postgresql://solvyn_restore_smoke:restore-smoke@localhost:5432/${RESTORE_DATABASE}"
DATABASE_URL="$BACKUP_DATABASE_URL" bash scripts/backup-db.sh "$BACKUP_DIR"
BACKUP_FILE="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'solvyn_*.sql.gz' -print -quit)"
test -n "$BACKUP_FILE"
printf '%s\n' "$RESTORE_DATABASE" | DATABASE_URL="$RESTORE_DATABASE_URL" \
  bash scripts/restore-db.sh "$BACKUP_FILE" --confirm-database "$RESTORE_DATABASE"

export PGPASSWORD=solvyn
SOURCE_VERSION="$(psql --host localhost --port 5432 --username solvyn --dbname solvyn --no-password --tuples-only --no-align --command 'SELECT version_num FROM alembic_version')"
RESTORED_VERSION="$(psql --host localhost --port 5432 --username solvyn --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align --command 'SELECT version_num FROM alembic_version')"
test "$SOURCE_VERSION" = "$RESTORED_VERSION"

for table_name in tenants tasks task_events task_deliveries; do
  SOURCE_COUNT="$(psql --host localhost --port 5432 --username solvyn --dbname solvyn --no-password --tuples-only --no-align --command "SELECT count(*) FROM ${table_name}")"
  RESTORED_COUNT="$(psql --host localhost --port 5432 --username solvyn --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align --command "SELECT count(*) FROM ${table_name}")"
  test "$SOURCE_COUNT" = "$RESTORED_COUNT"
done

for sentinel in "${TASK_A_ID}:${TENANT_A_ID}" "${TASK_B_ID}:${TENANT_B_ID}"; do
  task_id="${sentinel%%:*}"
  tenant_id="${sentinel##*:}"
  restored_task_count="$(psql --host localhost --port 5432 --username solvyn --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align \
    --command "SELECT count(*) FROM tasks WHERE id = '${task_id}' AND tenant_id = '${tenant_id}' AND title LIKE 'backup smoke tenant %'")"
  restored_event_count="$(psql --host localhost --port 5432 --username solvyn --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align \
    --command "SELECT count(*) FROM task_events WHERE task_id = '${task_id}' AND tenant_id = '${tenant_id}' AND event_type = 'created'")"
  test "$restored_task_count" = "1"
  test "$restored_event_count" = "1"
done

for tenant_pair in "${TENANT_A_ID}:${TASK_A_ID}:${TASK_B_ID}" "${TENANT_B_ID}:${TASK_B_ID}:${TASK_A_ID}"; do
  tenant_id="${tenant_pair%%:*}"
  remaining="${tenant_pair#*:}"
  visible_task="${remaining%%:*}"
  hidden_task="${remaining##*:}"
  visible_count="$(PGPASSWORD=solvyn_runtime psql --host localhost --port 5432 --username solvyn_runtime --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align \
    --command "SELECT set_config('app.tenant_id', '${tenant_id}', false); SELECT count(*) FROM tasks WHERE id = '${visible_task}'")"
  hidden_count="$(PGPASSWORD=solvyn_runtime psql --host localhost --port 5432 --username solvyn_runtime --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align \
    --command "SELECT set_config('app.tenant_id', '${tenant_id}', false); SELECT count(*) FROM tasks WHERE id = '${hidden_task}'")"
  visible_event_count="$(PGPASSWORD=solvyn_runtime psql --host localhost --port 5432 --username solvyn_runtime --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align \
    --command "SELECT set_config('app.tenant_id', '${tenant_id}', false); SELECT count(*) FROM task_events WHERE task_id = '${visible_task}' AND event_type = 'created'")"
  hidden_event_count="$(PGPASSWORD=solvyn_runtime psql --host localhost --port 5432 --username solvyn_runtime --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align \
    --command "SELECT set_config('app.tenant_id', '${tenant_id}', false); SELECT count(*) FROM task_events WHERE task_id = '${hidden_task}' AND event_type = 'created'")"
  test "$(printf '%s\n' "$visible_count" | tail -n 1)" = "1"
  test "$(printf '%s\n' "$hidden_count" | tail -n 1)" = "0"
  test "$(printf '%s\n' "$visible_event_count" | tail -n 1)" = "1"
  test "$(printf '%s\n' "$hidden_event_count" | tail -n 1)" = "0"
done

RESTORED_RLS_TABLES="$(psql --host localhost --port 5432 --username solvyn --dbname "$RESTORE_DATABASE" --no-password --tuples-only --no-align --command "SELECT count(*) FROM pg_class WHERE relkind = 'r' AND relrowsecurity AND relforcerowsecurity")"
test "$RESTORED_RLS_TABLES" -gt 0
echo "Backup and restore smoke check passed at migration ${RESTORED_VERSION}; tenant task visibility is isolated and ${RESTORED_RLS_TABLES} tables retain forced RLS."
