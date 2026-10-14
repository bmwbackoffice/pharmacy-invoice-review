"""Possible duplicates: copies linked by a duplicate group show in ONE set even when their numbers differ
(2026-10-14). The misread-number check flagged Lantev #93G4240 (Refuah scan, kept) with a forwarded copy read as
#9304240; sets were built by number only, so the misread copy sat alone in a one-record set that's never shown,
and the scan's set (scan + the vendor copy already ignored) looked settled. Keep this one couldn't reach it.

One real connection with commit/close as no-ops, rolled back in tearDown. Skipped when DATABASE_URL isn't set.
"""
import os
import unittest

import _base  # noqa: F401
import database as db
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
FN = "ZZTEST duplicate sets.eml"


class _Proxy:
    def __init__(self, c): self.__dict__["_c"] = c
    def cursor(self, *a, **k): return self._c.cursor(*a, **k)
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass
    def __setattr__(self, k, v): pass
    def __getattr__(self, n): return getattr(self._c, n)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class GroupLinkedCopiesShareASet(unittest.TestCase):
    def setUp(self):
        self._real_connect = db.connect
        self.real = self._real_connect()
        self.real.autocommit = False
        self.cur = self.real.cursor()
        db.connect = lambda: _Proxy(self.real)
        self.n = 0

        def inv(number, suspect=False, ignored=False, group=None):
            self.n += 1                                  # every copy from its own email, as in real life
            fn = f"{FN} {self.n}"
            self.cur.execute("INSERT INTO emails (filename, status) VALUES (%s, 'processed')", (fn,))
            self.cur.execute(
                "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, "
                "invoice_date, duplicate_suspect, duplicate_ignored, duplicate_group_id, skip_qb_import) "
                "VALUES (%s,'ZZTEST Dup Vendor','Refuah Pharmacy',%s,237.55,'2026-09-03',%s,%s,%s,FALSE) RETURNING id",
                (fn, number, suspect, ignored, group))
            return self.cur.fetchone()[0]
        self.scan = inv("ZZ93G4240", suspect=True, group="ZZGRP001")
        self.vendor_copy = inv("ZZ93G4240", ignored=True, group="ZZGRP001")
        self.misread = inv("ZZ9304240", suspect=True, group="ZZGRP001")
        self.other_a = inv("ZZ555", suspect=True, group="ZZGRP002")
        self.other_b = inv("ZZ555", group="ZZGRP002")

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        db.connect = self._real_connect

    def sets(self):
        mine = {self.scan, self.vendor_copy, self.misread, self.other_a, self.other_b}
        return [sorted(r["record_id"] for r in s["records"]) for s in web_db.list_duplicate_candidates()
                if any(r["record_type"] == "invoice" and r["record_id"] in mine for r in s["records"])]

    def test_misread_copy_joins_its_group_set(self):
        self.assertCountEqual(self.sets(), [sorted([self.scan, self.vendor_copy, self.misread]),
                                            sorted([self.other_a, self.other_b])])

    def test_keep_this_one_then_resolves_the_whole_group(self):
        web_db.keep_and_ignore_others("invoice", self.scan, [("invoice", self.misread)], "zz-test")
        self.cur.execute("SELECT id, duplicate_suspect, duplicate_ignored FROM invoices WHERE id = ANY(%s)",
                         ([self.scan, self.misread],))
        got = {i: (bool(s), bool(g)) for i, s, g in self.cur.fetchall()}
        self.assertEqual(got[self.scan], (False, False), "kept and no longer flagged")
        self.assertEqual(got[self.misread][1], True, "the misread copy is kept out")
        self.assertEqual(self.sets(), [sorted([self.other_a, self.other_b])], "the group's set is settled")


if __name__ == "__main__":
    unittest.main()
