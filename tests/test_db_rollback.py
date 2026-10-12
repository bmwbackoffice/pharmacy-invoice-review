"""Database-backed regression tests. Everything runs on ONE connection whose commit/close are no-ops
and is rolled back at the end, so live data is never changed (each test checks that). Skipped when
DATABASE_URL isn't set.
"""
import json
import os
import unittest

from _base import ce, ledger, inv
import database as db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class _Proxy:
    def __init__(self, c): self._c = c
    def cursor(self, *a, **k): return self._c.cursor(*a, **k)
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass
    def __getattr__(self, n): return getattr(self._c, n)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class RollbackTestCase(unittest.TestCase):
    def setUp(self):
        self._real_connect = db.connect
        self.real = self._real_connect()
        self.cur = self.real.cursor()
        db.connect = lambda: _Proxy(self.real)
        self._saved_use_db, ce.USE_DB = ce.USE_DB, True

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        db.connect = self._real_connect
        ce.USE_DB = self._saved_use_db
        db._QUEUE_AS_LOADED = None


class ReviewQueueMerge(RollbackTestCase):
    """LC-01: a classify run used to overwrite web work done while it ran."""

    def setUp(self):
        super().setUp()
        c = self.cur
        c.execute("CREATE TEMP TABLE review_queue (LIKE public.review_queue INCLUDING ALL)")
        c.execute("CREATE TEMP SEQUENCE rq_seq OWNED BY review_queue.id")
        c.execute("ALTER TABLE review_queue ALTER COLUMN id SET DEFAULT nextval('rq_seq')")
        c.execute("SELECT setval('rq_seq', 1000)")
        c.execute("CREATE TEMP TABLE ignored_reviews (LIKE public.ignored_reviews INCLUDING ALL)")

    def _ins(self, i, fn, answer=None, by=None, assigned=None):
        self.cur.execute(
            "INSERT INTO review_queue (id, filename, source_email, answer, reviewed_by, reviewed_at, "
            "assigned_to, question) VALUES (%s,%s,%s,%s,%s,CASE WHEN %s IS NULL THEN NULL ELSE now() END,%s,'q')",
            (i, fn, fn + ".eml", answer, by, by, assigned))

    def test_web_changes_during_a_run_survive(self):
        self._ins(1, "A"); self._ins(2, "B"); self._ins(3, "C", answer="x", by="amy"); self._ins(4, "D")
        rows = {r["Id"]: r for r in db.load_review_queue()}
        # web activity while the run works
        self.cur.execute("UPDATE review_queue SET answer='yes', reviewed_by='bob', reviewed_at=now() WHERE id=1")
        self.cur.execute("UPDATE review_queue SET assigned_to='carol' WHERE id=2")
        self.cur.execute("SELECT row_to_json(r) FROM review_queue r WHERE id=4")
        self.cur.execute("INSERT INTO ignored_reviews (snapshot, filename, ignored_by) VALUES (%s,'D','eve')",
                         (json.dumps(self.cur.fetchone()[0]),))
        self.cur.execute("DELETE FROM review_queue WHERE id=4")
        self._ins(5, "E")                                            # restored during the run
        db.save_review_queue([rows[1], rows[2], rows[3], rows[4], {"Filename": "N"}])
        self.cur.execute("SELECT filename, id, answer, reviewed_by, assigned_to FROM review_queue")
        got = {r[0]: r for r in self.cur.fetchall()}
        self.assertEqual(got["A"][2:4], ("yes", "bob"))
        self.assertEqual(got["B"][4], "carol")
        self.assertEqual(got["C"][2:4], ("x", "amy"), "who answered is kept")
        self.assertNotIn("D", got, "ignored during the run stays ignored")
        self.assertEqual(got["E"][1], 5, "restored during the run is kept")
        self.assertIn("N", got)

    def test_consumed_answer_is_not_resurrected(self):
        self._ins(3, "C", answer="x", by="amy")
        c = dict(db.load_review_queue()[0]); c["Answer"] = ""; c["Question (AI review reason)"] = "new"
        db.save_review_queue([c])
        self.cur.execute("SELECT answer, reviewed_by FROM review_queue WHERE filename='C'")
        self.assertEqual(self.cur.fetchone(), (None, None))


class StoreConflictDB(RollbackTestCase):
    """DU-03: a copy naming another store used to replace the earlier one silently (Lantev #9GH5739)."""

    def setUp(self):
        super().setUp()
        self._saved_std, ce.VENDOR_STD = ce.VENDOR_STD, {}
        ce._DEDUPE_VENDOR_CACHE.clear()

    def tearDown(self):
        ce.VENDOR_STD = self._saved_std
        ce._DEDUPE_VENDOR_CACHE.clear()
        super().tearDown()

    def _setup(self, tag, entered=False):
        old_fn, new_fn = f"ZZTEST old {tag}.eml", f"ZZTEST new {tag}.eml"
        for fn in (old_fn, new_fn):
            self.cur.execute("INSERT INTO emails (filename, status) VALUES (%s,'processed')", (fn,))
        self.cur.execute(
            "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, "
            "entered_in_qb, duplicate_suspect, duplicate_ignored) VALUES (%s,'Zz Test Vendor','Refuah Pharmacy',"
            "%s,100.00,%s,FALSE,FALSE) RETURNING id", (old_fn, "ZZ" + tag, entered))
        pid = self.cur.fetchone()[0]
        hist = [ledger("Zz Test Vendor", "ZZ" + tag, 100.0, old_fn, store="Refuah Pharmacy")]
        new = inv("Zz Test Vendor", "ZZ" + tag, 100.0, store="3M Pharmacy")
        res = [{"filename": new_fn, "classification": {"invoices": [new], "payments": []}}]
        return pid, hist, new, res

    def _prior(self, pid):
        self.cur.execute("SELECT duplicate_suspect, duplicate_ignored FROM invoices WHERE id=%s", (pid,))
        return self.cur.fetchone()

    def test_no_answer_marks_both_copies(self):
        pid, hist, new, res = self._setup("A")
        ce.apply_duplicate_checks(res, hist, [])
        self.assertEqual(self._prior(pid), (True, False))
        self.assertTrue(new["is_duplicate_suspect"])
        self.assertFalse(new["duplicate_ignored"])

    def test_reviewer_answer_still_supersedes(self):
        pid, hist, new, res = self._setup("B")
        res[0]["classification"]["_reviewer_answered"] = True
        ce.apply_duplicate_checks(res, hist, [])
        self.assertEqual(self._prior(pid)[1], True)
        self.assertFalse(new["is_duplicate_suspect"])

    def test_entered_prior_is_never_touched(self):
        pid, hist, new, res = self._setup("C", entered=True)
        ce.apply_duplicate_checks(res, hist, [])
        self.assertEqual(self._prior(pid), (False, False))
        self.assertIn("already entered in QuickBooks", new["duplicate_reason"])


if __name__ == "__main__":
    unittest.main()
