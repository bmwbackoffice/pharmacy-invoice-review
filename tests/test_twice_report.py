"""Joel's 10/26 asks for "In QB twice?": a store filter, the app saying which bill to keep when it has the bill
("two stores, same bill ... the system should say which one should be the correct one"), and a report-back of what
QuickBooks kept that the app follows (store, bill #, date, amount; its copies of the deleted bill set aside)."""
import os
import unittest

import psycopg2.extras

import _base  # noqa: F401
import database as db
import review_app
import web_db
from _base import ce
from test_db_rollback import RollbackTestCase

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
STORE, STORE2 = "Zz Test Store", "Zz Other Store"


class _Twice(RollbackTestCase):

    def setUp(self):
        super().setUp()
        self._saved = (web_db.connect, ce.VENDOR_STD)
        web_db.connect = db.connect                      # the rolled-back connection
        ce.VENDOR_STD = {"lookup": {}, "zz": True}       # non-empty: no reload
        ce._DEDUPE_VENDOR_CACHE.clear()
        self.dcur = self.real.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        self.cur.execute("INSERT INTO qb_import_batches (filename, uploaded_by) VALUES ('zz.xlsx', 'zz') RETURNING id")
        self.batch = self.cur.fetchone()[0]
        self.n = 0

    def tearDown(self):
        web_db.connect, ce.VENDOR_STD = self._saved
        ce._DEDUPE_VENDOR_CACHE.clear()
        super().tearDown()

    def _qb(self, number, amount, day="2026-09-03", store=STORE, name="Zz Twice Vendor"):
        self.cur.execute("INSERT INTO qb_register (batch_id, qb_store, qb_type, qb_date, qb_num, qb_name, qb_std_vendor, "
                         "qb_amount) VALUES (%s, %s, 'Bill', %s, %s, %s, %s, %s)",
                         (self.batch, store, day, number, name, name, amount))

    def _inv(self, number, amount, day="2026-09-03", store=STORE, entered=True, excluded=False):
        """An invoice with its duplicate-ledger row, like a classify run saves it."""
        self.n += 1
        fn = f"ZZTEST twice report {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename) VALUES (%s)", (fn,))
        self.cur.execute(
            "INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, invoice_date, "
            "entered_in_qb, entered_qb_by, entered_qb_at, duplicate_suspect, duplicate_ignored, skip_qb_import) "
            "VALUES (%s, 'Zz Twice Vendor', %s, %s, %s, %s, %s, 'zz', now(), FALSE, FALSE, %s) RETURNING id",
            (fn, store, number, amount, day, entered, excluded))
        rid = self.cur.fetchone()[0]
        self.cur.execute("INSERT INTO invoice_history (vendor_matched, store_matched, invoice_number, invoice_date, "
                         "invoice_amount, source_file, source_email_id) VALUES ('Zz Twice Vendor', %s, %s, %s, %s, %s, %s)",
                         (store, number, day, amount, fn, fn))
        return rid

    def _pair(self):
        pairs = [p for p in web_db.qb_double_entries("all") if STORE in (p["a"]["qb_store"], p["b"]["qb_store"])]
        self.assertEqual(len(pairs), 1, [(p["kind"], p["a"]["qb_num"], p["b"]["qb_num"]) for p in pairs])
        return web_db.qb_double_attach(pairs)[0]

    def _side(self, p, number):
        return "a" if p["a"]["qb_num"] == number else "b"

    def _row(self, rid):
        self.dcur.execute("SELECT * FROM invoices WHERE id = %s", (rid,))
        return self.dcur.fetchone()

    def _ledger(self, rid):
        self.dcur.execute("SELECT h.* FROM invoice_history h JOIN invoices i ON h.source_file = i.email_filename "
                          "WHERE i.id = %s", (rid,))
        return self.dcur.fetchone()

    def _resolved(self, key):
        self.dcur.execute("SELECT * FROM qb_double_dismissed WHERE pair_key = %s", (key,))
        return self.dcur.fetchone()


class TwiceSuggestAndReport(_Twice):

    def test_two_stores_the_app_says_which_and_follows_quickbooks(self):
        """Clyor #3524 $791.64 was in Refuah's and Maple's QuickBooks; the app has it under Refuah."""
        self._qb("ZZ3524", 791.64, store=STORE)
        self._qb("ZZ3524", 791.64, store=STORE2)
        rid = self._inv("ZZ3524", 791.64, store=STORE)
        p = self._pair()
        self.assertEqual(p["kind"], "two_stores")
        here = "a" if p["a"]["qb_store"] == STORE else "b"
        there = "b" if here == "a" else "a"
        self.assertEqual(p["suggest"], here)
        self.assertEqual(p["advice"], f"The app's record matches the one in {STORE}: keep it and delete the one in {STORE2}.")
        self.assertEqual(p["options"][0]["label"], f"Kept the one in {STORE}, deleted the one in {STORE2} ✓ as the app has it")
        res = web_db.qb_double_report(p["key"], f"kept_{there}", "zz-tester", "the bill is theirs")
        self.assertEqual(res["changes"], [f"moved rec {rid} to {STORE2}"])
        self.assertEqual(self._row(rid)["store_matched"], STORE2)
        self.assertEqual(self._ledger(rid)["store_matched"], STORE2, "the duplicate ledger follows")
        done = self._resolved(p["key"])
        self.assertEqual(done["dismissed_by"], "zz-tester")
        self.assertTrue(done["note"].startswith(f"Kept the one in {STORE2}, deleted the one in {STORE}: the bill is theirs"))
        self.assertIn(f"moved rec {rid} to {STORE2}", done.get("app_changes") or done["note"])
        self.dcur.execute("SELECT changed_by FROM edit_log WHERE table_name = 'invoices' AND record_id = %s "
                          "AND field = 'store_matched'", (rid,))
        self.assertTrue(self.dcur.fetchone()["changed_by"].startswith("zz-tester via In QB twice?"))

    def test_an_excluded_vendors_bill_still_says_which_store(self):
        """Parmed #7489163739 $111.89 in Refuah's and 3M's QuickBooks: Parmed's bills aren't entered from the app,
        but the app's copy, under 3M, still tells which one is right."""
        self._qb("ZZ7489163739", 111.89, store=STORE2)
        self._qb("ZZ7489163739", 111.89, store=STORE)
        rid = self._inv("ZZ7489163739", 111.89, store=STORE, entered=False, excluded=True)
        p = self._pair()
        self.assertEqual(p["suggest"], "a" if p["a"]["qb_store"] == STORE else "b")
        self.assertEqual([(r["record_id"], r["live"], r["excluded"]) for r in p["records"]], [(rid, True, True)])

    def test_misread_kept_number_reaches_the_record_and_its_ledger_row(self):
        """Vitality Plus #262515 / #26515: the app has #262515; QuickBooks kept #26515."""
        self._qb("ZZ262515", 270.00)
        self._qb("ZZ26515", 270.00)
        rid = self._inv("ZZ262515", 270.00)
        p = self._pair()
        self.assertEqual(p["kind"], "misread")
        self.assertEqual(p["suggest"], self._side(p, "ZZ262515"))
        kept = self._side(p, "ZZ26515")
        label = next(o["label"] for o in p["options"] if o["value"] == f"kept_{kept}")
        self.assertEqual(label, f"Kept #ZZ26515, deleted #ZZ262515 (the app: changes rec {rid} to #ZZ26515)")
        web_db.qb_double_report(p["key"], f"kept_{kept}", "zz-tester")
        self.assertEqual(self._row(rid)["invoice_number"], "ZZ26515")
        self.assertEqual(self._ledger(rid)["invoice_number"], "ZZ26515")
        self.assertFalse(self._row(rid)["duplicate_ignored"], "no duplicate re-check against its own ledger row")

    def test_the_app_has_both_copies_the_deleted_ones_is_set_aside(self):
        """Maxi Health #797203 / #797208: both copies live and marked entered in the app."""
        self._qb("ZZ797203", 5230.84)
        self._qb("ZZ797208", 5230.84)
        r3, r8 = self._inv("ZZ797203", 5230.84), self._inv("ZZ797208", 5230.84)
        p = self._pair()
        self.assertIsNone(p["suggest"])
        self.assertEqual(p["advice"], "The app has both: check which one is right.")
        kept = self._side(p, "ZZ797203")
        label = next(o["label"] for o in p["options"] if o["value"] == f"kept_{kept}")
        self.assertEqual(label, f"Kept #ZZ797203, deleted #ZZ797208 (the app: sets aside rec {r8})")
        self.dcur.execute("UPDATE invoices SET qb_twice = 'Entered twice in QuickBooks' WHERE id IN (%s, %s)", (r3, r8))
        res = web_db.qb_double_report(p["key"], f"kept_{kept}", "zz-tester")
        self.assertEqual(res["changes"], [f"set aside rec {r8}"])
        self.assertTrue(self._row(r8)["duplicate_ignored"])
        self.assertFalse(self._row(r3)["duplicate_ignored"])
        self.assertEqual(self._row(r3)["invoice_number"], "ZZ797203")
        self.assertIsNone(self._row(r3)["qb_twice"], "the Possible duplicates warning goes too")

    def test_a_split_record_fixed_in_quickbooks_changes_nothing(self):
        """Lantev Refuah: QuickBooks has 9304241 $718.08 and 93G4241 $718.00; the bill is #93G4241 $718.08."""
        self._qb("ZZ9304241", 718.08)
        self._qb("ZZ93G4241", 718.00)
        rid = self._inv("ZZ93G4241", 718.08)
        p = self._pair()
        self.assertEqual(p["kind"], "amount_differs")
        s = p["suggest"]
        self.assertEqual((p[s]["qb_num"], p["fix"]), ("ZZ93G4241", ["amount"]))
        self.assertEqual(p["advice"], "The app's record matches #ZZ93G4241 but has $718.08: keep #ZZ93G4241, "
                                      "change its amount to $718.08, and delete #ZZ9304241.")
        self.assertEqual(p["options"][0], {"value": f"kept_{s}_fixed", "answer": "Kept #ZZ93G4241, changed its amount "
                                           "to $718.08, deleted #ZZ9304241",
                                           "label": "Kept #ZZ93G4241, changed its amount to $718.08, deleted "
                                                    "#ZZ9304241 ✓ as the app has it"})
        res = web_db.qb_double_report(p["key"], f"kept_{s}_fixed", "zz-tester")
        self.assertEqual(res["changes"], [])
        self.assertAlmostEqual(float(self._row(rid)["amount"]), 718.08)

    def test_a_kept_bill_as_it_is_gives_the_record_its_amount(self):
        self._qb("ZZ9304241", 718.08)
        self._qb("ZZ93G4241", 718.00)
        rid = self._inv("ZZ93G4241", 718.08)
        p = self._pair()
        res = web_db.qb_double_report(p["key"], f"kept_{p['suggest']}", "zz-tester")
        self.assertEqual(res["changes"], [f"changed rec {rid} to $718.00"])
        self.assertAlmostEqual(float(self._row(rid)["amount"]), 718.00)

    def test_a_kept_bill_with_no_number_leaves_the_apps_number(self):
        """Monsey View $320.00: QuickBooks has #1602275 and a no-number copy of 9/10; the vendor's #1600728 of 9/3
        is another bill, not this one."""
        self._qb("ZZ1602275", 320.00, day="2026-09-10")
        self._qb(None, 320.00, day="2026-09-10")
        rid = self._inv("ZZ1602275", 320.00, day="2026-09-10")
        week_before = self._inv("ZZ1600728", 320.00, day="2026-09-03")
        p = self._pair()
        self.assertEqual(p["kind"], "no_number")
        self.assertEqual([r["record_id"] for r in p["records"]], [rid], f"rec {week_before} isn't this bill")
        res = web_db.qb_double_report(p["key"], f"kept_{self._side(p, None)}", "zz-tester")
        self.assertEqual(res["changes"], [])
        self.assertEqual(self._row(rid)["invoice_number"], "ZZ1602275")
        self.assertEqual(self._row(week_before)["invoice_number"], "ZZ1600728")

    def test_an_open_qb_correction_for_the_kept_value_closes(self):
        """Someone had asked for QuickBooks to show #26515 (QB corrections); QuickBooks kept that one."""
        self._qb("ZZ262515", 270.00)
        self._qb("ZZ26515", 270.00)
        rid = self._inv("ZZ262515", 270.00)
        self.cur.execute("INSERT INTO qb_corrections (record_type, record_id, field, old_value, new_value, store, vendor, "
                         "number, changed_by) VALUES ('invoice', %s, 'number', 'ZZ262515', 'ZZ26515', %s, "
                         "'Zz Twice Vendor', 'ZZ262515', 'zz') RETURNING id", (rid, STORE))
        cid = self.cur.fetchone()[0]
        p = self._pair()
        web_db.qb_double_report(p["key"], f"kept_{self._side(p, 'ZZ26515')}", "zz-tester")
        self.dcur.execute("SELECT done_at, done_by FROM qb_corrections WHERE id = %s", (cid,))
        row = self.dcur.fetchone()
        self.assertIsNotNone(row["done_at"])
        self.assertEqual(row["done_by"], "zz-tester")

    def test_not_in_the_app_only_the_answer_is_saved_and_only_once(self):
        self._qb("ZZ5550001", 99.10)
        self._qb("ZZ5550001", 99.10)
        p = self._pair()
        self.assertEqual(p["advice"], "Not in the app.")
        self.assertEqual([o["value"] for o in p["options"]], ["kept_a", "two_real", "corrected", "other"])
        res = web_db.qb_double_report(p["key"], "kept_a", "zz-tester", "deleted the second one")
        self.assertEqual((res["changes"], res["already"]), ([], None))
        self.assertEqual(self._resolved(p["key"])["note"], "Deleted the extra one in QuickBooks: deleted the second one")
        again = web_db.qb_double_report(p["key"], "two_real", "zz-someone-else")
        self.assertTrue(again["already"])
        self.assertEqual(self._resolved(p["key"])["dismissed_by"], "zz-tester")

    def test_a_choice_that_doesnt_fit_the_pair_is_refused(self):
        self._qb("ZZ5550001", 99.10)
        self._qb("ZZ5550001", 99.10)
        p = self._pair()
        with self.assertRaises(ValueError):
            web_db.qb_double_report(p["key"], "kept_b_fixed", "zz-tester")
        with self.assertRaises(ValueError):
            web_db.qb_double_report(p["key"], "drop table", "zz-tester")
        self.assertIsNone(self._resolved(p["key"]))


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class TwicePage(unittest.TestCase):
    """The page: a store filter (a bill in two stores' QuickBooks under both) and the report-back form."""

    def _pair(self, kind, store_a, store_b, num_a, num_b, suggest=None):
        bill = {"qb_type": "Bill", "qb_name": "Zz Twice Vendor", "qb_date": None, "qb_amount": 10.00}
        return {"key": f"zz|{kind}|{num_a}|{store_a}|{store_b}", "kind": kind, "strength": 2, "dismissed": None,
                "credit": False, "note": None, "a": dict(bill, qb_store=store_a, qb_num=num_a),
                "b": dict(bill, qb_store=store_b, qb_num=num_b), "records": [], "suggest": suggest, "fix": [],
                "advice": "Zz advice " + num_a,
                "options": [{"value": "kept_a", "answer": f"Kept #{num_a}", "label": f"Kept #{num_a} zz-label"},
                            {"value": "two_real", "answer": "Not a duplicate: two real bills",
                             "label": "Not a duplicate: two real bills"}]}

    def _client(self):
        from starlette.testclient import TestClient
        fake = {"id": -424242, "username": "zz-twice", "display_name": "Zz Twice", "role": "admin",
                "active": True, "password_hash": "x"}
        names = ("get_user", "verify_password", "get_user_by_id", "qb_double_entries", "qb_double_attach",
                 "get_user_prefs", "set_user_pref", "qb_double_report")
        saved = {n: getattr(web_db, n) for n in names}
        self.addCleanup(lambda: [setattr(web_db, n, f) for n, f in saved.items()])
        self.prefs, self.reports = [], []
        pairs = [self._pair("two_stores", STORE, STORE2, "ZZ1", "ZZ1", suggest="a"),
                 self._pair("misread", STORE, STORE, "ZZ22", "ZZ2"),
                 self._pair("misread", STORE2, STORE2, "ZZ33", "ZZ3")]
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        web_db.qb_double_entries = lambda show="open": [dict(p) for p in pairs]
        web_db.qb_double_attach = lambda ps: ps
        web_db.get_user_prefs = lambda uid: {"twice_store": STORE2}
        web_db.set_user_pref = lambda uid, k, v: self.prefs.append((uid, k, v))
        web_db.qb_double_report = lambda *a: (self.reports.append(a) or
                                              {"answer": "Kept #ZZ22", "changes": ["set aside rec 1"], "found": True,
                                               "already": None})
        cl = TestClient(review_app.app)
        cl.post("/login", data={"username": "zz-twice", "password": "x"})
        return cl

    def test_store_filter_puts_a_two_store_pair_under_both(self):
        cl = self._client()
        html = cl.get("/qb-reconcile/doubles", params={"store": STORE}).text
        self.assertEqual(self.prefs, [(-424242, "twice_store", STORE)], "the choice is remembered")
        self.assertIn("Zz advice ZZ1", html)
        self.assertIn("Zz advice ZZ22", html)
        self.assertNotIn("Zz advice ZZ33", html)
        self.assertIn("All stores (3)", html)
        self.assertIn(f"{STORE} (2)", html)
        self.assertIn(f"{STORE2} (2)", html)
        self.assertIn("✓ keep", html)
        self.assertIn('<option value="kept_a">Kept #ZZ22 zz-label</option>', html)

    def test_no_store_in_the_url_uses_the_remembered_one(self):
        cl = self._client()
        html = cl.get("/qb-reconcile/doubles").text
        self.assertEqual(self.prefs, [])
        self.assertIn("Zz advice ZZ33", html)
        self.assertNotIn("Zz advice ZZ22", html)
        html = cl.get("/qb-reconcile/doubles", params={"store": ""}).text
        self.assertEqual(self.prefs, [(-424242, "twice_store", "")])
        self.assertIn("Zz advice ZZ22", html)
        self.assertIn("Zz advice ZZ33", html)

    def test_report_back_saves_and_says_what_the_app_changed(self):
        cl = self._client()
        r = cl.post("/qb-reconcile/doubles/dismiss", data={"pair_key": "zz|k", "outcome": "kept_a", "note": "n"},
                    headers={"referer": "http://testserver/qb-reconcile/doubles?store=x&flash=old"},
                    follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.reports, [("zz|k", "kept_a", "zz-twice", "n")])
        loc = r.headers["location"]
        self.assertNotIn("flash=old", loc)
        self.assertIn("store=x", loc)
        self.assertIn("Saved%3A%20Kept%20%23ZZ22.%20The%20app%20set%20aside%20rec%201.", loc)


if __name__ == "__main__":
    unittest.main()
