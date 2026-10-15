#!/bin/bash
# Nightly database backup for the AB Group staff app.
#
#   Install (once):   chmod 700 deploy/backup.sh
#                     (crontab -l 2>/dev/null; echo "15 2 * * * $PWD/deploy/backup.sh >> /root/backups/ab-roster/backup.log 2>&1") | crontab -
#   Run by hand:      ./deploy/backup.sh
#
# Keeps KEEP_DAYS days of compressed dumps in BACKUP_DIR. A failed dump never
# replaces a good file: it's written to .tmp first and only renamed on success.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/root/backups/ab-roster}"
KEEP_DAYS="${KEEP_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
cd "$APP_DIR"

file="$BACKUP_DIR/roster-$(date +%Y-%m-%d-%H%M).sql.gz"
docker compose exec -T roster-db pg_dump -U roster --clean --if-exists roster | gzip > "$file.tmp"

# Sanity check: a real dump contains the staff table.
if ! gunzip -c "$file.tmp" | grep -q "CREATE TABLE public.staff"; then
  echo "$(date '+%F %T') BACKUP FAILED: dump looks empty or incomplete" >&2
  rm -f "$file.tmp"
  exit 1
fi

mv "$file.tmp" "$file"
chmod 600 "$file"
find "$BACKUP_DIR" -name 'roster-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "$(date '+%F %T') backup OK: $file ($(du -h "$file" | cut -f1))"
