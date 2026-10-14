"""Sales orders matched to their invoice in the background (2026-10-14, Joel's rules). A held order is replaced
by its invoice even when the invoice number differs (BluPax: order #1992625 -> invoice #1204976, same vendor,
store and amount). After the hold, a later invoice replaces an order not yet entered in QuickBooks; one entered
with the same amount keeps its record (now showing the invoice's document) and the invoice is kept out; a changed
amount, or both copies entered (Rockland #9636677 / #34910), goes to a person. An order held twice is handled once.

One real connection with commit/close as no-ops, rolled back in tearDown. Skipped when DATABASE_URL isn't set.
"""
import json
import os
import unittest

from _base import ce
import database as db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
V, STORE = "ZZTEST Order Vendor", "Maple Pharmacy"


class _Proxy:
    def __init__(self, c): self.__dict__["_c"] = c
    def cursor(self, *a, **k): return self._c.cursor(*a, **k)
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass
    def __setattr__(self, k, v): pass
    def __getattr__(self, n): return getattr(self._c, n)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class SalesOrderMatching(unittest.TestCase):
    def setUp(self):
        self._real_connect = db.connect
        self.real = self._real_connect()
        self.real.autocommit = False
        self.cur = self.real.cursor()
        db.connect = lambda: _Proxy(self.real)
        self._saved = (ce.USE_DB, ce.VENDOR_STD)
        ce.USE_DB, ce.VENDOR_STD = True, {}
        # Only this test's orders: park every real held / released order out of reach for the transaction.
        self.cur.execute("UPDATE pending_sales_orders SET status = 'zz-' || status WHERE status IN ('held', 'released')")
        self.n = 0

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        db.connect = self._real_connect
        ce.USE_DB, ce.VENDOR_STD = self._saved

    def email(self):
        self.n += 1
        fn = f"ZZTEST order matching {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename, status) VALUES (%s, 'processed')", (fn,))
        return fn

    def invoice(self, number, amount, date="2026-10-01", entered=False, fn=None, file_path=None):
        fn = fn or self.email()
        self.cur.execute("INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, "
                         "invoice_date, entered_in_qb, file_path, notes, date_added) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'',current_date) RETURNING id",
                         (fn, V, STORE, number, amount, date, entered, file_path))
        return self.cur.fetchone()[0]

    def order(self, number, amount, status, date="2026-10-01", fn=None, release_in_days=3):
        fn = fn or self.email()
        snap = {"filename": fn, "invoice": {"vendor_matched": V, "store_matched": STORE, "invoice_number": number,
                                            "invoice_amount": amount, "invoice_date": date}}
        self.cur.execute("INSERT INTO pending_sales_orders (vendor_matched, store_matched, number, amount, order_date, "
                         "source_file, snapshot, status, release_after) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, "
                         "now() + %s * interval '1 day') RETURNING id",
                         (V, STORE, number, amount, date, fn, json.dumps(snap), status, release_in_days))
        return self.cur.fetchone()[0], fn

    def row(self, table, rid, cols):
        self.cur.execute(f"SELECT {cols} FROM {table} WHERE id = %s", (rid,))
        return self.cur.fetchone()

    def test_held_order_replaced_by_invoice_under_another_number(self):
        oid, _ = self.order("ZZ1992625", 151.90, "held")
        self.invoice("ZZ1204976", 151.90, date="2026-10-03")
        ce.reconcile_sales_orders("zztest")
        self.assertEqual(self.row("pending_sales_orders", oid, "status, matched_invoice"), ("superseded", "ZZ1204976"))

    def test_held_order_with_two_amount_matches_stays_held(self):
        oid, _ = self.order("ZZ500", 75.00, "held")
        self.invoice("ZZ601", 75.00)
        self.invoice("ZZ602", 75.00, date="2026-10-05")
        ce.reconcile_sales_orders("zztest")
        self.assertEqual(self.row("pending_sales_orders", oid, "status"), ("held",))

    def test_far_apart_dates_dont_match(self):
        oid, _ = self.order("ZZ700", 75.00, "held", date="2026-08-01")
        self.invoice("ZZ801", 75.00, date="2026-10-01")
        ce.reconcile_sales_orders("zztest")
        self.assertEqual(self.row("pending_sales_orders", oid, "status"), ("held",))

    def released(self, number, amount, entered=False):
        oid, fn = self.order(number, amount, "released")
        rec = self.invoice(number, amount, entered=entered, fn=fn, file_path="order.pdf")
        return oid, rec, fn

    def test_released_not_entered_invoice_wins(self):
        oid, rec, fn = self.released("ZZ9001", 100.00)
        inv = self.invoice("ZZ9001", 104.00)             # same number, amount changed: the invoice wins anyway
        ce.reconcile_sales_orders("zztest")
        self.assertEqual(self.row("invoices", rec, "duplicate_ignored, duplicate_of")[0], True)
        self.assertEqual(tuple(bool(x) for x in self.row("invoices", inv, "duplicate_ignored, duplicate_suspect")),
                         (False, False))
        self.assertEqual(self.row("pending_sales_orders", oid, "matched_invoice"), ("ZZ9001",))

    def test_released_not_entered_invoice_live_again_after_auto_ignore(self):
        oid, rec, fn = self.released("ZZ9002", 100.00)
        inv = self.invoice("ZZ9002", 100.00)
        self.cur.execute("UPDATE invoices SET duplicate_ignored = TRUE, duplicate_of = %s WHERE id = %s", (fn, inv))
        ce.reconcile_sales_orders("zztest")
        self.assertEqual(self.row("invoices", rec, "duplicate_ignored")[0], True)
        self.assertEqual(self.row("invoices", inv, "duplicate_ignored")[0], False, "the invoice is the one kept")

    def test_entered_same_amount_keeps_record_with_invoice_document(self):
        oid, rec, fn = self.released("ZZ9003", 100.00, entered=True)
        inv = self.invoice("ZZ7777", 100.00, date="2026-10-04", file_path="invoice.pdf")   # different number
        ce.reconcile_sales_orders("zztest")
        file_path, notes, ignored = self.row("invoices", rec, "file_path, notes, duplicate_ignored")
        self.assertEqual((file_path, ignored), ("invoice.pdf", False))
        self.assertIn("Invoice #ZZ7777 arrived after this sales order was entered", notes)
        self.assertEqual(self.row("invoices", inv, "duplicate_ignored, duplicate_of"), (True, fn))

    def test_entered_amount_changed_goes_to_a_person(self):
        oid, rec, fn = self.released("ZZ9004", 100.00, entered=True)
        inv = self.invoice("ZZ9004", 120.00)
        ce.reconcile_sales_orders("zztest")
        suspect, reason, group = self.row("invoices", inv, "duplicate_suspect, duplicate_reason, duplicate_group_id")
        self.assertTrue(suspect)
        self.assertIn("says $120.00", reason)
        self.assertTrue(reason.startswith("Already entered in QuickBooks, amount changed"), reason)
        self.assertEqual(self.row("invoices", rec, "duplicate_group_id")[0], group, "shown together")

    def test_both_entered_is_a_possible_double_entry(self):
        oid, rec, fn = self.released("ZZ9005", 421.20, entered=True)
        inv = self.invoice("ZZ34910", 421.20, entered=True)
        ce.reconcile_sales_orders("zztest")
        for rid in (rec, inv):
            suspect, reason = self.row("invoices", rid, "duplicate_suspect, duplicate_reason")
            self.assertTrue(suspect)
            self.assertTrue(reason.startswith("Already entered in QuickBooks, twice?"), reason)

    def test_order_held_twice_is_resolved_once(self):
        oid, rec, fn = self.released("ZZ9006", 100.00, entered=True)
        twin, _ = self.order("ZZ9006", 100.00, "released", fn=fn)
        inv = self.invoice("ZZ9006", 100.00)
        ce.reconcile_sales_orders("zztest")
        self.assertEqual(self.row("pending_sales_orders", oid, "matched_invoice"), ("ZZ9006",))
        self.assertEqual(self.row("pending_sales_orders", twin, "matched_invoice"), ("ZZ9006",))
        self.cur.execute("SELECT count(*) FROM edit_log WHERE table_name = 'invoices' AND record_id = %s "
                         "AND field = 'notes'", (rec,))
        self.assertEqual(self.cur.fetchone()[0], 1, "the note is added once")


if __name__ == "__main__":
    unittest.main()
