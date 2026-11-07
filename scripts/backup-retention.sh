#!/usr/bin/env bash
set -euo pipefail
umask 077

# Run a backup, upload it if configured, then prune expired local archives.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${1:-${BACKUP_DIR:-./backups}}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"

if ! [[ "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  echo "RETENTION_DAYS must be a positive integer." >&2
  exit 2
fi

bash "$SCRIPT_DIR/backup-db.sh" "$BACKUP_DIR"

echo "Removing local Solvyn backups older than ${RETENTION_DAYS} days..."
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'solvyn_*.sql.gz' \
  -mtime "+${RETENTION_DAYS}" -print -delete
