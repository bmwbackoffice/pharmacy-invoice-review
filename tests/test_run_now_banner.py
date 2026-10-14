"""Red banner when the automatic run fails or is missed, and the 'Check for new email now' button
(2026-10-13). The button's launcher is replaced by a recorder -- no real run is started."""
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import _base  # noqa: F401
import database
import review_app
import scheduled_run
import web_db
from starlette.testclient import TestClient


def _log(start, result="OK -- 1.0 min"):
    return f"===== Scheduled run {start:%Y-%m-%d %H:%M:%S} =====\n===== Result: {result} =====\n"


class _LogCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved_paths = (review_app.SCHEDULED_LOG, review_app.SCHEDULE_FILE, review_app.SCHEDULED_RUNS_DIR)
        review_app.SCHEDULED_LOG = Path(self.tmp.name) / "scheduled_runs.log"
        review_app.SCHEDULE_FILE = Path(self.tmp.name) / "schedule.json"
        review_app.SCHEDULED_RUNS_DIR = Path(self.tmp.name) / "runs"     # never the real runs going now
        review_app._RUN_ALERT_CACHE["at"] = 0.0

    def tearDown(self):
        review_app.SCHEDULED_LOG, review_app.SCHEDULE_FILE, review_app.SCHEDULED_RUNS_DIR = self._saved_paths
        review_app._RUN_ALERT_CACHE["at"] = 0.0
        self.tmp.cleanup()

    def write(self, text):
        review_app.SCHEDULED_LOG.write_text(text, encoding="utf-8")
        review_app._RUN_ALERT_CACHE["at"] = 0.0


class Banner(_LogCase):

    def test_failed_run(self):
        self.write(_log(datetime.now() - timedelta(minutes=5), "FAILED (exit 1) -- 0.5 min"))
        self.assertIn("failed", review_app.run_alert())

    def test_missed_run(self):
        self.write(_log(datetime.now() - timedelta(days=2)))
        self.assertIn("didn't run at", review_app.run_alert())

    def test_ok_run_has_no_banner(self):
        self.write(_log(datetime.now() - timedelta(minutes=5)))
        self.assertIsNone(review_app.run_alert())

    def test_banner_on_a_page_for_everyone(self):
        self.write(_log(datetime.now() - timedelta(minutes=5), "TIMED OUT -- 90.0 min"))
        saved = review_app.current_user
        review_app.current_user = lambda r: {"id": -939393, "username": "zz", "role": "reviewer",
                                             "display_name": "zz", "active": True}
        try:
            html = TestClient(review_app.app).get("/scheduled-runs").text
        finally:
            review_app.current_user = saved
        self.assertIn('id="run-alert"', html)
        self.assertIn("timed out", html)


class RunNow(_LogCase):

    def setUp(self):
        super().setUp()
        self.started = []
        self._saved = (subprocess.Popen, database.run_lock_held, review_app.current_user)
        subprocess.Popen = lambda args, **kw: self.started.append(args)
        database.run_lock_held = lambda: False
        review_app.current_user = lambda r: {"id": -949494, "username": "amy", "role": "reviewer",
                                             "display_name": "amy", "active": True}
        self.cl = TestClient(review_app.app)

    def tearDown(self):
        subprocess.Popen, database.run_lock_held, review_app.current_user = self._saved
        super().tearDown()

    def _press(self):
        r = self.cl.post("/scheduled-runs/run-now", headers={"referer": "http://testserver/dashboard"},
                         follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        return r.headers["location"]

    def test_starts_the_classifier_only(self):
        loc = self._press()
        self.assertEqual(len(self.started), 1)
        args = self.started[0]
        self.assertTrue(args[1].endswith("scheduled_run.py"))
        self.assertEqual(args[2:], ["--classify-only", "--by", "amy"], "no Gmail fetch, just the classifier")
        self.assertIn("Applying%20changes%20now", loc)

    def test_refused_while_a_run_holds_the_lock(self):
        database.run_lock_held = lambda: True
        self.assertIn("already%20going", self._press())
        self.assertEqual(self.started, [])

    def test_refused_right_after_another_start(self):
        self.write(_log(datetime.now() - timedelta(minutes=1)))
        self.assertIn("try%20again", self._press())
        self.assertEqual(self.started, [])

    def test_refused_while_the_log_shows_a_run_going(self):
        self.write(f"===== Scheduled run {datetime.now() - timedelta(minutes=10):%Y-%m-%d %H:%M:%S} =====\n")
        self.assertIn("already%20going", self._press())
        self.assertEqual(self.started, [])


class ReadOnlyUsersCannotStartIt(unittest.TestCase):

    def test_viewer_is_refused(self):
        fake = {"id": -959595, "username": "zz-viewer", "display_name": "zz", "role": "viewer",
                "active": True, "password_hash": "x"}
        saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id, subprocess.Popen)
        started = []
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        subprocess.Popen = lambda args, **kw: started.append(args)
        try:
            cl = TestClient(review_app.app)
            cl.post("/login", data={"username": "zz-viewer", "password": "x"})
            r = cl.post("/scheduled-runs/run-now", follow_redirects=False)
            self.assertEqual(r.status_code, 403)
            self.assertEqual(started, [])
        finally:
            web_db.get_user, web_db.verify_password, web_db.get_user_by_id, subprocess.Popen = saved


class LogNote(unittest.TestCase):

    def _main(self, argv):
        tmp = tempfile.TemporaryDirectory()
        saved = (scheduled_run.LOG_DIR, scheduled_run.LOG, scheduled_run._step, sys.argv)
        steps = []
        scheduled_run.LOG_DIR = Path(tmp.name)
        scheduled_run.LOG = Path(tmp.name) / "scheduled_runs.log"
        scheduled_run._step = lambda log, script, args, timeout: steps.append(script) or 0
        sys.argv = ["scheduled_run.py"] + argv
        try:
            scheduled_run.main()
            return scheduled_run.LOG.read_text(encoding="utf-8"), steps
        finally:
            scheduled_run.LOG_DIR, scheduled_run.LOG, scheduled_run._step, sys.argv = saved
            tmp.cleanup()

    def test_classifier_only_by_hand(self):
        text, steps = self._main(["--classify-only", "--by", "amy"])
        self.assertEqual(steps, ["classify_emails.py"], "the Gmail fetch is skipped")
        self.assertIn("(classifier only, started by hand by amy from the web app)", text)
        self.assertIn("Result: OK", text)

    def test_scheduled_run_still_fetches(self):
        text, steps = self._main([])
        self.assertEqual(steps, ["gmail_fetcher.py", "classify_emails.py"])
        self.assertNotIn("by hand", text)


if __name__ == "__main__":
    unittest.main()
