#!/usr/bin/env bash
set -euo pipefail

# PostgreSQL backup script for Solvyn
# Usage: ./backup-db.sh [output-dir]
# Requires: pg_dump, gzip, and AWS CLI (optional, for S3 upload)

BACKUP_DIR="${1:-./backups}"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/solvyn_${TIMESTAMP}.sql.gz"
S3_BUCKET="${S3_BUCKET:-s3://solvyn-backups}"

DB_URL="${DATABASE_URL:-postgresql://solvyn:solvyn@localhost:5432/solvyn}"

mkdir -p "${BACKUP_DIR}"

echo "Backing up Solvyn database..."
echo "  Database: ${DB_URL}"
echo "  Output:   ${BACKUP_FILE}"

pg_dump "${DB_URL}" | gzip > "${BACKUP_FILE}"

echo "Backup complete: $(du -h "${BACKUP_FILE}" | cut -f1)"

# Upload to S3 if configured
if [ -n "${S3_BUCKET:-}" ]; then
    echo "Uploading to ${S3_BUCKET}..."
    aws s3 cp "${BACKUP_FILE}" "${S3_BUCKET}/"
    echo "Upload complete."

    # Cleanup local files older than 7 days
    find "${BACKUP_DIR}" -name "solvyn_*.sql.gz" -mtime +7 -delete
    echo "Cleaned up local backups older than 7 days."
fi

echo "Done."
