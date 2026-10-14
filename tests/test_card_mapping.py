"""Mapping a card gives its store to the records waiting for QuickBooks that have none (2026-10-13). Joel mapped
card 6641 and asked whether that updates what's already there: it didn't (assign_card_store only saved the
mapping). Now the Cards tab and Setup › Card → store also fill in the store on not-entered invoices, credit
memos and payments that carry the card and have a blank or 'unknown' store.

One real connection with commit/close as no-ops, rolled back in tearDown. Skipped when DATABASE_URL isn't set.
"""
import os
import unittest

import _base  # noqa: F401
import database as db
import review_app
import web_db
from starlette.testclient import TestClient

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
CARD = "9091"                      # not a real card on file
FN = "ZZTEST card mapping.eml"


class _Proxy:
    def __init__(self, c): self.__dict__["_c"] = c
    def cursor(self, *a, **k): return self._c.cursor(*a, **k)
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass
    def __setattr__(self, k, v): pass
    def __getattr__(self, n): return getattr(self._c, n)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class CardMappingFillsStores(unittest.TestCase):
    def setUp(self):
        self._real_connect = db.connect
        self.real = self._real_connect()
        self.real.autocommit = False
        self.cur = self.real.cursor()
        db.connect = lambda: _Proxy(self.real)
        c = self.cur
        c.execute("SELECT count(*) FROM card_store_map WHERE card_last4 = %s", (CARD,))
        assert c.fetchone()[0] == 0, "test card number is in use"
        c.execute("INSERT INTO emails (filename, status) VALUES (%s, 'processed')", (FN,))

        def pay(store, entered=False, card=CARD, ref="", notes=""):
            c.execute("INSERT INTO payments (email_filename, vendor_matched, store_matched, reference_number, amount, "
                      "payment_date, card_last4, entered_in_qb, notes) VALUES (%s,'ZZTEST Vendor',%s,%s,10,'2026-10-13',"
                      "%s,%s,%s) RETURNING id", (FN, store, ref, card, entered, notes))
            return c.fetchone()[0]
        self.blank, self.unknown = pay(""), pay("unknown")
        self.has_store = pay("Madison Pharmacy")
        self.entered = pay("", entered=True)
        self.in_ref = pay("", card=None, ref=f"Visa ending in {CARD}")
        self.other = pay("", card="9092")
        c.execute("INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, "
                  "card_last4, entered_in_qb) VALUES (%s,'ZZTEST Vendor','',%s,10,%s,FALSE) RETURNING id",
                  (FN, "ZZ-1", CARD))
        self.inv = c.fetchone()[0]

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        db.connect = self._real_connect

    def stores(self):
        self.cur.execute("SELECT id, store_matched FROM payments WHERE email_filename = %s", (FN,))
        out = dict(self.cur.fetchall())
        self.cur.execute("SELECT id, store_matched FROM invoices WHERE email_filename = %s", (FN,))
        out.update(self.cur.fetchall())
        return out

    def test_only_records_without_a_store_get_it(self):
        web_db.assign_card_store(CARD, "Maple Pharmacy", "zz-test")
        changed = web_db.apply_card_map_to_records("zz-test", CARD)
        s = self.stores()
        self.assertEqual({(t, i) for t, i, _, _ in changed},
                         {("payments", self.blank), ("payments", self.unknown), ("payments", self.in_ref),
                          ("invoices", self.inv)})
        for i in (self.blank, self.unknown, self.in_ref, self.inv):
            self.assertEqual(s[i], "Maple Pharmacy")
        self.assertEqual(s[self.has_store], "Madison Pharmacy", "a store already set is left alone")
        self.assertEqual(s[self.entered], "", "entered in QuickBooks: never changed")
        self.assertEqual(s[self.other], "", "another card")
        self.cur.execute("SELECT count(*) FROM edit_log WHERE field = 'store_matched' AND changed_by = %s",
                         (f"zz-test (card {CARD} mapped to Maple Pharmacy)",))
        self.assertEqual(self.cur.fetchone()[0], 4)
        self.assertEqual(web_db.apply_card_map_to_records("zz-test", CARD), [], "running it again changes nothing")

    def test_cards_tab_reports_how_many(self):
        saved = review_app.current_user
        review_app.current_user = lambda r: {"id": -424242, "username": "zz-test", "role": "reviewer",
                                             "display_name": "zz", "active": True}
        try:
            cl = TestClient(review_app.app)
            r = cl.post("/cards/assign", data={"card_last4": CARD, "store_matched": "Maple Pharmacy"},
                        follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            self.assertIn("updated=4", r.headers["location"])
            html = cl.get(r.headers["location"]).text
            self.assertIn(f"Card {CARD} saved.", html)
            self.assertIn("4 records waiting for QuickBooks that had no store now", html)
        finally:
            review_app.current_user = saved


if __name__ == "__main__":
    unittest.main()
