"""Joel's 10/26 follow-ups: "In QB twice?" also catches the same bill entered with two different amounts (Vita Vocal
H002875 $819.00 and 002875 $81,900.00), a pair the app marked entered twice shows once, on its QuickBooks pair, and
"Bills with no live copy" compares bill numbers the way the duplicate checks do (Reisman's R205029-IN / R205029)."""
import os
import unittest

import _base  # noqa: F401
import database as db
import review_app
import web_db
from _base import ce
from test_db_rollback import RollbackTestCase

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
STORE = "Zz Test Store"


class QbTwiceAmountDiffers(RollbackTestCase):

    def setUp(self):
        super().setUp()
        self._saved = (web_db.connect, ce.VENDOR_STD)
        web_db.connect = db.connect                      # the rolled-back connection
        ce.VENDOR_STD = {"lookup": {}, "zz": True}       # non-empty: no reload
        ce._DEDUPE_VENDOR_CACHE.clear()
        self.cur.execute("INSERT INTO qb_import_batches (filename, uploaded_by) VALUES ('zz.xlsx', 'zz') RETURNING id")
        self.batch = self.cur.fetchone()[0]

    def tearDown(self):
        web_db.connect, ce.VENDOR_STD = self._saved
        ce._DEDUPE_VENDOR_CACHE.clear()
        super().tearDown()

    def _qb(self, number, day, amount, name="Zz Vita Vocal Test"):
        self.cur.execute("INSERT INTO qb_register (batch_id, qb_store, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, "
                         "qb_amount) VALUES (%s, %s, 'Bill', %s, %s, %s, %s, %s)",
                         (self.batch, STORE, day, number, name, name, amount))

    def _pairs(self):
        return [p for p in web_db.qb_double_entries("all") if p["a"]["qb_store"] == STORE]

    def test_decimal_slip_a_month_apart_is_flagged(self):
        self._qb("H002875", "2026-08-02", 819.00)
        self._qb("002875", "2026-09-02", 81900.00)
        pairs = self._pairs()
        self.assertEqual([p["kind"] for p in pairs], ["amount_differs"])
        self.assertIn("100 times", pairs[0]["note"])

    def test_same_number_other_amount_is_flagged_but_not_a_recurring_bill(self):
        self._qb("ZZ803670", "2026-10-07", 6957.40, "Zz Maxi Test")
        self._qb("ZZ803670", "2026-10-07", 6682.27, "Zz Maxi Test")
        self._qb("ZZACCT77", "2026-01-01", 120.50, "Zz Power Test")      # a monthly bill under the account number
        self._qb("ZZACCT77", "2026-02-01", 131.10, "Zz Power Test")
        pairs = self._pairs()
        self.assertEqual([(p["a"]["qb_name"], p["kind"]) for p in pairs], [("Zz Maxi Test", "amount_differs")])
        self.assertIn("Same bill #", pairs[0]["note"])

    def test_next_bill_isnt_flagged_but_a_same_day_typo_is(self):
        self._qb("ZZ-50536", "2026-04-14", 143.82, "Zz Jams Test")       # the vendor's next bill, a look-alike digit on
        self._qb("ZZ-50538", "2026-04-16", 236.31, "Zz Jams Test")
        self._qb("ZZ50603", "2026-07-20", 164.23, "Zz Jams Test")        # one digit apart, but 9 days apart
        self._qb("ZZ50608", "2026-07-29", 164.43, "Zz Jams Test")
        self._qb("ZZ92H0348", "2026-09-02", 1071.27, "Zz Lantev Test")   # misread number, one digit off, same day
        self._qb("ZZ92H03408", "2026-09-02", 1071.57, "Zz Lantev Test")
        pairs = self._pairs()
        self.assertEqual([(p["a"]["qb_name"], p["kind"]) for p in pairs], [("Zz Lantev Test", "amount_differs")])
        self.assertIn("one digit", pairs[0]["note"])

    def test_app_pair_shows_once_on_its_quickbooks_pair(self):
        """Maxi Health #797203 / #797208 had two boxes: the QuickBooks pair and 'marked entered twice in the app'."""
        for n, number in enumerate(("ZZ797203", "ZZ797208")):
            fn = f"ZZTEST twice {n}.eml"
            self.cur.execute("INSERT INTO emails (filename) VALUES (%s)", (fn,))
            self.cur.execute(
                "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, invoice_date, "
                "entered_in_qb, entered_qb_by, entered_qb_at, duplicate_suspect, duplicate_ignored, skip_qb_import, "
                "duplicate_group_id) VALUES (%s, 'Zz Twice Vendor', %s, %s, 523.08, '2026-08-10', TRUE, 'zz', now(), "
                "TRUE, FALSE, FALSE, 'ZZTWICE1')", (fn, STORE, number))
            self._qb(number, "2026-08-10", 523.08, "Zz Twice Vendor")
        pairs = self._pairs()
        self.assertEqual([p["kind"] for p in pairs], ["misread"], "one box, not a second 'marked entered twice' one")
        self.assertTrue(pairs[0]["a"].get("record_id") and pairs[0]["b"].get("record_id"))
        self.assertIn("marked entered in the app", pairs[0]["note"])


class NoLiveCopyNumbers(RollbackTestCase):

    def setUp(self):
        super().setUp()
        self._saved = (web_db.connect, web_db._vendor_tables)
        web_db.connect = db.connect
        web_db._vendor_tables = lambda: {}              # vendor keys from the names as written
        self.n = 0

    def tearDown(self):
        web_db.connect, web_db._vendor_tables = self._saved
        super().tearDown()

    def _inv(self, number, amount, ignored):
        self.n += 1
        fn = f"ZZTEST nolive {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename) VALUES (%s)", (fn,))
        self.cur.execute(
            "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, invoice_date, "
            "entered_in_qb, duplicate_suspect, duplicate_ignored, skip_qb_import) "
            "VALUES (%s, 'Zz Reisman Test Bakery', 'Madison Pharmacy', %s, %s, '2026-10-01', FALSE, %s, %s, FALSE)",
            (fn, number, amount, ignored, ignored))

    def _numbers(self):
        return sorted(g["number"] for g in web_db.no_live_copy_groups() if g["vendor_key"] == "zz reisman test bakery")

    def test_copy_kept_under_a_tagged_number_is_the_live_copy(self):
        self._inv("ZZR205029-IN", 56.70, True)
        self._inv("ZZR205029", 56.70, False)
        self.assertEqual(self._numbers(), [])

    def test_punctuation_doesnt_split_a_bill(self):
        self._inv("ZZ-555-01", 10.00, True)
        self._inv("ZZ55501", 12.00, False)              # a revised copy, kept
        self.assertEqual(self._numbers(), [])

    def test_a_bill_with_no_live_copy_is_still_listed(self):
        self._inv("ZZ777001", 99.99, True)
        self._inv("ZZ777009", 50.00, False)             # a near number, but another bill
        self.assertEqual(self._numbers(), ["ZZ777001"])


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class DoublesPage(unittest.TestCase):

    def test_amount_differs_box(self):
        from starlette.testclient import TestClient
        import datetime
        fake = {"id": -424242, "username": "zz-fixes", "display_name": "Zz Fixes", "role": "admin",
                "active": True, "password_hash": "x"}
        saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id, web_db.qb_double_entries)
        bill = {"qb_store": "Refuah Pharmacy", "qb_type": "Bill", "qb_name": "Zz Vita Vocal Test"}
        pair = {"key": "zz", "kind": "amount_differs", "strength": 2, "dismissed": None, "credit": False,
                "note": "One amount is 100 times the other: a misplaced decimal point?",
                "a": dict(bill, qb_num="H002875", qb_date=datetime.date(2026, 8, 2), qb_amount=819.00),
                "b": dict(bill, qb_num="002875", qb_date=datetime.date(2026, 9, 2), qb_amount=81900.00)}
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        web_db.qb_double_entries = lambda show="open": [dict(pair)]
        try:
            cl = TestClient(review_app.app)
            cl.post("/login", data={"username": "zz-fixes", "password": "x"})
            html = cl.get("/qb-reconcile/doubles").text
        finally:
            web_db.get_user, web_db.verify_password, web_db.get_user_by_id, web_db.qb_double_entries = saved
        self.assertIn("Amount differs (1)", html)
        self.assertIn("Same store · same bill, different amounts", html)
        self.assertIn("100 times the other", html)
        box = html[html.index('<div class="dbl"'):]
        self.assertIn('<td class="diff">$81,900.00', box, "the amounts are marked as what differs")


if __name__ == "__main__":
    unittest.main()
