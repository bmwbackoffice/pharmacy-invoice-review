"""Dashboard split into Work / System health, the Last scheduled run tile, and the clearer labels that
replaced the five meanings of "Ignore" (2026-10-12)."""
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import _base  # noqa: F401
import review_app
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
TEMPLATES = Path(review_app.BASE_DIR) / "templates"

LOG_OK = """
===== Scheduled run 2026-10-12 14:04:48 =====
--- gmail_fetcher.py
===== Result: OK -- 14.4 min =====
"""


class LastScheduledRun(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (review_app.SCHEDULED_LOG, review_app.SCHEDULE_FILE)
        review_app.SCHEDULED_LOG = Path(self.tmp.name) / "scheduled_runs.log"
        review_app.SCHEDULE_FILE = Path(self.tmp.name) / "schedule.json"     # absent -> the defaults

    def tearDown(self):
        review_app.SCHEDULED_LOG, review_app.SCHEDULE_FILE = self._saved
        self.tmp.cleanup()

    def _run(self, text, now):
        review_app.SCHEDULED_LOG.write_text(text, encoding="utf-8")
        return review_app.last_scheduled_run(now=now)

    def test_ok(self):
        r = self._run(LOG_OK, datetime(2026, 10, 12, 14, 30))
        self.assertEqual((r["result"], r["minutes"], r["alert"]), ("OK", 14.4, False))

    def test_running_takes_the_last_block(self):
        r = self._run(LOG_OK + "\n===== Scheduled run 2026-10-12 14:30:00 =====\n--- classify_emails.py --drive\n",
                      datetime(2026, 10, 12, 14, 35))
        self.assertTrue(r["running"])
        self.assertEqual(r["result"], "Running")
        self.assertFalse(r["alert"])

    def test_failed_alerts(self):
        r = self._run("===== Scheduled run 2026-10-12 14:00:00 =====\n===== Result: FAILED (exit 1) -- 0.5 min =====\n",
                      datetime(2026, 10, 12, 14, 10))
        self.assertTrue(r["alert"])

    @staticmethod
    def _log(start, minutes):
        return f"===== Scheduled run {start:%Y-%m-%d %H:%M:%S} =====\n===== Result: OK -- {minutes} min =====\n"

    def test_default_schedule_slots(self):
        s = review_app.load_schedule()
        mon = review_app.schedule_slots(datetime(2026, 10, 12).date(), s)            # a Monday
        self.assertEqual(len(mon), 20, "7:30, 9:00, 10:00-18:00 every 30 min, 20:00")
        self.assertEqual((mon[0].strftime("%H:%M"), mon[2].strftime("%H:%M"), mon[-1].strftime("%H:%M")),
                         ("07:30", "10:00", "20:00"))
        sat = review_app.schedule_slots(datetime(2026, 10, 17).date(), s)
        self.assertEqual([t.hour for t in sat], [9, 12, 15, 18, 21])

    def test_missed_working_hours_run_is_stale(self):
        r = self._run(LOG_OK, datetime(2026, 10, 12, 16, 0))                           # Mon 2 PM run, now 4 PM
        self.assertTrue(r["stale"])
        self.assertEqual(r["missed"].strftime("%H:%M"), "15:30")

    def test_quiet_morning_is_not_stale(self):
        log = self._log(datetime(2026, 10, 13, 9, 0), 1.0)                            # Tue 9:00 ran
        self.assertFalse(self._run(log, datetime(2026, 10, 13, 9, 55))["stale"])

    def test_weekend_gap_is_not_stale(self):
        log = self._log(datetime(2026, 10, 17, 9, 0), 1.0)                            # Sat 9:00 ran
        self.assertFalse(self._run(log, datetime(2026, 10, 17, 11, 50))["stale"])
        self.assertTrue(self._run(log, datetime(2026, 10, 17, 12, 30))["stale"], "the noon run was missed")

    def test_slot_skipped_by_a_long_run_is_not_missed(self):
        log = self._log(datetime(2026, 10, 12, 10, 0), 40.0)                          # ran 10:00-10:40
        self.assertFalse(self._run(log, datetime(2026, 10, 12, 10, 55))["stale"])     # 10:30 slot covered

    def test_nothing_all_weekend_is_stale_monday_morning(self):
        log = self._log(datetime(2026, 10, 16, 20, 0), 1.0)                           # last run Fri 8 PM
        self.assertTrue(self._run(log, datetime(2026, 10, 19, 7, 0))["stale"])

    def test_schedule_file_is_used(self):
        review_app.SCHEDULE_FILE.write_text('{"work_start": "09:00", "every_minutes": 60, "morning_runs": "08:00", '
                                            '"evening_runs": ""}', encoding="utf-8")
        s = review_app.load_schedule()
        self.assertEqual(s["morning_runs"], ["08:00"], "a one-item list written as a string")
        mon = review_app.schedule_slots(datetime(2026, 10, 12).date(), s)
        self.assertEqual([t.strftime("%H:%M") for t in mon][:3], ["08:00", "09:00", "10:00"])
        self.assertIn("every 60 minutes 9:00 AM–6:00 PM", review_app.schedule_description(s))

    def test_no_log(self):
        self.assertIsNone(review_app.last_scheduled_run())
        self.assertEqual(review_app.last_scheduled_run_tile()["count"], "—")


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class DashboardSections(unittest.TestCase):

    def _client(self, role):
        from starlette.testclient import TestClient
        fake = {"id": -616161, "username": "zz-dash", "display_name": "zz dash", "role": role,
                "active": True, "password_hash": "x"}
        self._saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id)
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        cl = TestClient(review_app.app)
        cl.post("/login", data={"username": "zz-dash", "password": "x"})
        return cl

    def tearDown(self):
        web_db.get_user, web_db.verify_password, web_db.get_user_by_id = self._saved

    def test_admin_sees_both_sections(self):
        html = self._client("admin").get("/dashboard").text
        self.assertIn(">Work<", html)
        self.assertIn("System health", html)
        self.assertIn("Last scheduled run", html)
        health = html[html.index("System health"):]
        for label in ("Failed emails", "Never classified", "Bills with no live copy", "Orphaned ledger rows"):
            self.assertIn(label, health)
        work = html[html.index(">Work<"):html.index("System health")]
        self.assertNotIn("Orphaned ledger rows", work)

    def test_reviewer_sees_only_work(self):
        cl = self._client("reviewer")
        html = cl.get("/dashboard").text
        self.assertIn("Review queue", html)
        self.assertNotIn("System health", html)
        self.assertNotIn("Orphaned ledger rows", html)
        self.assertIn("Admins only", cl.get("/scheduled-runs").text)

    def test_admin_log_page(self):
        r = self._client("admin").get("/scheduled-runs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Scheduled runs", r.text)


class IgnoreLabels(unittest.TestCase):
    """The old catch-all "Ignore" labels are gone from the pages; each meaning has its own word."""

    def _all(self):
        return {p.name: p.read_text(encoding="utf-8") for p in TEMPLATES.glob("*.html")}

    def test_old_labels_are_gone(self):
        for name, text in self._all().items():
            for old in (">Ignore<", ">ignore<", ">Ignore item<", "ignore this one<", "Duplicate — ignored<",
                        "'Ignore this vendor'", "'ignore vendor'", "Ignore (exclude from QB)",
                        "Ignore as duplicate", "('/ignored','Ignored')", "View ignored ("):
                self.assertNotIn(old, text, f"{old!r} still in {name}")

    def test_new_labels_are_used(self):
        t = self._all()
        self.assertIn(">Dismiss<", t["review_list.html"])
        self.assertIn("('/ignored','Dismissed')", t["base.html"])
        self.assertIn("mark duplicate", t["qb.html"])
        self.assertIn("don't enter", t["payments.html"])
        self.assertIn("Exclude this vendor", t["_macros.html"])
        self.assertIn("Drop order", t["sales_orders.html"])

    def test_behaviour_hooks_unchanged(self):
        """Only words changed: the JS class and form values the routes rely on are still there."""
        t = self._all()
        for name in ("qb.html", "payments.html"):
            self.assertIn("ignore-btn", t[name])
        self.assertIn('name="action" value="ignore"', t["duplicates.html"])
        self.assertTrue(re.search(r'action="/review/\{\{ it.id \}\}/ignore"', t["review_list.html"]))


if __name__ == "__main__":
    unittest.main()
