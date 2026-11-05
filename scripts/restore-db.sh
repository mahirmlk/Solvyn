#!/usr/bin/env bash
set -euo pipefail

# Restore a plain SQL gzip backup into an existing database. No database is dropped.
# Usage: DATABASE_URL=... ./scripts/restore-db.sh backup.sql.gz --confirm-database solvyn

if [ "$#" -ne 3 ] || [ "$2" != "--confirm-database" ]; then
  echo "Usage: $0 <backup.sql.gz> --confirm-database <target-database-name>" >&2
  exit 2
fi
BACKUP_FILE="$1"
CONFIRMED_DATABASE="$3"
if [ ! -f "$BACKUP_FILE" ]; then
  echo "Backup file does not exist: $BACKUP_FILE" >&2
  exit 1
fi

DB_URL="${DATABASE_URL:-}"
if [ -z "$DB_URL" ]; then
  echo "Set DATABASE_URL to the restore target explicitly." >&2
  exit 1
fi
export DB_URL
mapfile -d '' -t DB_PARTS < <(python - <<'PY'
import os
import sys
from urllib.parse import parse_qs, unquote, urlsplit

url = urlsplit(os.environ["DB_URL"])
if url.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}:
    raise SystemExit("DATABASE_URL must use PostgreSQL")
if "," in (url.hostname or ""):
    raise SystemExit("Multi-host DATABASE_URL values are not supported by this script")
query = parse_qs(url.query)
values = [
    url.hostname or "localhost",
    str(url.port or 5432),
    unquote(url.username or ""),
    unquote(url.password or ""),
    unquote(url.path.lstrip("/")),
    query.get("sslmode", [""])[0],
]
sys.stdout.buffer.write(b"\0".join(value.encode() for value in values) + b"\0")
PY
)
if [ "${#DB_PARTS[@]}" -ne 6 ] || [ -z "${DB_PARTS[4]}" ]; then
  echo "DATABASE_URL is missing a database name or is invalid." >&2
  exit 1
fi
if [ "${DB_PARTS[4]}" != "$CONFIRMED_DATABASE" ]; then
  echo "Confirmation name does not match the target database '${DB_PARTS[4]}'." >&2
  exit 1
fi
export PGPASSWORD="${DB_PARTS[3]}"
if [ -n "${DB_PARTS[5]}" ]; then export PGSSLMODE="${DB_PARTS[5]}"; fi

if ! gzip -t -- "$BACKUP_FILE"; then
  echo "Backup archive is corrupt or incomplete; restore was not started." >&2
  exit 1
fi

echo "This will apply SQL from ${BACKUP_FILE} to database '${DB_PARTS[4]}'. Existing objects may be replaced."
read -r -p "Type the target database name to continue: " typed_database
if [ "$typed_database" != "$CONFIRMED_DATABASE" ]; then
  echo "Restore cancelled." >&2
  exit 1
fi

gzip -dc -- "$BACKUP_FILE" | psql --host "${DB_PARTS[0]}" --port "${DB_PARTS[1]}" \
  --username "${DB_PARTS[2]}" --dbname "${DB_PARTS[4]}" --no-password \
  --single-transaction --set ON_ERROR_STOP=1
echo "Restore complete."
