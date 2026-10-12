"""Scheduled run (2026-10-12): one classify run at a time, and scheduled_run.py's result lines."""
import os
import tempfile
import unittest
from pathlib import Path

from _base import ce
import database as db
import scheduled_run

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class _Tripwire(Exception):
    pass


def _tripwire(*a, **k):
    raise _Tripwire("main() got past the run lock")


class RunLockInMain(unittest.TestCase):
    """classify_emails.main() must stop before any work when another run holds the lock."""

    def setUp(self):
        self._saved = (ce.Anthropic, ce.USE_DB, db.acquire_run_lock, os.environ.get("ANTHROPIC_API_KEY"))
        ce.Anthropic = _tripwire                     # the first thing main() does after the lock check
        ce.USE_DB = True
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test-not-a-real-key"

    def tearDown(self):
        ce.Anthropic, ce.USE_DB, db.acquire_run_lock, key = self._saved
        if key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = key

    def test_lock_held_elsewhere_skips_with_exit_3(self):
        db.acquire_run_lock = lambda: False
        with self.assertRaises(SystemExit) as cm:
            ce.main()
        self.assertEqual(cm.exception.code, 3)

    def test_lock_free_carries_on(self):
        db.acquire_run_lock = lambda: True
        with self.assertRaises(_Tripwire):
            ce.main()


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class AdvisoryLock(unittest.TestCase):
    """The real Postgres lock: a second session can't take it while it's held. Changes no data."""

    def test_second_session_is_refused_until_release(self):
        if not db.acquire_run_lock():
            self.skipTest("a real classify run holds the lock right now")
        try:
            other = db.connect()
            other.autocommit = True
            with other.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s)", (db.RUN_LOCK_KEY,))
                self.assertFalse(cur.fetchone()[0], "a second run must be refused")
                db.release_run_lock()
                cur.execute("SELECT pg_try_advisory_lock(%s)", (db.RUN_LOCK_KEY,))
                self.assertTrue(cur.fetchone()[0], "free again after release")
                cur.execute("SELECT pg_advisory_unlock(%s)", (db.RUN_LOCK_KEY,))
            other.close()
        finally:
            db.release_run_lock()


class ScheduledRunResults(unittest.TestCase):
    """scheduled_run.main() writes one block per run ending in a clear result line."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (scheduled_run.LOG_DIR, scheduled_run.LOG, scheduled_run._step)
        scheduled_run.LOG_DIR = Path(self.tmp.name)
        scheduled_run.LOG = Path(self.tmp.name) / "scheduled_runs.log"

    def tearDown(self):
        scheduled_run.LOG_DIR, scheduled_run.LOG, scheduled_run._step = self._saved
        self.tmp.cleanup()

    def _run(self, fetch_rc, classify_rc):
        codes = {"gmail_fetcher.py": fetch_rc, "classify_emails.py": classify_rc}
        scheduled_run._step = lambda log, script, args, timeout: codes[script]
        rc = scheduled_run.main()
        return rc, scheduled_run.LOG.read_text(encoding="utf-8").strip().splitlines()[-1]

    def test_results(self):
        self.assertEqual(self._run(0, 0)[0], 0)
        self.assertIn("Result: OK", self._run(0, 0)[1])
        rc, line = self._run(0, 3)
        self.assertEqual(rc, 0)
        self.assertIn("SKIPPED", line)
        rc, line = self._run(0, 1)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED (exit 1)", line)
        rc, line = self._run(None, 0)
        self.assertIn("TIMED OUT", line)
        rc, line = self._run(1, 0)                   # Gmail hiccup: classify still runs
        self.assertIn("Result: OK", line)

    def test_child_output_is_utf8(self):
        env = scheduled_run._env()
        self.assertEqual((env["PYTHONUTF8"], env["PYTHONIOENCODING"]), ("1", "utf-8"))
        self.assertTrue(env.get("STATEMENT_GOLIVE_DATE"))


if __name__ == "__main__":
    unittest.main()
