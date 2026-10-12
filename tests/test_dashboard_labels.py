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
        self._saved = review_app.SCHEDULED_LOG
        review_app.SCHEDULED_LOG = Path(self.tmp.name) / "scheduled_runs.log"

    def tearDown(self):
        review_app.SCHEDULED_LOG = self._saved
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

    def test_stale_only_inside_the_schedule_window(self):
        self.assertTrue(self._run(LOG_OK, datetime(2026, 10, 12, 16, 0))["stale"])     # 2 h, mid-day
        self.assertFalse(self._run(LOG_OK, datetime(2026, 10, 12, 23, 0))["stale"])    # after 9 PM

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
