#!/usr/bin/env bash
# Installs or updates Castle Cleaning on Debian. Run as root from a copy of the
# repo:  bash deploy/install.sh
# Safe to run again: the database and /etc/castle-cleaning.env are left alone.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this as root." >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/castle-cleaning
DATA_DIR=/var/lib/castle-cleaning
BACKUP_DIR=/var/backups/castle-cleaning
ENV_FILE=/etc/castle-cleaning.env

apt-get update
apt-get install -y --no-install-recommends python3 sqlite3 tzdata ca-certificates

if ! id castle >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin castle
fi

install -d -o castle -g castle -m 750 "$DATA_DIR" "$BACKUP_DIR"
install -d -m 755 "$APP_DIR" "$APP_DIR/public"
install -m 644 "$SOURCE_DIR/server.py" "$APP_DIR/server.py"
install -m 644 "$SOURCE_DIR/public/index.html" "$APP_DIR/public/index.html"
install -m 755 "$SOURCE_DIR/deploy/backup.sh" "$APP_DIR/backup.sh"

if [ ! -f "$ENV_FILE" ]; then
  install -m 644 "$SOURCE_DIR/deploy/castle-cleaning.env.example" "$ENV_FILE"
fi

install -m 644 "$SOURCE_DIR/deploy/castle-cleaning.service" /etc/systemd/system/
install -m 644 "$SOURCE_DIR/deploy/castle-cleaning-backup.service" /etc/systemd/system/
install -m 644 "$SOURCE_DIR/deploy/castle-cleaning-backup.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable castle-cleaning.service castle-cleaning-backup.timer
systemctl restart castle-cleaning.service
systemctl start castle-cleaning-backup.timer

sleep 2
if ! systemctl is-active --quiet castle-cleaning.service; then
  echo "The service did not start. Recent logs:" >&2
  journalctl -u castle-cleaning.service -n 30 --no-pager >&2
  exit 1
fi

PORT="$(sed -n 's/^CASTLE_PORT=//p' "$ENV_FILE")"
echo
echo "Castle Cleaning is running: http://$(hostname -I | awk '{print $1}'):${PORT:-8080}"
