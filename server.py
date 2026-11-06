#!/usr/bin/env python3
"""Castle Cleaning Schedule server.

Uses only the Python standard library, so there is nothing to install beyond
python3. Data lives in one SQLite file. Configuration comes from environment
variables, see deploy/castle-cleaning.env.example.
"""

import csv
import io
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent
INDEX_FILE = BASE_DIR / "public" / "index.html"

HOST = os.environ.get("CASTLE_HOST", "0.0.0.0")
PORT = int(os.environ.get("CASTLE_PORT", "8080"))
DB_PATH = os.environ.get("CASTLE_DB", str(BASE_DIR / "castle-cleaning.db"))
TZ = ZoneInfo(os.environ.get("CASTLE_TZ", "Europe/London"))

MAX_BODY_BYTES = 64 * 1024
RECENT_LOG_LIMIT = 50

ROTA_URL = "https://docs.google.com/spreadsheets/d/{id}/gviz/tq?{query}"
ROTA_TIMEOUT_SECONDS = 5
ROTA_FRESH_SECONDS = 300
ROTA_RETRY_SECONDS = 60
ROTA_ERROR_LABEL = (
    "Could not read rota - check the link, and that the sheet is shared as "
    "'Anyone with the link can view'"
)

DEFAULT_SETTINGS = {
    "venueName": "Castle Sports Lounge",
    "openTempDueBy": "10:00",
    "closeTempDueBy": "23:30",
    "openCloseMinTemp": 0,
    "openCloseMaxTemp": 8,
    "toiletOpenDueBy": "10:00",
    "toiletTwoPmDueBy": "14:00",
    "toiletSixPmDueBy": "18:00",
    "toiletEightPmDueBy": "20:00",
    "toiletCloseDueBy": "23:30",
    "rotaSpreadsheetId": "",
    "rotaSheetName": "Shifts",
    "staffList": "",
}

DEFAULT_TEMPLATES = [
    ("Open/Close Temp", "Bar", "Open and close", "Temperature"),
    ("Open/Close Temp", "Cellar", "Open and close", "Temperature"),
    ("Open/Close Temp", "Canteen", "Open and close", "Temperature"),
    ("Toilet Check", "Ladies Toilets", "Open, 2pm, 6pm, 8pm and close", "Checklist"),
    ("Toilet Check", "Gents Toilets", "Open, 2pm, 6pm, 8pm and close", "Checklist"),
    ("Toilet Check", "Accessible Toilet", "Open, 2pm, 6pm, 8pm and close", "Checklist"),
]

TEMP_SLOTS = [
    ("Opening Temp", "openTempDueBy"),
    ("Closing Temp", "closeTempDueBy"),
]

TOILET_SLOTS = [
    ("Open Check", "toiletOpenDueBy"),
    ("2pm Check", "toiletTwoPmDueBy"),
    ("6pm Check", "toiletSixPmDueBy"),
    ("8pm Check", "toiletEightPmDueBy"),
    ("Close Check", "toiletCloseDueBy"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    task_type TEXT NOT NULL,
    area      TEXT NOT NULL,
    schedule  TEXT NOT NULL,
    input     TEXT NOT NULL,
    PRIMARY KEY (task_type, area)
);
CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    date       TEXT NOT NULL,
    task_type  TEXT NOT NULL,
    area       TEXT NOT NULL,
    slot       TEXT NOT NULL,
    staff_name TEXT NOT NULL,
    value      TEXT NOT NULL,
    status     TEXT NOT NULL,
    notes      TEXT NOT NULL,
    task_id    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS logs_date ON logs (date);
"""

log = logging.getLogger("castle-cleaning")


class BadRequest(Exception):
    """A problem the person using the app can fix. Its message is shown to them."""


class RotaError(Exception):
    """The rota could not be read."""


def now_local():
    return datetime.now(TZ)


def today_key():
    return now_local().strftime("%Y-%m-%d")


def today_display():
    today = now_local()
    return f"{today:%A} {today.day} {today:%B %Y}"


def clean_text(value):
    return "" if value is None else str(value).strip()


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def restore_defaults(conn):
    """Add any missing default settings and areas. Existing values are kept."""
    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        [(key, str(value)) for key, value in DEFAULT_SETTINGS.items()],
    )
    conn.executemany(
        "INSERT OR IGNORE INTO templates (task_type, area, schedule, input) VALUES (?, ?, ?, ?)",
        DEFAULT_TEMPLATES,
    )


def init_db():
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        # Areas are only seeded into an empty table, so a default area that has
        # been deliberately removed stays removed across restarts.
        has_areas = conn.execute("SELECT 1 FROM templates LIMIT 1").fetchone()
        with conn:
            if has_areas:
                conn.executemany(
                    "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                    [(key, str(value)) for key, value in DEFAULT_SETTINGS.items()],
                )
            else:
                restore_defaults(conn)
    finally:
        conn.close()


def to_number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def get_settings(conn):
    settings = dict(DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        settings[row["key"]] = row["value"]
    settings["openCloseMinTemp"] = to_number(
        settings["openCloseMinTemp"], DEFAULT_SETTINGS["openCloseMinTemp"]
    )
    settings["openCloseMaxTemp"] = to_number(
        settings["openCloseMaxTemp"], DEFAULT_SETTINGS["openCloseMaxTemp"]
    )
    return settings


def row_to_log(row):
    stamp = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
    return {
        "timestamp": stamp.strftime("%d/%m/%Y %H:%M"),
        "date": row["date"],
        "type": row["task_type"],
        "area": row["area"],
        "slotLabel": row["slot"],
        "staffName": row["staff_name"],
        "value": row["value"],
        "status": row["status"],
        "notes": row["notes"],
        "taskId": row["task_id"],
    }


def get_logs_for_date(conn, date_key):
    rows = conn.execute("SELECT * FROM logs WHERE date = ? ORDER BY id", (date_key,))
    return [row_to_log(row) for row in rows]


def get_recent_logs(conn, limit):
    rows = conn.execute("SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,))
    return [row_to_log(row) for row in rows]


def get_areas(conn, task_type):
    rows = conn.execute(
        "SELECT area FROM templates WHERE task_type = ? ORDER BY rowid", (task_type,)
    )
    return [row["area"] for row in rows]


def time_to_minutes(value):
    parts = str(value).split(":")
    try:
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0
    return hours * 60 + minutes


def format_time(value):
    minutes = time_to_minutes(value)
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def create_task(task_type, area, slot_label, due_time, input_type, logs, now):
    task_id = "|".join([now.strftime("%Y-%m-%d"), task_type, area, slot_label])
    matching = [entry for entry in logs if entry["taskId"] == task_id]
    latest = matching[-1] if matching else None
    due_minutes = time_to_minutes(due_time)
    minutes_late = now.hour * 60 + now.minute - due_minutes
    done = latest is not None
    overdue = not done and minutes_late > 0

    if done:
        status = latest["status"]
    else:
        status = "Overdue" if overdue else "Due"

    return {
        "id": task_id,
        "type": task_type,
        "area": area,
        "slotLabel": slot_label,
        "dueTime": due_time,
        "dueDisplay": format_time(due_time),
        "inputType": input_type,
        "done": done,
        "overdue": overdue,
        "status": status,
        "latest": latest,
        "sortMinutes": due_minutes,
        "minutesLate": max(minutes_late, 0),
    }


def build_tasks(conn, settings, logs, now):
    groups = [
        ("Open/Close Temp", TEMP_SLOTS, "temperature"),
        ("Toilet Check", TOILET_SLOTS, "checklist"),
    ]
    tasks = []
    for task_type, slots, input_type in groups:
        for area in get_areas(conn, task_type):
            for label, setting_key in slots:
                due_time = str(settings[setting_key] or DEFAULT_SETTINGS[setting_key])
                tasks.append(create_task(task_type, area, label, due_time, input_type, logs, now))
    tasks.sort(key=lambda task: (task["sortMinutes"], task["area"].lower()))
    return tasks


def build_stats(tasks, logs):
    total = len(tasks)
    done = sum(1 for task in tasks if task["done"])
    return {
        "total": total,
        "done": done,
        "due": total - done,
        "overdue": sum(1 for task in tasks if task["overdue"]),
        "flagged": sum(1 for entry in logs if str(entry["status"]).startswith("Flag")),
        "completionPercent": round(done / total * 100) if total else 0,
    }


def submitted_status(task, value, settings):
    if task["inputType"] == "temperature":
        try:
            temperature = float(value)
        except ValueError:
            return "Flag - No temp entered"
        if not math.isfinite(temperature):
            return "Flag - No temp entered"
        if temperature < settings["openCloseMinTemp"] or temperature > settings["openCloseMaxTemp"]:
            return "Flag - Temp out of range"
    return "Complete"


def normalise_value(value):
    if isinstance(value, list):
        return "; ".join(str(item) for item in value)
    return clean_text(value)


def extract_spreadsheet_id(value):
    text = clean_text(value)
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", text)
    return match.group(1) if match else text


def normalise_date_text(value):
    text = clean_text(value)
    if not text:
        return ""

    iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return "-".join(iso.groups())

    uk = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if uk:
        return f"{uk.group(3)}-{int(uk.group(2)):02d}-{int(uk.group(1)):02d}"

    for date_format in ("%d/%m/%y", "%d %b %Y", "%d %B %Y", "%a %d %b %Y", "%A %d %B %Y"):
        try:
            return datetime.strptime(text, date_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text


def is_working_shift(row):
    fields = ["Start Time", "End Time", "Shift 2 Start", "Shift 2 End", "Type", "Notes"]
    combined = " ".join(clean_text(row.get(field)) for field in fields).strip().lower()
    if not combined:
        return False
    return not re.search(r"\b(off|holiday|annual leave|rest day)\b", combined)


def rota_staff_label(name, row):
    first = "-".join(filter(None, [clean_text(row.get("Start Time")), clean_text(row.get("End Time"))]))
    second = "-".join(filter(None, [clean_text(row.get("Shift 2 Start")), clean_text(row.get("Shift 2 End"))]))
    return " - ".join(filter(None, [name, first, second]))


def staff_from_rota_csv(text, date_key):
    reader = csv.reader(io.StringIO(text))
    headers = [clean_text(header) for header in next(reader, [])]
    if "Date" not in headers:
        raise RotaError("The rota sheet has no Date column. Check the rota sheet name.")

    seen = set()
    staff = []
    for values in reader:
        row = {header: value for header, value in zip(headers, values) if header}
        if normalise_date_text(row.get("Date")) != date_key:
            continue
        name = clean_text(row.get("Staff Name") or row.get("Person") or row.get("Name"))
        if not name or name.lower() in seen or not is_working_shift(row):
            continue
        seen.add(name.lower())
        staff.append({"name": name, "label": rota_staff_label(name, row)})

    staff.sort(key=lambda person: person["name"].lower())
    return staff


def download_rota(spreadsheet_id, sheet_name):
    query = urllib.parse.urlencode({"tqx": "out:csv", "sheet": sheet_name, "headers": 1})
    url = ROTA_URL.format(id=urllib.parse.quote(spreadsheet_id, safe=""), query=query)
    request = urllib.request.Request(url, headers={"User-Agent": "castle-cleaning"})
    try:
        with urllib.request.urlopen(request, timeout=ROTA_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except OSError as error:
        raise RotaError(f"Could not reach the rota sheet: {error}") from error

    # A sheet that is not shared redirects to a Google sign-in page.
    if "text/csv" not in content_type:
        raise RotaError("The rota sheet is not shared with 'Anyone with the link'.")
    return body.decode("utf-8-sig")


class RotaCache:
    """Keeps the rota in memory so page loads do not wait on Google.

    A stale rota is served straight away and refreshed in the background. The
    only request that waits is the first one of the day.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}
        self.refreshing = set()

    def clear(self):
        with self.lock:
            self.entries.clear()

    def get(self, spreadsheet_id, sheet_name, date_key):
        key = (spreadsheet_id, sheet_name, date_key)
        with self.lock:
            entry = self.entries.get(key)
            if entry is None:
                start_background = False
            elif time.monotonic() < entry["expires"]:
                return entry["staff"], entry["error"]
            else:
                start_background = key not in self.refreshing
                if start_background:
                    self.refreshing.add(key)

        if entry is None:
            return self.refresh(key)
        if start_background:
            threading.Thread(target=self.refresh, args=(key,), daemon=True).start()
        return entry["staff"], entry["error"]

    def refresh(self, key):
        spreadsheet_id, sheet_name, date_key = key
        error = ""
        try:
            staff = staff_from_rota_csv(download_rota(spreadsheet_id, sheet_name), date_key)
            lifetime = ROTA_FRESH_SECONDS
        except RotaError as failure:
            log.warning("Rota read failed: %s", failure)
            error = str(failure)
            lifetime = ROTA_RETRY_SECONDS
            with self.lock:
                previous = self.entries.get(key)
            staff = previous["staff"] if previous else []

        with self.lock:
            self.entries[key] = {
                "staff": staff,
                "error": error,
                "expires": time.monotonic() + lifetime,
            }
            self.refreshing.discard(key)
        return staff, error


rota_cache = RotaCache()


def staff_from_list(text):
    names = []
    seen = set()
    for line in str(text).splitlines():
        name = line.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    return [{"name": name, "label": name} for name in sorted(names, key=str.lower)]


def get_staff_options(settings):
    """Return (staff options, rota problem). The saved staff list is the backup."""
    spreadsheet_id = extract_spreadsheet_id(settings["rotaSpreadsheetId"])
    sheet_name = clean_text(settings["rotaSheetName"]) or "Shifts"
    rota_staff, rota_error = [], ""
    if spreadsheet_id:
        rota_staff, rota_error = rota_cache.get(spreadsheet_id, sheet_name, today_key())
    if rota_staff:
        return rota_staff, rota_error

    backup = staff_from_list(settings["staffList"])
    if backup:
        return backup, rota_error
    if rota_error:
        return [{"name": "", "label": ROTA_ERROR_LABEL, "error": rota_error}], rota_error
    return [], ""


def get_app_data(conn):
    settings = get_settings(conn)
    logs = get_logs_for_date(conn, today_key())
    tasks = build_tasks(conn, settings, logs, now_local())
    staff_options, rota_error = get_staff_options(settings)
    return {
        "venueName": settings["venueName"],
        "today": today_display(),
        "settings": settings,
        "tasks": tasks,
        "stats": build_stats(tasks, logs),
        "recentLogs": get_recent_logs(conn, RECENT_LOG_LIMIT),
        "staffOptions": staff_options,
        "rotaError": rota_error,
    }


def save_check(conn, payload):
    task_id = clean_text(payload.get("taskId"))
    staff_name = clean_text(payload.get("staffName"))
    if not task_id or not staff_name:
        raise BadRequest("Staff name and task are required.")

    settings = get_settings(conn)
    now = now_local()
    tasks = build_tasks(conn, settings, [], now)
    task = next((item for item in tasks if item["id"] == task_id), None)
    if task is None:
        raise BadRequest("This check could not be found. Refresh and try again.")

    value = normalise_value(payload.get("value"))
    with conn:
        conn.execute(
            """INSERT INTO logs
               (timestamp, date, task_type, area, slot, staff_name, value, status, notes, task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                now.strftime("%Y-%m-%d %H:%M:%S"),
                now.strftime("%Y-%m-%d"),
                task["type"],
                task["area"],
                task["slotLabel"],
                staff_name,
                value,
                submitted_status(task, value, settings),
                clean_text(payload.get("notes")),
                task["id"],
            ),
        )
    return get_app_data(conn)


def save_settings(conn, payload):
    rows = [(key, str(payload[key])) for key in DEFAULT_SETTINGS if payload.get(key) is not None]
    with conn:
        conn.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            rows,
        )
    rota_cache.clear()
    return get_app_data(conn)


def repair_setup(conn, _payload):
    with conn:
        restore_defaults(conn)
    rota_cache.clear()
    return get_app_data(conn)


def logs_csv(conn):
    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerow(
        ["Timestamp", "Date", "Task Type", "Area", "Slot", "Staff Name", "Value", "Status", "Notes", "Task ID"]
    )
    rows = conn.execute(
        """SELECT timestamp, date, task_type, area, slot, staff_name, value, status, notes, task_id
           FROM logs ORDER BY id"""
    )
    writer.writerows(tuple(row) for row in rows)
    return output.getvalue()


POST_ROUTES = {
    "/api/checks": save_check,
    "/api/settings": save_settings,
    "/api/setup": repair_setup,
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CastleCleaning"
    sys_version = ""
    timeout = 30

    def send_body(self, status, content_type, body, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_body(status, "application/json; charset=utf-8", body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.close_connection = True
            raise BadRequest("Invalid request.") from None
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            raise BadRequest("That request is too large.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            raise BadRequest("Invalid request.") from None
        if not isinstance(payload, dict):
            raise BadRequest("Invalid request.")
        return payload

    def run_api(self, action):
        try:
            conn = connect()
            try:
                data = action(conn)
            finally:
                conn.close()
        except BadRequest as error:
            self.send_json(400, {"ok": False, "error": str(error)})
        except Exception:
            log.exception("Request failed: %s %s", self.command, self.path)
            self.send_json(500, {"ok": False, "error": "The server hit an error. Check its logs."})
        else:
            self.send_json(200, {"ok": True, "data": data})

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self.send_body(200, "text/html; charset=utf-8", INDEX_FILE.read_bytes())
        elif path == "/api/data":
            self.run_api(get_app_data)
        elif path == "/api/logs.csv":
            self.send_csv()
        else:
            self.send_json(404, {"ok": False, "error": "Not found."})

    def send_csv(self):
        conn = connect()
        try:
            body = logs_csv(conn).encode("utf-8")
        finally:
            conn.close()
        self.send_body(
            200,
            "text/csv; charset=utf-8",
            body,
            {"Content-Disposition": 'attachment; filename="castle-cleaning-logs.csv"'},
        )

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        route = POST_ROUTES.get(path)
        if route is None:
            self.close_connection = True
            self.send_json(404, {"ok": False, "error": "Not found."})
            return
        try:
            payload = self.read_json()
        except BadRequest as error:
            self.send_json(400, {"ok": False, "error": str(error)})
            return
        self.run_api(lambda conn: route(conn, payload))


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("Castle Cleaning running on http://%s:%s (database: %s)", HOST, PORT, DB_PATH)
    server.serve_forever()


if __name__ == "__main__":
    main()
