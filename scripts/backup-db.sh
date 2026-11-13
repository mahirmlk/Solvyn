#!/usr/bin/env bash
set -euo pipefail
umask 077

# Usage: DATABASE_URL=... ./scripts/backup-db.sh [output-dir]
# Optional remote copy: S3_BUCKET=s3://bucket/prefix
# Requires: Python 3, pg_dump, gzip. AWS CLI is only needed with S3_BUCKET.

BACKUP_DIR="${1:-./backups}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_FILE="${BACKUP_DIR}/solvyn_${TIMESTAMP}.sql.gz"
TEMP_FILE="${BACKUP_FILE}.partial"
if [ -z "${DATABASE_URL:-}" ]; then
  echo "Set DATABASE_URL explicitly to the database that should be backed up." >&2
  exit 1
fi
DB_URL="${DATABASE_URL}"
export DB_URL

mkdir -p "${BACKUP_DIR}"
trap 'rm -f "${TEMP_FILE}"' EXIT

# Accept SQLAlchemy's asyncpg URL form while keeping credentials out of argv and logs.
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
export PGPASSWORD="${DB_PARTS[3]}"
if [ -n "${DB_PARTS[5]}" ]; then export PGSSLMODE="${DB_PARTS[5]}"; fi

echo "Backing up database '${DB_PARTS[4]}' to ${BACKUP_FILE}..."
pg_dump --host "${DB_PARTS[0]}" --port "${DB_PARTS[1]}" \
  --username "${DB_PARTS[2]}" --dbname "${DB_PARTS[4]}" --no-password \
  | gzip -c > "${TEMP_FILE}"
mv "${TEMP_FILE}" "${BACKUP_FILE}"
trap - EXIT
echo "Backup complete: $(du -h "${BACKUP_FILE}" | cut -f1)"

if [ -n "${S3_BUCKET:-}" ]; then
  echo "Uploading backup to configured S3 destination..."
  aws s3 cp "${BACKUP_FILE}" "${S3_BUCKET%/}/"
fi
