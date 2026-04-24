#!/bin/bash
# ============================================================
# GenLy AI — PostgreSQL Backup Script
# ============================================================
# Usage:
#   ./scripts/backup.sh                    # Manual backup
#   crontab: 0 3 * * * /path/to/backup.sh  # Daily at 3 AM
#
# Environment:
#   PGHOST, PGUSER, PGPASSWORD, PGDATABASE (or uses defaults)
#   BACKUP_DIR (default: ./backups)
#   BACKUP_RETENTION_DAYS (default: 30)
#   S3_BACKUP_BUCKET (optional, for S3 upload)
# ============================================================

set -euo pipefail

# Config
PGHOST="${PGHOST:-localhost}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-genly}"
PGDATABASE="${PGDATABASE:-genly}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$0")/../backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

# Create backup dir
mkdir -p "$BACKUP_DIR"

# Timestamp
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
FILENAME="genly_backup_${TIMESTAMP}.sql.gz"
FILEPATH="${BACKUP_DIR}/${FILENAME}"

echo "╔══════════════════════════════════════════╗"
echo "║  GenLy AI — Database Backup              ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Database: ${PGDATABASE}@${PGHOST}:${PGPORT}"
echo "Output:   ${FILEPATH}"
echo ""

# Dump
echo "[1/3] Dumping database..."
pg_dump -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" "$PGDATABASE" \
    --no-owner --no-privileges --clean --if-exists \
    | gzip > "$FILEPATH"

SIZE=$(du -h "$FILEPATH" | cut -f1)
echo "      Done! Size: ${SIZE}"

# Upload to S3 (if configured)
if [ -n "${S3_BACKUP_BUCKET:-}" ]; then
    echo "[2/3] Uploading to S3..."
    aws s3 cp "$FILEPATH" "s3://${S3_BACKUP_BUCKET}/backups/${FILENAME}" --quiet
    echo "      Uploaded to s3://${S3_BACKUP_BUCKET}/backups/${FILENAME}"
else
    echo "[2/3] S3 upload skipped (S3_BACKUP_BUCKET not set)"
fi

# Cleanup old backups
echo "[3/3] Cleaning backups older than ${RETENTION_DAYS} days..."
DELETED=$(find "$BACKUP_DIR" -name "genly_backup_*.sql.gz" -mtime +"$RETENTION_DAYS" -delete -print | wc -l)
echo "      Deleted ${DELETED} old backup(s)"

echo ""
echo "Backup complete: ${FILEPATH}"
