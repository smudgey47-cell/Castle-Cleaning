#!/bin/sh
# Takes a safe copy of the live database and removes copies over 30 days old.
set -eu

DB="${CASTLE_DB:-/var/lib/castle-cleaning/castle-cleaning.db}"
BACKUP_DIR="${CASTLE_BACKUP_DIR:-/var/backups/castle-cleaning}"

sqlite3 "$DB" ".backup '$BACKUP_DIR/castle-cleaning-$(date +%F).db'"
find "$BACKUP_DIR" -name 'castle-cleaning-*.db' -mtime +30 -delete
