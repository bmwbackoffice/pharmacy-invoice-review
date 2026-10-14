"""Scheduled runs page, one collapsed block per run (2026-10-13). On 10/13 an "Apply changes now" run was
still going when the 5 PM scheduled run started; both wrote into logs/scheduled_runs.log at once, so their
lines got mixed, and the running one then wrote over the 5 PM block. Now each run writes its own file in
logs/runs/ and the finished block goes into the shared log in one piece. The page splits the log into
runs, including old overlapped blocks."""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import _base  # noqa: F401
import review_app
import scheduled_run
from starlette.testclient import TestClient

T = "%Y-%m-%d %H:%M:%S"


def block(start, lines, result="OK -- 1.0 min"):
    end = [f"===== Result: {result} ====="] if result else []
    return "\n".join([f"===== Scheduled run {start:{T}} ====="] + lines + end) + "\n"


class _Paths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self._saved = (review_app.SCHEDULED_LOG, review_app.SCHEDULED_RUNS_DIR, review_app.SCHEDULE_FILE)
        review_app.SCHEDULED_LOG = d / "scheduled_runs.log"
        review_app.SCHEDULED_RUNS_DIR = d / "runs"
        review_app.SCHEDULE_FILE = d / "schedule.json"
        review_app.SCHEDULED_RUNS_DIR.mkdir()

    def tearDown(self):
        review_app.SCHEDULED_LOG, review_app.SCHEDULED_RUNS_DIR, review_app.SCHEDULE_FILE = self._saved
        self.tmp.cleanup()


class SplitIntoRuns(_Paths):

    def test_overlapped_old_blocks_are_untangled(self):
        a, b = datetime(2026, 10, 13, 16, 57, 3), datetime(2026, 10, 13, 17, 0)
        text = ("\n" + f"===== Scheduled run {a:{T}} =====\n(classifier only, started by hand by joel from the web app)\n"
                "Reprocessing one.pdf with your answer...\n"
                f"\n===== Scheduled run {b:{T}} =====\n--- gmail_fetcher.py\nFetched 1 message(s) into x.\n"
                "Another classify run is in progress (scheduled or manual) - skipping this one.\n"
                "===== Result: SKIPPED (another run was in progress) -- 0.1 min =====\n"
                "Reprocessing two.pdf with your answer...\n===== Result: OK -- 20.0 min =====\n")
        runs = review_app.parse_run_log(text)
        self.assertEqual([(r["started"], r["result"]) for r in runs],
                         [(a, "OK"), (b, "SKIPPED (another run was in progress)")])
        self.assertIn("Reprocessing two.pdf with your answer...", runs[0]["lines"], "back to the run it interrupted")
        self.assertNotIn("Reprocessing two.pdf with your answer...", runs[1]["lines"])

    def test_runs_still_going_come_from_their_own_files(self):
        old = datetime.now() - timedelta(minutes=40)
        new = datetime.now() - timedelta(minutes=5)
        review_app.SCHEDULED_LOG.write_text(block(old, ["--- classify_emails.py --drive"]), encoding="utf-8")
        (review_app.SCHEDULED_RUNS_DIR / "x.log").write_text(block(new, ["--- gmail_fetcher.py"], None), encoding="utf-8")
        runs = review_app.load_runs()
        self.assertEqual([r["started"].replace(microsecond=0) for r in runs],
                         [old.replace(microsecond=0), new.replace(microsecond=0)])
        last = review_app.last_scheduled_run()
        self.assertTrue(last["running"])
        self.assertEqual(last["result"], "Running")

    def test_finished_copy_wins_when_a_run_is_in_both_places(self):
        s = datetime.now() - timedelta(minutes=5)
        review_app.SCHEDULED_LOG.write_text(block(s, ["--- x"]), encoding="utf-8")
        (review_app.SCHEDULED_RUNS_DIR / "x.log").write_text(block(s, ["--- x"], None), encoding="utf-8")
        runs = review_app.load_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["result"], "OK")

    def test_skipped_run_isnt_the_last_run(self):
        busy = datetime.now() - timedelta(minutes=12)
        review_app.SCHEDULED_LOG.write_text(
            block(busy, ["(classifier only, started by hand by amy from the web app)"], None)
            + block(busy + timedelta(minutes=3), ["Another classify run is in progress"],
                    "SKIPPED (another run was in progress) -- 0.1 min"), encoding="utf-8")
        last = review_app.last_scheduled_run()
        self.assertEqual(last["started"], busy.replace(microsecond=0))
        self.assertTrue(last["running"], "the run that's actually working, not the one that skipped")

    def test_no_result_for_hours_is_stopped_not_running(self):
        review_app.SCHEDULED_LOG.write_text(block(datetime.now() - timedelta(hours=5), ["--- x"], None),
                                            encoding="utf-8")
        last = review_app.last_scheduled_run()
        self.assertFalse(last["running"])
        self.assertTrue(last["result"].startswith("STOPPED"))


class OneLinePerRun(unittest.TestCase):

    def view(self, lines, result="OK", minutes=2.0, started=None):
        return review_app.run_view({"started": started or datetime(2026, 10, 13, 16, 30), "result": result,
                                    "minutes": minutes, "lines": lines})

    def test_busy_run_summary_and_colours(self):
        v = self.view(["", "--- gmail_fetcher.py", "Mailbox: ap@x.com", "Fetched 4 message(s) into test_emails.",
                       "--- classify_emails.py --drive", "Reprocessing a.pdf with your answer...",
                       "Processing b.eml... (1/1)", "  cache: 1 written, 0 read", "  saved 2 invoice file(s)",
                       "  saved 3 flagged record(s) to review_pending/", "Saved 37 record row(s) to PostgreSQL.",
                       "Review queue: 135 open item(s) (17 new this run).", "Total emails processed: 5",
                       "Traceback (most recent call last):", "QB-ENTERED WARNING: check it"])
        self.assertEqual(v["status"], "ok")
        self.assertEqual(v["who"], "Scheduled")
        self.assertEqual(v["summary"], "4 new email(s) fetched, 1 answered item(s) redone, 5 email(s) processed, "
                                       "37 record(s) saved, 17 new for review, 1 error line(s)")
        styles = dict((text, cls) for cls, text in v["lines"])
        self.assertEqual(styles["--- gmail_fetcher.py"], "step")
        self.assertEqual(styles["Mailbox: ap@x.com"], "dim")
        self.assertEqual(styles["Fetched 4 message(s) into test_emails."], "good")
        self.assertEqual(styles["Reprocessing a.pdf with your answer..."], "email")
        self.assertEqual(styles["  cache: 1 written, 0 read"], "dim")
        self.assertEqual(styles["  saved 2 invoice file(s)"], "good")
        self.assertEqual(styles["  saved 3 flagged record(s) to review_pending/"], "review")
        self.assertEqual(styles["Traceback (most recent call last):"], "err")
        self.assertEqual(styles["QB-ENTERED WARNING: check it"], "warn")
        self.assertFalse(v["quiet"])

    def test_quiet_skipped_failed_running_and_by_hand(self):
        quiet = self.view(["--- gmail_fetcher.py", "No new messages matched.", "Saved 0 record row(s)",
                           "Review queue: 111 open item(s) (0 new this run).", "Total emails processed: 0"])
        self.assertEqual((quiet["summary"], quiet["quiet"]), ("nothing new", True))
        skipped = self.view(["Another classify run is in progress"], "SKIPPED (another run was in progress)")
        self.assertEqual((skipped["status"], skipped["label"]), ("skipped", "Skipped"))
        self.assertIn("another run was going", skipped["summary"])
        failed = self.view(["boom"], "FAILED (exit 1)")
        self.assertEqual((failed["status"], failed["label"]), ("bad", "Failed"))
        running = self.view(["(classifier only, started by hand by joel from the web app)"], None, None,
                            started=datetime.now() - timedelta(minutes=3))
        self.assertEqual((running["status"], running["label"], running["who"]),
                         ("running", "Running", "Apply changes now · joel"))


class WriterKeepsBlocksWhole(unittest.TestCase):
    """scheduled_run.main(): a run's lines go to its own file and land in the shared log in one piece."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._saved = (scheduled_run.LOG_DIR, scheduled_run.LOG, scheduled_run._step, sys.argv)
        scheduled_run.LOG_DIR = Path(self.tmp.name)
        scheduled_run.LOG = Path(self.tmp.name) / "scheduled_runs.log"

    def tearDown(self):
        scheduled_run.LOG_DIR, scheduled_run.LOG, scheduled_run._step, sys.argv = self._saved
        self.tmp.cleanup()

    def test_overlapping_runs_stay_separate(self):
        def step(log, script, args, timeout):
            log.write(f"outer {script} line 1\n")
            log.flush()
            if script == "classify_emails.py":           # a scheduled run starts while this one is going
                inner_argv, sys.argv = sys.argv, ["scheduled_run.py"]
                scheduled_run._step = lambda lg, s, a, t: (lg.write(f"inner {s}\n"), 3)[1]
                try:
                    scheduled_run.main()
                finally:
                    scheduled_run._step, sys.argv = step, inner_argv
            log.write(f"outer {script} line 2\n")
            return 0
        scheduled_run._step = step
        sys.argv = ["scheduled_run.py", "--classify-only", "--by", "amy"]
        self.assertEqual(scheduled_run.main(), 0)
        text = scheduled_run.LOG.read_text(encoding="utf-8")
        runs = review_app.parse_run_log(text)
        self.assertEqual([r["result"] for r in runs], ["SKIPPED (another run was in progress)", "OK"],
                         "the inner run finished first and its block went in first")
        self.assertEqual([l for l in runs[1]["lines"] if l.startswith(("outer", "inner"))],
                         ["outer classify_emails.py line 1", "outer classify_emails.py line 2"])
        self.assertEqual([l for l in runs[0]["lines"] if l.startswith(("outer", "inner"))],
                         ["inner gmail_fetcher.py", "inner classify_emails.py"])
        self.assertEqual(list((scheduled_run.LOG_DIR / "runs").glob("*.log")), [], "run files are cleaned up")

    def test_cut_off_run_is_folded_in_as_stopped(self):
        runs = scheduled_run.LOG_DIR / "runs"
        runs.mkdir()
        old, fresh = runs / "old.log", runs / "fresh.log"
        old.write_text(block(datetime.now() - timedelta(hours=5), ["--- classify_emails.py --drive"], None),
                       encoding="utf-8")
        fresh.write_text(block(datetime.now() - timedelta(minutes=5), ["--- x"], None), encoding="utf-8")
        t = time.time() - 4 * 3600
        os.utime(old, (t, t))
        scheduled_run._step = lambda log, script, args, timeout: 0
        sys.argv = ["scheduled_run.py"]
        scheduled_run.main()
        text = scheduled_run.LOG.read_text(encoding="utf-8")
        self.assertIn("Result: STOPPED (cut off before it finished)", text)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists(), "a run that may still be going is left alone")


class Page(_Paths):

    def test_page_shows_collapsed_runs(self):
        now = datetime.now()
        review_app.SCHEDULED_LOG.write_text(
            block(now - timedelta(minutes=30), ["--- gmail_fetcher.py", "Fetched 2 message(s) into x.",
                                                "Total emails processed: 2", "Saved 3 record row(s) to PostgreSQL."])
            + block(now - timedelta(minutes=5), ["Another classify run is in progress"],
                    "SKIPPED (another run was in progress) -- 0.1 min"), encoding="utf-8")
        saved = review_app.current_user
        review_app.current_user = lambda r: {"id": -424242, "username": "zz-test", "role": "reviewer",
                                             "display_name": "zz", "active": True}
        try:
            html = TestClient(review_app.app).get("/scheduled-runs").text
        finally:
            review_app.current_user = saved
        self.assertEqual(html.count('<details class="run'), 2)
        self.assertNotIn("<details class=\"run run-ok\" open", html, "collapsed until clicked")
        self.assertIn("2 new email(s) fetched, 2 email(s) processed, 3 record(s) saved", html)
        self.assertIn('<span class="l-good">Fetched 2 message(s) into x.</span>', html)
        self.assertLess(html.index("run-skipped"), html.index('details class="run run-ok'), "newest first")


if __name__ == "__main__":
    unittest.main()
