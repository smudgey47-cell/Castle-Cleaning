# Castle Cleaning Schedule

Daily temperature and toilet checks for Castle Sports Lounge. It is a single
Python server using only the standard library, with data in one SQLite file.
The original Google Apps Script version is kept in `apps-script-original/`.

## Install on Debian

Needs Debian 12 or newer, in a Proxmox LXC or on bare metal. Run as root:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/smudgey47-cell/Castle-Cleaning.git
cd Castle-Cleaning
bash deploy/install.sh
```

The script prints the address to open when it finishes. The app listens on port
8080. Give the container a fixed IP address so that this address does not change.

## Update

```bash
cd Castle-Cleaning
git pull
bash deploy/install.sh
```

Updates keep your database and settings.

## Access

The app has no login: anyone who can reach the address can submit checks and
change settings. Keep it on your local network, and do not forward the port on
your router.

## Rota

In Manager View, paste the rota's Google Sheet link and tab name. The sheet must
be shared as **Anyone with the link can view**, because the server has no Google
login. Anyone holding that link can read the rota.

The rota needs a `Date` column and a `Staff Name`, `Person` or `Name` column. The
server re-reads it every 5 minutes.

The **Backup staff list** is used when the rota is not linked, cannot be read, or
has nobody on today. Keep it filled in, as checks cannot be submitted without a
staff name.

## Add or remove an area

Run these in the container, changing the area name as needed. New areas appear
straight away.

```bash
DB=/var/lib/castle-cleaning/castle-cleaning.db

# Add a temperature area
sqlite3 "$DB" "INSERT INTO templates VALUES ('Open/Close Temp', 'Kitchen', 'Open and close', 'Temperature');"

# Add a toilet area
sqlite3 "$DB" "INSERT INTO templates VALUES ('Toilet Check', 'Staff Toilet', 'Open, 2pm, 6pm, 8pm and close', 'Checklist');"

# Remove an area (past check records are kept)
sqlite3 "$DB" "DELETE FROM templates WHERE area = 'Kitchen';"
```

**Repair Setup** in the app puts back any default area that has been removed.

## Backups

A copy of the database is saved to `/var/backups/castle-cleaning/` at 03:30 each
day, and copies are kept for 30 days. These stay inside the container, so also
back up the container in Proxmox, ideally to a different disk.

To restore a copy:

```bash
systemctl stop castle-cleaning
rm -f /var/lib/castle-cleaning/castle-cleaning.db-wal /var/lib/castle-cleaning/castle-cleaning.db-shm
cp /var/backups/castle-cleaning/castle-cleaning-YYYY-MM-DD.db /var/lib/castle-cleaning/castle-cleaning.db
chown castle:castle /var/lib/castle-cleaning/castle-cleaning.db
systemctl start castle-cleaning
```

## Troubleshooting

```bash
systemctl status castle-cleaning
journalctl -u castle-cleaning -n 50
```

The port, timezone and database location are set in `/etc/castle-cleaning.env`.
Restart the service after changing them.

## Tests

```bash
python3 -m unittest discover -s tests
```
