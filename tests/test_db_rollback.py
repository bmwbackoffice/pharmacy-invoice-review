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

    def test_dismiss_during_run_via_real_ignore_review(self):
        """dedupe_review_queue (another chat, 2026-10-12) dismisses items through web_db.ignore_review
        DURING a classify run, before save_review_queue. The save must not put them back."""
        import web_db
        self._ins(1, "A"); self._ins(2, "B")
        rows = db.load_review_queue()
        saved = web_db.connect
        web_db.connect = db.connect                      # same rolled-back connection
        try:
            self.assertTrue(web_db.ignore_review(2, "auto-collapsed (review-queue dedupe backstop)"))
        finally:
            web_db.connect = saved
        db.save_review_queue(rows)                       # the run's start-of-run copy still has B
        self.cur.execute("SELECT filename FROM review_queue ORDER BY id")
        self.assertEqual([r[0] for r in self.cur.fetchall()], ["A"])

    def test_consumed_answer_is_not_resurrected(self):
        self._ins(3, "C", answer="x", by="amy")
        c = dict(db.load_review_queue()[0]); c["Answer"] = ""; c["Question (AI review reason)"] = "new"
        db.save_review_queue([c])
        self.cur.execute("SELECT answer, reviewed_by FROM review_queue WHERE filename='C'")
        self.assertEqual(self.cur.fetchone(), (None, None))


class SweepsUseStandardNames(RollbackTestCase):
    """DU-16 / DU-17 (switched 2026-10-13): copies spelled 'Maxi Health' and 'MaxiHealth' are one vendor.
    The standard-name lookup is stood in for by a key that maps both spellings together."""

    def setUp(self):
        super().setUp()
        self._saved_key = ce._dedupe_vendor_key
        real = self._saved_key
        ce._dedupe_vendor_key = lambda v: "zz maxihealth" if "maxi" in (v or "").lower() else real(v)

    def tearDown(self):
        ce._dedupe_vendor_key = self._saved_key
        super().tearDown()

    def test_misread_number_sweep_groups_spellings(self):
        ids = []
        for i, (vendor, num) in enumerate((("Zz Maxi Health", "ZZ803670"), ("Zz MaxiHealth", "ZZ803G70"))):
            fn = f"ZZTEST maxi {i}.eml"
            self.cur.execute("INSERT INTO emails (filename, status) VALUES (%s,'processed')", (fn,))
            self.cur.execute(
                "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, "
                "invoice_date, entered_in_qb, duplicate_suspect, duplicate_ignored, skip_qb_import) "
                "VALUES (%s,%s,'Maple Pharmacy',%s,6957.40,'2026-10-01',FALSE,FALSE,FALSE,FALSE) RETURNING id",
                (fn, vendor, num))
            ids.append(self.cur.fetchone()[0])
        res = ce.flag_number_misread_duplicates("test", dry_run=False)
        self.assertTrue(any(set(ids) <= set(f["ids"]) for f in res["flagged"]))
        self.cur.execute("SELECT bool_and(duplicate_suspect) FROM invoices WHERE id = ANY(%s)", (ids,))
        self.assertTrue(self.cur.fetchone()[0])

    def test_review_queue_collapse_groups_spellings(self):
        import web_db
        c = self.cur
        c.execute("CREATE TEMP TABLE review_queue (LIKE public.review_queue INCLUDING ALL)")
        c.execute("CREATE TEMP TABLE ignored_reviews (LIKE public.ignored_reviews INCLUDING ALL)")
        for i, vendor in ((1, "Zz Maxi Health"), (2, "Zz MaxiHealth")):
            c.execute("INSERT INTO review_queue (id, filename, source_email, vendor_matched, invoice_number, "
                      "amount, question) VALUES (%s,%s,%s,%s,'ZZ803670',6957.40,'q')",
                      (i, f"ZZ{i}.pdf", f"ZZTEST mail {i}.eml", vendor))
        saved = web_db.connect
        web_db.connect = db.connect
        try:
            res = ce.dedupe_review_queue("test", dry_run=False)
        finally:
            web_db.connect = saved
        self.assertEqual(len(res["dismissed"]), 1)
        c.execute("SELECT count(*) FROM review_queue")
        self.assertEqual(c.fetchone()[0], 1, "one copy kept, the other dismissed")


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
