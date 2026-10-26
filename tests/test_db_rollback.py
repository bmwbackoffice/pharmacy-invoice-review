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


class PaymentDuplicateSweep(RollbackTestCase):
    """resolve_payment_duplicates (2026-10-13): for payments keep the VENDOR's copy, and settle copies
    with no reference number when they name the same applied invoice."""

    def setUp(self):
        super().setUp()
        self._saved_smap = ce.load_sender_store_map
        ce.load_sender_store_map = lambda: {"zzscanner@example.com": "Refuah Pharmacy"}
        self._saved_std, ce.VENDOR_STD = ce.VENDOR_STD, {}
        ce._DEDUPE_VENDOR_CACHE.clear()
        self.n = 0

    def tearDown(self):
        ce.load_sender_store_map = self._saved_smap
        ce.VENDOR_STD = self._saved_std
        ce._DEDUPE_VENDOR_CACHE.clear()
        super().tearDown()

    def _pay(self, sender, ref="", inv="ZZ52164619", ignored=False, suspect=False, by=None, entered=False,
             amount=1372.62, vendor="Zz Garden Test"):
        self.n += 1
        fn = f"ZZTEST pay {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename, status, from_address) VALUES (%s,'processed',%s)", (fn, sender))
        self.cur.execute(
            "INSERT INTO payments (email_filename, vendor_matched, store_matched, reference_number, invoice_number, "
            "amount, payment_date, duplicate_suspect, duplicate_ignored, duplicate_ignored_by, entered_in_qb, "
            "skip_qb_import) VALUES (%s,%s,'Refuah Pharmacy',%s,%s,%s,'2026-10-07',%s,%s,%s,%s,FALSE) RETURNING id",
            (fn, vendor, ref, inv, amount, suspect or ignored, ignored, by, entered))
        return self.cur.fetchone()[0]

    def _state(self, *ids):
        self.cur.execute("SELECT id, duplicate_ignored, duplicate_suspect FROM payments WHERE id = ANY(%s) ORDER BY id",
                         (list(ids),))
        return {r[0]: (r[1], r[2]) for r in self.cur.fetchall()}

    def test_garden_of_life_case_keeps_the_vendor_copy(self):
        v = self._pay("ar@vendor.example")
        s1 = self._pay("zzscanner@example.com", suspect=True)
        s2 = self._pay("zzscanner@example.com", suspect=True)
        ce.resolve_payment_duplicates("test")
        st = self._state(v, s1, s2)
        self.assertEqual(st[v], (False, False), "vendor copy live and clean")
        self.assertEqual((st[s1][0], st[s2][0]), (True, True), "both scans ignored")

    def test_swaps_when_the_scan_arrived_first(self):
        s = self._pay("zzscanner@example.com")
        v = self._pay("ar@vendor.example", ignored=True)              # pipeline auto-ignored the vendor copy
        ce.resolve_payment_duplicates("test")
        st = self._state(s, v)
        self.assertEqual(st[v][0], False)
        self.assertEqual(st[s][0], True)

    def test_one_reference_for_different_invoices_is_left_alone(self):
        a = self._pay("ar@vendor.example", ref="FJGYX1XV1H", inv="ZZ20873185", amount=25.98)
        b = self._pay("ar@vendor.example", ref="FJGYX1XV1H", inv="ZZ21048489", amount=25.98)
        ce.resolve_payment_duplicates("test")
        self.assertEqual(self._state(a, b), {a: (False, False), b: (False, False)})

    def test_amount_and_date_alone_are_never_enough(self):
        a = self._pay("ar@vendor.example", inv="")
        b = self._pay("zzscanner@example.com", inv="", suspect=True)
        ce.resolve_payment_duplicates("test")
        self.assertEqual(self._state(a, b)[b], (False, True), "still a possible duplicate for a person")

    def test_entered_or_person_marked_copies_are_respected(self):
        v = self._pay("ar@vendor.example")
        s = self._pay("zzscanner@example.com", entered=True)
        ce.resolve_payment_duplicates("test")
        self.assertEqual(self._state(v, s)[s], (False, False), "a set touching QB entry is left to a person")
        v2 = self._pay("ar@vendor.example", inv="ZZ999", ignored=True, by="joel")   # a person ignored it
        s2 = self._pay("zzscanner@example.com", inv="ZZ999")
        ce.resolve_payment_duplicates("test")
        self.assertEqual(self._state(v2, s2), {v2: (True, True), s2: (False, False)})

    def test_dry_run_and_idempotent(self):
        v = self._pay("ar@vendor.example")
        s = self._pay("zzscanner@example.com", suspect=True)
        dry = ce.resolve_payment_duplicates("test", dry_run=True)
        self.assertTrue(any(r["keep_id"] == v for r in dry["resolved"]))
        self.assertEqual(self._state(s)[s], (False, True), "dry run wrote nothing")
        ce.resolve_payment_duplicates("test")
        again = ce.resolve_payment_duplicates("test")
        self.assertFalse(any(r["keep_id"] == v for r in again["resolved"]), "a settled set is left alone")


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


class MisreadNumberSweepVsEntered(RollbackTestCase):
    """DU-16, 2026-10-15 (Joel: yes): the misread-number check only compared copies NOT entered in QB, so a
    waiting copy whose twin was already entered sat on QB entry unflagged: 11 such bills, e.g. Maxi Health
    $5,230.84 #797203 waiting beside #797208 entered, and Masterko $1,459.40 #3551 beside #35551."""

    def setUp(self):
        super().setUp()
        self._saved_std, ce.VENDOR_STD = ce.VENDOR_STD, {}
        ce._DEDUPE_VENDOR_CACHE.clear()
        self.n = 0

    def tearDown(self):
        ce.VENDOR_STD = self._saved_std
        ce._DEDUPE_VENDOR_CACHE.clear()
        super().tearDown()

    def _inv(self, number, date, entered=False, gid=None, amount=5230.84, vendor="Zz Misread Vendor"):
        self.n += 1
        fn = f"ZZTEST misread {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename, status) VALUES (%s,'processed')", (fn,))
        self.cur.execute(
            "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, invoice_date, "
            "entered_in_qb, duplicate_suspect, duplicate_ignored, skip_qb_import, duplicate_group_id) "
            "VALUES (%s,%s,'Refuah Pharmacy',%s,%s,%s,%s,FALSE,FALSE,FALSE,%s) RETURNING id",
            (fn, vendor, number, amount, date, entered, gid))
        return self.cur.fetchone()[0]

    def _state(self, rid):
        self.cur.execute("SELECT duplicate_suspect, entered_in_qb, duplicate_group_id, duplicate_reason "
                         "FROM invoices WHERE id = %s", (rid,))
        return self.cur.fetchone()

    def _sweep(self):
        return [f for f in ce.flag_number_misread_duplicates("test")["flagged"]
                if f["vendor"] and f["vendor"].startswith("Zz Misread")]

    def test_waiting_copy_of_an_entered_bill_is_flagged(self):
        entered = self._inv("ZZ797208", "2026-08-10", entered=True)
        waiting = self._inv("ZZ797203", "2026-08-10")
        sets = self._sweep()
        self.assertEqual([sorted(f["ids"]) for f in sets], [sorted([entered, waiting])])
        self.assertEqual(sets[0]["entered_ids"], [entered])
        suspect, _, gid, reason = self._state(waiting)
        self.assertTrue(suspect)
        self.assertIn("already entered in QuickBooks", reason)
        self.assertIn("#ZZ797208", reason)
        e_suspect, e_entered, e_gid, _ = self._state(entered)
        self.assertEqual((e_suspect, e_entered, e_gid), (False, True, gid),
                         "the entered copy stays entered and unflagged; it only joins the set")
        self.assertEqual(self._sweep(), [], "a second run changes nothing")

    def test_misread_number_and_date(self):
        """Masterko #3730 dated 8/12 beside #33730 dated 8/13: one character apart, a day apart."""
        entered = self._inv("ZZ35551", "2026-10-07", entered=True, amount=1459.40)
        waiting = self._inv("ZZ3551", "2026-10-08", amount=1459.40)
        suffix_e = self._inv("ZZR204977", "2026-09-29", entered=True, amount=226.80)
        suffix_w = self._inv("ZZR204977-IN", "2026-09-30", amount=226.80)
        self.assertEqual(sorted(sorted(f["ids"]) for f in self._sweep()),
                         sorted([sorted([entered, waiting]), sorted([suffix_e, suffix_w])]))

    def test_separate_bills_are_left_alone(self):
        self._inv("ZZ7898", "2026-08-17", entered=True, amount=1760.00)       # weekly, same amount
        self._inv("ZZ7984", "2026-08-24", amount=1760.00)
        self._inv("ZZFI2614901", "2026-10-07", entered=True, amount=44.59)    # two days apart, other numbers
        self._inv("ZZFI24150750", "2026-10-09", amount=44.59)
        self._inv("ZZ1001", "2026-08-01", amount=99.00)                       # one character apart, a month apart
        self._inv("ZZ1002", "2026-09-01", amount=99.00)
        self.assertEqual(self._sweep(), [])

    def test_two_entered_copies_are_not_a_set(self):
        """Both already in QuickBooks: nothing for this check to stop (a person fixes QuickBooks)."""
        self._inv("ZZ3730", "2026-08-12", entered=True, amount=632.35)
        self._inv("ZZ33730", "2026-08-13", entered=True, amount=632.35)
        self.assertEqual(self._sweep(), [])

    def test_not_a_duplicate_decision_sticks(self):
        import web_db
        self._inv("ZZ797208", "2026-08-10", entered=True)
        waiting = self._inv("ZZ797203", "2026-08-10")
        self.assertEqual(len(self._sweep()), 1)
        saved = web_db.connect
        web_db.connect = db.connect
        try:
            web_db.clear_duplicate_flag("invoice", waiting, "test")      # the page's "not a duplicate"
        finally:
            web_db.connect = saved
        self.assertEqual(self._sweep(), [])
        self.assertFalse(self._state(waiting)[0])

    def test_blank_group_ids_count_as_none(self):
        """Copies whose group ids were all '' looked already reviewed and were never flagged."""
        a = self._inv("ZZ599722", "2026-09-10", gid="", amount=624.54)
        b = self._inv("ZZ9AG5445", "2026-09-10", gid="", amount=624.54)
        self.assertEqual([sorted(f["ids"]) for f in self._sweep()], [sorted([a, b])])
        self.assertTrue(self._state(a)[0] and self._state(b)[0], "two waiting copies are both flagged")

    def test_possible_duplicates_shows_the_entered_copy(self):
        import web_db
        entered = self._inv("ZZ797208", "2026-08-10", entered=True)
        waiting = self._inv("ZZ797203", "2026-08-10")
        self._sweep()
        saved = web_db.connect
        web_db.connect = db.connect
        try:
            sets = web_db.list_duplicate_candidates()
        finally:
            web_db.connect = saved
        mine = [s for s in sets if {r["record_id"] for r in s["records"]} & {entered, waiting}]
        self.assertEqual(len(mine), 1)
        self.assertEqual({r["record_id"] for r in mine[0]["records"]}, {entered, waiting})
        self.assertTrue(next(r for r in mine[0]["records"] if r["record_id"] == entered)["entered_in_qb"])


if __name__ == "__main__":
    unittest.main()
