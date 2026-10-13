"""Stopping and deleting automated investigations (2026-10-13). Investigation #15 sat 'running' for five
days after a restart, and because a running investigation disables every Investigate button, the
built-in clean-up (which only ran on Investigate) never ran."""
import os
import sys
import threading
import time
import unittest

import _base  # noqa: F401
import investigate
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class StopKillsTheProcess(unittest.TestCase):
    """The real runner (_run) with a harmless stand-in for claude: a process that waits forever."""

    def setUp(self):
        self._saved = (investigate._build_cmd, investigate.build_prompt, investigate._api_key,
                       web_db.finish_investigation, web_db.stop_investigation)
        self.finished, self.stopped = [], []
        investigate._build_cmd = lambda: [sys.executable, "-c", "import sys, time; sys.stdin.read(); time.sleep(120)"]
        investigate.build_prompt = lambda kind, subject: "test prompt"
        investigate._api_key = lambda: "test-key"
        web_db.finish_investigation = lambda *a, **k: self.finished.append((a, k))
        web_db.stop_investigation = lambda inv_id, user, detail="": self.stopped.append((inv_id, user, detail)) or True

    def tearDown(self):
        (investigate._build_cmd, investigate.build_prompt, investigate._api_key,
         web_db.finish_investigation, web_db.stop_investigation) = self._saved
        investigate._STOPPED.discard(-1)

    def test_stop_mid_run(self):
        t = threading.Thread(target=investigate._run, args=(-1, "flag", {}), daemon=True)
        t.start()
        for _ in range(100):                         # wait for the process to be registered
            if -1 in investigate._PROCS:
                break
            time.sleep(0.05)
        proc = investigate._PROCS[-1]
        ok, msg = investigate.stop(-1, "joel")
        t.join(timeout=20)
        self.assertFalse(t.is_alive(), "the worker finished once its process was killed")
        self.assertIsNotNone(proc.poll(), "the process is gone")
        self.assertTrue(ok)
        self.assertEqual(msg, "Investigation stopped.")
        self.assertEqual(self.stopped[0][:2], (-1, "joel"))
        self.assertEqual(self.finished, [], "the stopped record isn't overwritten by the worker")
        self.assertNotIn(-1, investigate._PROCS)

    def test_stop_without_a_process(self):
        ok, msg = investigate.stop(-1, "joel")
        self.assertTrue(ok)
        self.assertIn("already ended", msg)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class StopAndDeleteRecords(unittest.TestCase):
    """Database side, on one connection rolled back at the end."""

    def setUp(self):
        self.real = web_db.connect()
        self.real.autocommit = False      # web_db.connect() is AUTOCOMMIT -- without this nothing rolls back
        self.cur = self.real.cursor()
        real = self.real

        class P:
            autocommit = False
            def cursor(s, *a, **k): return real.cursor(*a, **k)
            def commit(s): pass
            def close(s): pass
            def rollback(s): pass
        self._saved = web_db.connect
        web_db.connect = lambda: P()
        self.cur.execute("SELECT coalesce(max(id), 0) FROM edit_log")
        self.log0 = self.cur.fetchone()[0]
        self.cur.execute("INSERT INTO change_requests (title, description, submitted_by) "
                         "VALUES ('zz test', 'zz', 'zz') RETURNING id")
        self.req = self.cur.fetchone()[0]

    def tearDown(self):
        web_db.connect = self._saved
        self.real.rollback()
        self.real.close()

    def _inv(self, status="running", age_min=0):
        self.cur.execute("INSERT INTO investigations (request_id, status, triggered_by, started_at) "
                         "VALUES (%s, %s, 'zz', now() - make_interval(mins => %s)) RETURNING id",
                         (self.req, status, age_min))
        return self.cur.fetchone()[0]

    def _row(self, inv_id):
        self.cur.execute("SELECT status, error FROM investigations WHERE id = %s", (inv_id,))
        return self.cur.fetchone()

    def test_stop_marks_it_and_a_late_finish_cannot_overwrite(self):
        i = self._inv()
        self.assertTrue(web_db.stop_investigation(i, "joel", "process ended"))
        self.assertEqual(self._row(i), ("failed", "Stopped by joel (process ended)"))
        web_db.finish_investigation(i, "completed", findings="late result")
        self.assertEqual(self._row(i)[0], "failed", "a run that finishes after being stopped is ignored")
        self.assertFalse(web_db.stop_investigation(i, "joel"), "not running any more")

    def test_delete(self):
        running, done = self._inv(), self._inv("completed")
        ok, msg = web_db.delete_investigation(running, "joel")
        self.assertFalse(ok)
        self.assertIn("stop it first", msg)
        ok, _ = web_db.delete_investigation(done, "joel")
        self.assertTrue(ok)
        self.assertIsNone(self._row(done))
        self.cur.execute("SELECT count(*) FROM change_requests WHERE id = %s", (self.req,))
        self.assertEqual(self.cur.fetchone()[0], 1, "the request itself stays")
        self.cur.execute("SELECT count(*) FROM edit_log WHERE id > %s AND table_name = 'investigations' "
                         "AND field = 'deleted'", (self.log0,))
        self.assertEqual(self.cur.fetchone()[0], 1)

    def test_page_load_clears_a_run_left_by_a_restart(self):
        stuck = self._inv(age_min=investigate.TIMEOUT_SEC * 2 // 60 + 5)
        fresh = self._inv()
        investigate.sweep_stale()
        self.assertEqual(self._row(stuck)[0], "failed")
        self.assertEqual(self._row(fresh)[0], "running", "a run inside its time limit is left alone")

    def test_buttons_on_the_requests_page(self):
        import review_app
        from starlette.testclient import TestClient
        self._inv()
        saved = review_app.current_user
        review_app.current_user = lambda r: {"id": -919191, "username": "zz", "role": "admin",
                                             "display_name": "zz", "active": True}
        try:
            html = TestClient(review_app.app).get("/requests").text
        finally:
            review_app.current_user = saved
        self.assertIn("Stop investigation", html)


if __name__ == "__main__":
    unittest.main()
