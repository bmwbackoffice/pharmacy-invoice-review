"""Daily report (2026-10-26, Joel: how many items are processed each day, and the breakdown). The numbers come from
web_db.daily_report; each test adds sample rows inside a rolled-back transaction and checks what they add."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone

import _base  # noqa: F401
import database as db
import review_app
import web_db
from test_db_rollback import RollbackTestCase

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class Counts(RollbackTestCase):

    def setUp(self):
        super().setUp()
        self._saved_connect = web_db.connect
        web_db.connect = db.connect                      # the rolled-back connection

    def tearDown(self):
        web_db.connect = self._saved_connect
        super().tearDown()

    def _today(self):
        rep = web_db.daily_report(7)
        self.assertEqual(rep["rows"][0]["day"], date.today(), "newest day first")
        return rep["rows"][0], next(s for s in rep["stores"] if s["store"] == "Refuah Pharmacy")

    def _email_row(self, fn):
        """The emails row a record needs (foreign key). No source, so it isn't counted as an email received."""
        self.cur.execute("INSERT INTO emails (filename) VALUES (%s)", (fn,))

    def _bill(self, n, amount, **cols):
        fn = f"ZZTEST report {n}.eml"
        self._email_row(fn)
        cols = {"skip_qb_import": False, "duplicate_ignored": False, "entered_in_qb": False, **cols}
        names = ", ".join(cols)
        self.cur.execute(
            f"INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, date_added, "
            f"{names}) VALUES (%s, 'Zz Report Vendor', 'Refuah Pharmacy', %s, %s, CURRENT_DATE, "
            f"{', '.join(['%s'] * len(cols))})", (fn, f"ZZ{n}", amount, *cols.values()))

    def test_each_column_counts_what_the_page_says(self):
        day0, store0 = self._today()
        c = self.cur
        for i, etype in enumerate(("invoice", "invoice", "payment", "spam")):
            c.execute("INSERT INTO emails (filename, source, email_type, status, received_at) "
                      "VALUES (%s, 'gmail', %s, 'processed', now())", (f"ZZTEST report mail {i}.eml", etype))
        # A review document re-read under its own name gets an emails row with no source: not an email.
        c.execute("INSERT INTO emails (filename, needs_response) VALUES ('ZZTEST report split.pdf', FALSE)")
        self._bill(1, 100.00, entered_in_qb=True, entered_qb_by="zz-person", entered_qb_at=datetime.now(timezone.utc))
        self._bill(2, 50.00, skip_qb_import=True)                                  # excluded vendor: a bill, not for QB
        self._bill(3, 100.00, duplicate_ignored=True)                              # a copy set aside when saved
        self._bill(4, 70.00, entered_in_qb=True, entered_qb_by="joel via QB reconcile import",
                   entered_qb_at=datetime.now(timezone.utc))                                          # an automatic mark: not "entered"
        for fn in ("ZZTEST report p1.eml", "ZZTEST report p2.eml"):
            self._email_row(fn)
        c.execute("INSERT INTO payments (email_filename, vendor_matched, amount, date_added, duplicate_ignored) "
                  "VALUES ('ZZTEST report p1.eml', 'Zz Report Vendor', 10, CURRENT_DATE, FALSE), "
                  "('ZZTEST report p2.eml', 'Zz Report Vendor', 10, CURRENT_DATE, TRUE)")
        c.execute("INSERT INTO runs (run_id, started_at, n_review) VALUES ('ZZTEST-report-run', now(), 3)")
        day1, store1 = self._today()

        def added(key):
            return day1[key] - day0[key]

        self.assertEqual(added("emails"), 4, "the no-source row isn't an email")
        self.assertEqual((added("invoice"), added("payment"), added("other")), (2, 1, 1))
        self.assertEqual((added("bills"), added("for_qb")), (3, 2), "the set-aside copy isn't a bill")
        self.assertAlmostEqual(added("bills_amt"), 220.00)
        self.assertEqual(added("payments"), 1)
        self.assertEqual(added("review"), 3)
        self.assertEqual(added("entered"), 1, "a QuickBooks-upload match isn't someone entering it")
        self.assertAlmostEqual(added("entered_amt"), 100.00)
        self.assertEqual(added("dups"), 2, "the set-aside bill and the payment copy, on their save day")
        self.assertEqual(store1["bills"] - store0["bills"], 3)
        self.assertEqual(store1["entered"] - store0["entered"], 1)

    def test_period_and_totals(self):
        rep = web_db.daily_report(30)
        self.assertEqual(len(rep["rows"]), 30)
        self.assertEqual(rep["start"], rep["rows"][-1]["day"])
        self.assertEqual(rep["totals"]["emails"], sum(r["emails"] for r in rep["rows"]))
        self.assertEqual(rep["totals"]["emails"],
                         sum(rep["totals"][k] for k in web_db.REPORT_EMAIL_TYPES) + rep["totals"]["other"],
                         "every email lands in exactly one type column")
        self.assertEqual(len(web_db.daily_report("nonsense")["rows"]), 14)
        self.assertEqual(len(web_db.daily_report(10000)["rows"]), 120, "capped")


class Chart(unittest.TestCase):
    def _rows(self, counts):
        rows = []
        for i, n in enumerate(counts):
            r = {"day": date(2026, 10, 26) - timedelta(days=i), "emails": n, "other": 0}
            r.update({k: 0 for k, _l, _c in review_app.REPORT_SERIES})
            r["invoice"], r["payment"] = n - n // 4, n // 4
            rows.append(r)
        return rows

    def test_geometry(self):
        ch = review_app._daily_chart(self._rows([140, 0, 37, 2]))
        self.assertEqual([t["v"] for t in ch["ticks"]], [0, 50, 100, 150], "clean steps up to the peak")
        self.assertEqual(len(ch["cols"]), 4)
        self.assertEqual(ch["cols"][-1]["label"], "10/26", "oldest on the left, newest labeled")
        self.assertEqual(ch["cols"][2]["segs"], [], "a day with no email draws nothing")
        self.assertEqual(len(ch["cols"][3]["segs"]), 2, "invoices and payments stacked")
        self.assertEqual(ch["readout"][3]["day"], "Mon 10/26")

    def test_long_period_labels_about_a_dozen_days(self):
        ch = review_app._daily_chart(self._rows([5] * 90))
        labeled = [c for c in ch["cols"] if c["label"]]
        self.assertLessEqual(len(labeled), 13)
        self.assertTrue(ch["cols"][-1]["label"])


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class Page(unittest.TestCase):

    def setUp(self):
        from starlette.testclient import TestClient
        fake = {"id": -424242, "username": "zz-report", "display_name": "zz", "role": "viewer",
                "active": True, "password_hash": "x"}
        self._saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id)
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        self.client = TestClient(review_app.app)
        self.client.post("/login", data={"username": "zz-report", "password": "x"})

    def tearDown(self):
        web_db.get_user, web_db.verify_password, web_db.get_user_by_id = self._saved

    def test_page_for_a_read_only_user(self):
        r = self.client.get("/reports/daily?days=30")
        self.assertEqual(r.status_code, 200)
        for text in ("Daily report", "Emails received per day, by type", "Day by day", "By store",
                     'class="rp-range on" href="/reports/daily?days=30"', "<svg", "What the numbers mean"):
            self.assertIn(text, r.text)
        self.assertIn('href="/reports/daily?days=14"', self.client.get("/reports/daily?days=abc").text)

    def test_csv(self):
        r = self.client.get("/reports/daily.csv?days=7")
        self.assertEqual(r.status_code, 200)
        lines = r.text.lstrip("﻿").strip().splitlines()
        self.assertTrue(lines[0].startswith("Day,Emails received,Invoice emails"))
        self.assertEqual(len(lines), 8, "header + 7 days")

    def test_menu_links_it(self):
        html = self.client.get("/dashboard").text
        self.assertIn('href="/reports/daily"', html)
        self.assertIn('href="/scheduled-runs"', html)


if __name__ == "__main__":
    unittest.main()
