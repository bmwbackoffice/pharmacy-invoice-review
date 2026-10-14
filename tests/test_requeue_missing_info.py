"""requeue_missing_info.py (QB-10 follow-up, 2026-10-13): 17 payments waiting for QuickBooks entry had no
amount and/or date (NJNG, Herbal Health, JCP&L AutoPay, ...). Re-reading their emails under today's rules
either completes them or sends them to review. Payment #302 had been saved from a review split file
that's gone; it must be pointed at its original email, or the rerun adds a second copy next to it.

One real connection with commit/close as no-ops, rolled back in tearDown; the email folders are temporary
folders, so real emails are never found or moved. Skipped when DATABASE_URL isn't set.
"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import _base  # noqa: F401  (puts the project folder on sys.path)
import database as db
import requeue_by_vendor as rbv
import requeue_missing_info as rmi

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
A, B, C, D = (f"ZZTEST requeue {x}.eml" for x in "ABCD")
SPLIT = "ZZTEST requeue - split B__2.pdf"


class _Proxy:
    def __init__(self, c): self._c = c
    def cursor(self, *a, **k): return self._c.cursor(*a, **k)
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass
    def __getattr__(self, n): return getattr(self._c, n)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class RequeueMissingInfo(unittest.TestCase):
    def setUp(self):
        self._real_connect = db.connect
        self.real = self._real_connect()
        self.real.autocommit = False
        self.cur = self.real.cursor()
        db.connect = lambda: _Proxy(self.real)
        self.tmp = Path(tempfile.mkdtemp(prefix="zztest_requeue_"))
        self._dirs = (rbv.PROCESSED, rbv.REVIEW_PENDING, rbv.TEST_EMAILS)
        rbv.PROCESSED, rbv.REVIEW_PENDING, rbv.TEST_EMAILS = (
            self.tmp / "processed_emails", self.tmp / "review_pending", self.tmp / "test_emails")
        rbv.PROCESSED.mkdir()
        rbv.REVIEW_PENDING.mkdir()
        for fn in (A, B, C):                                   # D is deliberately not on disk
            (rbv.PROCESSED / fn).write_text("x")
        c = self.cur
        for fn, status, mid in ((A, "processed", "fffffffffff0000a"), (B, "review", "fffffffffff0000b"), (C, "processed", "fffffffffff0000c"),
                                (D, "processed", "fffffffffff0000d"), (SPLIT, "processed", None)):
            c.execute("INSERT INTO emails (filename, status, gmail_message_id) VALUES (%s,%s,%s)", (fn, status, mid))

        def pay(fn, ref, amount=None, date="", store="Maple Pharmacy", link=None, edited_by=None):
            c.execute("INSERT INTO payments (email_filename, vendor_matched, store_matched, reference_number, "
                      "payment_date, amount, gmail_link, edited_by) VALUES (%s,'ZZTEST Vendor',%s,%s,%s,%s,%s,%s) "
                      "RETURNING id", (fn, store, ref, date, amount, link, edited_by))
            return c.fetchone()[0]
        self.a1 = pay(A, "ZZ-A1")                                         # no amount, no date
        self.a2 = pay(A, "ZZ-A2", 10, "2026-10-01")                       # complete sibling
        self.b = pay(SPLIT, "ZZ-B", 20, "2026-10-01", store="unknown",
                     link="https://mail.google.com/mail/u/0/?authuser=x#all/fffffffffff0000b")
        self.c1 = pay(C, "ZZ-C1", 30)                                     # no date
        self.c2 = pay(C, "ZZ-C2", 40, "2026-10-01", edited_by="amy")      # a person edited it
        self.d = pay(D, "ZZ-D", None, "2026-10-01")                       # no amount; email not on disk

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        db.connect = self._real_connect
        rbv.PROCESSED, rbv.REVIEW_PENDING, rbv.TEST_EMAILS = self._dirs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self):
        with self.real.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
            return rmi.plan(cur)

    def test_plan(self):
        emails, not_found = self._plan()
        self.assertEqual([t["id"] for t in emails[A]["targets"]], [self.a1])
        self.assertEqual({r["id"] for r in emails[A]["records"]}, {self.a1, self.a2}, "siblings are re-read too")
        self.assertEqual(emails[A]["held"], [])
        self.assertIn(B, emails, "the split-file record is traced to its email by the Gmail id")
        self.assertEqual([r["id"] for r in emails[B]["repoint"]], [self.b])
        self.assertEqual(emails[C]["held"], [f"payment #{self.c2}: edited by amy"],
                         "a person's edit on any record of the email holds the whole email")
        self.assertIn(self.d, [r["id"] for r in not_found])
        self.assertNotIn(self.a2, [t["id"] for e in emails.values() for t in e["targets"]], "complete")

    def test_commit_requeues_and_repoints(self):
        emails, _ = self._plan()
        ready = {fn: e for fn, e in emails.items() if not e["held"]}
        self.assertEqual(set(ready), {A, B}, "real emails aren't in the temp folders, C is held")
        with self.real.cursor(cursor_factory=db.psycopg2.extras.RealDictCursor) as cur:
            moved, n_status = rmi.commit(cur, ready, "zztest")
        self.assertEqual((moved, n_status), (2, 2))
        self.assertTrue((rbv.TEST_EMAILS / A).exists() and (rbv.TEST_EMAILS / B).exists())
        self.assertTrue((rbv.PROCESSED / C).exists(), "held email isn't moved")
        c = self.cur
        c.execute("SELECT filename, status FROM emails WHERE filename = ANY(%s)", ([A, B, C],))
        self.assertEqual(dict(c.fetchall()), {A: "fetched", B: "fetched", C: "processed"})
        c.execute("SELECT email_filename FROM payments WHERE id = %s", (self.b,))
        self.assertEqual(c.fetchone()[0], B)
        c.execute("SELECT old_value, new_value FROM edit_log WHERE table_name = 'payments' AND record_id = %s "
                  "AND field = 'email_filename' AND changed_by = %s", (self.b, rmi.ACTOR))
        self.assertEqual(c.fetchall(), [(SPLIT, B)])
        c.execute("SELECT id FROM payments_requeue_backup_zztest ORDER BY id")
        self.assertEqual([r[0] for r in c.fetchall()], sorted([self.a1, self.a2, self.b]))
        # The rerun replaces by email filename, so the re-pointed record is replaced, not doubled.
        db.save_results([], {A, B})
        c.execute("SELECT count(*) FROM payments WHERE id = ANY(%s)", ([self.a1, self.a2, self.b],))
        self.assertEqual(c.fetchone()[0], 0)
        c.execute("SELECT count(*) FROM payments WHERE id = ANY(%s)", ([self.c1, self.c2],))
        self.assertEqual(c.fetchone()[0], 2)

    def test_dry_run_changes_nothing(self):
        import sys
        argv, sys.argv = sys.argv, ["requeue_missing_info.py"]
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                rmi.main()
        finally:
            sys.argv = argv
        self.assertIn("DRY RUN", out.getvalue())
        self.assertTrue(all((rbv.PROCESSED / fn).exists() for fn in (A, B, C)))
        self.cur.execute("SELECT status FROM emails WHERE filename = ANY(%s) ORDER BY filename", ([A, B],))
        self.assertEqual([r[0] for r in self.cur.fetchall()], ["processed", "review"])
        self.cur.execute("SELECT email_filename FROM payments WHERE id = %s", (self.b,))
        self.assertEqual(self.cur.fetchone()[0], SPLIT)


if __name__ == "__main__":
    unittest.main()
