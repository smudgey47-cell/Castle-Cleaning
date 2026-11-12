"""End-to-end tests. They start the real server against a temporary database.

Run from the project folder with:  python3 -m unittest discover -s tests
"""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server as app  # noqa: E402

FIXED_NOW = datetime(2026, 11, 6, 15, 0, tzinfo=app.TZ)
TODAY = "2026-11-06"

ROTA_CSV = """Date,Staff Name,Start Time,End Time,Shift 2 Start,Shift 2 End,Type,Notes
06/11/2026,Sam Jones,09:00,17:00,,,,
06/11/2026,Alex Lee,17:00,23:30,,,,
06/11/2026,Sam Jones,18:00,20:00,,,,
06/11/2026,Jo Holiday,,,,,Holiday,
06/11/2026,Rest Person,,,,,,
2026-11-06,Iso Date,10:00,12:00,,,,
07/11/2026,Tomorrow Person,09:00,17:00,,,,
"""


class FakeGoogle(BaseHTTPRequestHandler):
    """Stands in for the Google Sheets CSV endpoint."""

    body = ROTA_CSV
    content_type = "text/csv; charset=utf-8"
    requests = 0

    def do_GET(self):
        FakeGoogle.requests += 1
        data = FakeGoogle.body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", FakeGoogle.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Quiet(app.Handler):
    def log_message(self, *args):
        pass


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

        patches = [
            mock.patch.object(app, "DB_PATH", os.path.join(self.tmp.name, "test.db")),
            mock.patch.object(app, "now_local", lambda: FIXED_NOW),
            mock.patch.object(app, "rota_cache", app.RotaCache()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

        app.init_db()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start_fake_google(self):
        FakeGoogle.body = ROTA_CSV
        FakeGoogle.content_type = "text/csv; charset=utf-8"
        FakeGoogle.requests = 0
        google = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogle)
        threading.Thread(target=google.serve_forever, daemon=True).start()
        self.addCleanup(google.server_close)
        self.addCleanup(google.shutdown)
        url = f"http://127.0.0.1:{google.server_address[1]}/{{id}}?{{query}}"
        patch = mock.patch.object(app, "ROTA_URL", url)
        patch.start()
        self.addCleanup(patch.stop)

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def data(self):
        status, body = self.request("/api/data")
        self.assertEqual(status, 200)
        return body["data"]

    def task(self, data, area, slot):
        return next(t for t in data["tasks"] if t["area"] == area and t["slotLabel"] == slot)

    def submit(self, task_id, staff="Sam", value="", notes=""):
        return self.request(
            "/api/checks",
            {"taskId": task_id, "staffName": staff, "value": value, "notes": notes},
        )

    def test_page_has_no_apps_script_left(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as response:
            page = response.read().decode("utf-8")
        self.assertIn("Castle Cleaning Schedule", page)
        self.assertIn("°C", page)
        for leftover in ("google.script", "<?", "runServer"):
            self.assertNotIn(leftover, page)

    def test_builds_all_checks_for_today(self):
        data = self.data()
        self.assertEqual(data["venueName"], "Castle Sports Lounge")
        self.assertEqual(data["today"], "Friday 6 November 2026")
        self.assertEqual(len(data["tasks"]), 3 * 2 + 3 * 5)
        self.assertEqual(data["tasks"][0]["dueDisplay"], "10:00")
        self.assertEqual(data["stats"]["total"], 21)

    def test_due_and_overdue_at_3pm(self):
        data = self.data()
        late = self.task(data, "Bar", "Opening Temp")
        self.assertTrue(late["overdue"])
        self.assertEqual(late["status"], "Overdue")
        self.assertEqual(late["minutesLate"], 300)
        upcoming = self.task(data, "Bar", "Closing Temp")
        self.assertFalse(upcoming["overdue"])
        self.assertEqual(upcoming["status"], "Due")
        # Opening temps x3, open toilet x3 and 2pm toilet x3.
        self.assertEqual(data["stats"]["overdue"], 9)

    def test_saved_check_is_marked_done(self):
        task = self.task(self.data(), "Bar", "Opening Temp")
        status, body = self.submit(task["id"], value="4.2", notes="  fine  ")
        self.assertEqual(status, 200)
        saved = self.task(body["data"], "Bar", "Opening Temp")
        self.assertTrue(saved["done"])
        self.assertFalse(saved["overdue"])
        self.assertEqual(saved["status"], "Complete")
        self.assertEqual(saved["latest"]["staffName"], "Sam")
        self.assertEqual(saved["latest"]["timestamp"], "06/11/2026 15:00")
        self.assertEqual(saved["latest"]["notes"], "fine")
        self.assertEqual(body["data"]["stats"]["done"], 1)
        self.assertEqual(body["data"]["stats"]["completionPercent"], 5)
        self.assertTrue(self.task(self.data(), "Bar", "Opening Temp")["done"])

    def test_temperature_flags(self):
        cases = [
            ("Bar", "9", "Flag - Temp out of range"),
            ("Cellar", "-1", "Flag - Temp out of range"),
            ("Canteen", "abc", "Flag - No temp entered"),
        ]
        for area, value, expected in cases:
            task = self.task(self.data(), area, "Opening Temp")
            _, body = self.submit(task["id"], value=value)
            self.assertEqual(self.task(body["data"], area, "Opening Temp")["status"], expected)
        self.assertEqual(self.data()["stats"]["flagged"], 3)

    def test_temperature_limits_are_allowed(self):
        for area, value in (("Bar", "0"), ("Cellar", "8")):
            task = self.task(self.data(), area, "Opening Temp")
            _, body = self.submit(task["id"], value=value)
            self.assertEqual(self.task(body["data"], area, "Opening Temp")["status"], "Complete")

    def test_checklist_values_are_joined(self):
        task = self.task(self.data(), "Gents Toilets", "2pm Check")
        _, body = self.submit(task["id"], value=["Soap stocked", "Bins checked"])
        saved = self.task(body["data"], "Gents Toilets", "2pm Check")
        self.assertEqual(saved["latest"]["value"], "Soap stocked; Bins checked")
        self.assertEqual(saved["status"], "Complete")

    def test_rejects_bad_checks(self):
        task = self.task(self.data(), "Bar", "Opening Temp")
        status, body = self.submit(task["id"], staff="   ")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "Staff name and task are required.")
        status, body = self.submit("2020-01-01|Toilet Check|Bar|Old")
        self.assertEqual(status, 400)
        self.assertIn("could not be found", body["error"])
        self.assertEqual(self.data()["stats"]["done"], 0)

    def test_bad_json_is_rejected_cleanly(self):
        request = urllib.request.Request(self.base + "/api/checks", data=b"{nope")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)
        self.assertFalse(json.loads(caught.exception.read())["ok"])

    def test_settings_change_due_times_and_limits(self):
        status, body = self.request(
            "/api/settings",
            {"openTempDueBy": "16:30", "openCloseMaxTemp": "5", "somethingElse": "x"},
        )
        self.assertEqual(status, 200)
        settings = body["data"]["settings"]
        self.assertEqual(settings["openCloseMaxTemp"], 5)
        self.assertNotIn("somethingElse", settings)
        task = self.task(body["data"], "Bar", "Opening Temp")
        self.assertEqual(task["dueDisplay"], "16:30")
        self.assertFalse(task["overdue"])
        _, saved = self.submit(task["id"], value="6")
        self.assertEqual(
            self.task(saved["data"], "Bar", "Opening Temp")["status"],
            "Flag - Temp out of range",
        )

    def test_blank_max_temp_falls_back_to_default(self):
        _, body = self.request("/api/settings", {"openCloseMaxTemp": ""})
        self.assertEqual(body["data"]["settings"]["openCloseMaxTemp"], 8)

    def test_csv_export(self):
        task = self.task(self.data(), "Bar", "Opening Temp")
        self.submit(task["id"], value="4.2", notes='said "ok"')
        with urllib.request.urlopen(self.base + "/api/logs.csv", timeout=10) as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            lines = response.read().decode("utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith('"Timestamp","Date","Task Type"'))
        self.assertIn('"said ""ok"""', lines[1])

    def test_repair_restores_a_removed_area_and_keeps_settings(self):
        conn = app.connect()
        with conn:
            conn.execute("DELETE FROM templates WHERE area = 'Cellar'")
        conn.close()
        self.request("/api/settings", {"openCloseMaxTemp": "5"})
        self.assertEqual(len(self.data()["tasks"]), 19)
        app.init_db()
        self.assertEqual(len(self.data()["tasks"]), 19)
        status, _ = self.request("/api/setup", {})
        self.assertEqual(status, 200)
        data = self.data()
        self.assertEqual(len(data["tasks"]), 21)
        self.assertEqual(data["settings"]["openCloseMaxTemp"], 5)

    def test_no_rota_and_no_list_gives_no_staff(self):
        data = self.data()
        self.assertEqual(data["staffOptions"], [])
        self.assertEqual(data["rotaError"], "")

    def test_backup_staff_list(self):
        _, body = self.request("/api/settings", {"staffList": "Zed\n\n  amy  \nZED\n"})
        names = [person["name"] for person in body["data"]["staffOptions"]]
        self.assertEqual(names, ["amy", "Zed"])

    def test_rota_filters_to_todays_working_staff(self):
        self.start_fake_google()
        url = "https://docs.google.com/spreadsheets/d/abc-123_X/edit#gid=0"
        _, body = self.request("/api/settings", {"rotaSpreadsheetId": url})
        staff = body["data"]["staffOptions"]
        self.assertEqual([p["name"] for p in staff], ["Alex Lee", "Iso Date", "Sam Jones"])
        self.assertEqual(staff[2]["label"], "Sam Jones - 09:00-17:00")
        self.assertEqual(body["data"]["rotaError"], "")

    def test_rota_is_cached_between_page_loads(self):
        self.start_fake_google()
        self.request("/api/settings", {"rotaSpreadsheetId": "abc"})
        for _ in range(5):
            self.data()
        self.assertEqual(FakeGoogle.requests, 1)

    def test_stale_rota_is_served_without_waiting(self):
        self.start_fake_google()
        self.request("/api/settings", {"rotaSpreadsheetId": "abc"})
        for entry in app.rota_cache.entries.values():
            entry["expires"] = 0
        FakeGoogle.body = ROTA_CSV.replace("Alex Lee", "New Starter")
        first = [p["name"] for p in self.data()["staffOptions"]]
        self.assertIn("Alex Lee", first)
        for _ in range(100):
            if "New Starter" in [p["name"] for p in self.data()["staffOptions"]]:
                break
            threading.Event().wait(0.02)
        else:
            self.fail("The rota was not refreshed in the background")

    def test_private_rota_explains_itself_and_uses_backup(self):
        self.start_fake_google()
        FakeGoogle.body = "<html>Sign in</html>"
        FakeGoogle.content_type = "text/html"
        _, body = self.request("/api/settings", {"rotaSpreadsheetId": "abc"})
        self.assertIn("Anyone with the link", body["data"]["rotaError"])
        self.assertEqual(body["data"]["staffOptions"][0]["name"], "")

        _, body = self.request("/api/settings", {"staffList": "Backup Person"})
        self.assertEqual(body["data"]["staffOptions"][0]["name"], "Backup Person")
        self.assertIn("Anyone with the link", body["data"]["rotaError"])

    def test_wrong_sheet_name_is_reported(self):
        self.start_fake_google()
        FakeGoogle.body = "Name,Thing\nx,y\n"
        _, body = self.request("/api/settings", {"rotaSpreadsheetId": "abc"})
        self.assertIn("no Date column", body["data"]["rotaError"])

    def start_fake_management(self, body, content_type="text/csv; charset=utf-8"):
        seen = []

        class FakeManagement(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                data = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeManagement)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", seen

    def test_rota_from_castle_management(self):
        feed = (
            "Date,Staff Name,Start Time,End Time,Shift 2 Start,Shift 2 End,Notes\n"
            "2026-11-06,Sam Jones,OPEN,17:00,,,\n"
            "2026-11-06,Alex Lee,17:00,CLOSE,,,\n"
            "2026-11-06,Jo Away,HOLIDAY,,,,Approved holiday request\n"
            "2026-11-06,Rest Day,OFF,,,,\n"
        )
        base, seen = self.start_fake_management(feed)
        for link in (base, base + "/", base + "/rota.csv"):
            app.rota_cache.clear()
            seen.clear()
            _, body = self.request("/api/settings", {"rotaSpreadsheetId": link})
            staff = body["data"]["staffOptions"]
            self.assertEqual([p["name"] for p in staff], ["Alex Lee", "Sam Jones"])
            self.assertEqual(staff[1]["label"], "Sam Jones - OPEN-17:00")
            self.assertEqual(body["data"]["rotaError"], "")
            self.assertEqual(seen, [f"/rota.csv?date={TODAY}"])

    def test_wrong_management_address_is_reported(self):
        base, _ = self.start_fake_management("<html></html>", "text/html")
        _, body = self.request("/api/settings", {"rotaSpreadsheetId": base})
        self.assertIn("did not return a rota", body["data"]["rotaError"])

    def test_unreachable_rota_does_not_break_the_app(self):
        with mock.patch.object(app, "ROTA_URL", "http://127.0.0.1:9/{id}?{query}"):
            status, body = self.request("/api/settings", {"rotaSpreadsheetId": "abc"})
        self.assertEqual(status, 200)
        self.assertIn("Could not reach", body["data"]["rotaError"])
        self.assertEqual(len(body["data"]["tasks"]), 21)


class DateParsingTest(unittest.TestCase):
    def test_date_formats(self):
        cases = {
            "2026-11-06": TODAY,
            "2026-11-06T09:00:00": TODAY,
            "06/11/2026": TODAY,
            "6/11/2026": TODAY,
            "6/11/26": TODAY,
            "6 Nov 2026": TODAY,
            "6 November 2026": TODAY,
            "Fri 6 Nov 2026": TODAY,
            "Friday 6 November 2026": TODAY,
        }
        for text, expected in cases.items():
            self.assertEqual(app.normalise_date_text(text), expected, text)


if __name__ == "__main__":
    unittest.main()
