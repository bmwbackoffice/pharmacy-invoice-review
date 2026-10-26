"""Joel's decisions on the 2026-10-14 rule-collision analysis (the code side):
- exclusions match WHOLE vendor names ('Anda' is inside 'Standard Process', which kept 3 Refuah bills out of QB);
- a card that says one store while the document names another goes to review ("we really need a review");
- the AI gets the stores a sender's earlier bills went to, as a tie-breaker when only an address hints;
- a revised invoice a person answered in review isn't flagged again as a possible duplicate;
- "Keep this one" carries the entered-in-QuickBooks mark when the copies match (no financial impact).

DB tests use one real connection with commit/close as no-ops, rolled back in tearDown; skipped without DATABASE_URL.
"""
import os
import unittest
from unittest import mock

from _base import ce
import database as db
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class WholeNameExclusion(unittest.TestCase):
    EXCLUDED = ["Anda", "Parmed", "Parmed Pharmaceuticals", "Top Rx", "Smith Drug Company",
                "Jersey Central Power & Light", "Numed", "Maple Pharmacy"]

    def setUp(self):
        p = mock.patch.object(ce, "load_excluded_vendors", return_value=list(self.EXCLUDED))
        p.start()
        self.addCleanup(p.stop)

    def test_names_that_merely_contain_an_excluded_name_are_not_excluded(self):
        for v in ("Standard Process", "BOO BOO BANDAGE", "Landau's Health Tree", "Panda Health", "Numedica",
                  "Maple", "Pharmacy", "Rx", "Smith", "Anda New"):
            self.assertFalse(ce.is_excluded_vendor(v), v)

    def test_whole_names_match_however_written(self):
        for v in ("Anda", "ANDA, Inc.", "anda", "ParMed Pharmaceuticals", "Parmed", "Top Rx Inc",
                  "Smith Drug Co.", "Jersey Central Power and Light", "Maple Pharmacy"):
            self.assertTrue(ce.is_excluded_vendor(v), v)

    def test_blank(self):
        self.assertFalse(ce.is_excluded_vendor(""))
        self.assertFalse(ce.is_excluded_vendor(None))


class DuplicateSetDifferences(unittest.TestCase):
    def test_odd_store_and_date_and_number_characters(self):
        recs = [{"number": "FI26136794", "store_matched": "Westgate Pharmacy", "record_date": "2026-09-17"},
                {"number": "F12613674", "store_matched": "Westgate Pharmacy", "record_date": "2026-09-17"},
                {"number": "FI26136794", "store_matched": "Maple Pharmacy", "record_date": "2026-09-18"}]
        web_db._mark_set_differences(recs)
        self.assertEqual([r["store_diff"] for r in recs], [False, False, True])
        self.assertEqual([r["date_diff"] for r in recs], [False, False, True])
        # the number most copies share is the reference; the odd one is compared with it, and vice versa
        self.assertEqual([r["number_vs"] for r in recs], ["F12613674", "FI26136794", "F12613674"])

    def test_a_set_that_agrees_has_no_marks(self):
        recs = [{"number": "1302", "store_matched": "Westgate Pharmacy", "record_date": "2026-10-11"},
                {"number": "1302 ", "store_matched": "westgate pharmacy", "record_date": "2026-10-11"}]
        web_db._mark_set_differences(recs)
        self.assertEqual([(r["store_diff"], r["date_diff"], r["number_vs"]) for r in recs], [(False, False, None)] * 2)


class StoreListFollowsSetup(unittest.TestCase):
    def test_a_store_added_in_setup_reaches_the_ai(self):
        stores = [{"store_name": "Maple Pharmacy", "address": "59 Route 59"},
                  {"store_name": "Newtown Pharmacy", "aliases": "legal name Newtown Rx LLC", "address": "1 Main St"}]
        p = ce.build_system_prompt(stores=stores, excluded_vendors=[], card_store_map={})
        self.assertIn("- Newtown Pharmacy (legal name Newtown Rx LLC) — 1 Main St", p)
        self.assertIn('"store_matched": "Maple Pharmacy | Newtown Pharmacy | unknown"', p)
        self.assertNotIn("%%", p)
        self.assertNotRegex(p, r"(?i)\bsix\b")

    def test_setup_store_rules_reach_the_ai(self):
        rules = [{"reference": "Carepax", "store_matched": "BMW", "applies_to": "every vendor except these",
                  "vendors": ["Schwartz & Company"]},
                 {"reference": "Accuscript", "store_matched": "Lakewood Rx", "applies_to": "only these vendors",
                  "vendors": ["East Coast Building Maintenance"]}]
        p = ce.build_system_prompt(stores=[{"store_name": "BMW"}], excluded_vendors=[], card_store_map={},
                                   store_rules=rules)
        self.assertIn('- "Carepax" on a bill means BMW, for every vendor except: Schwartz & Company.', p)
        self.assertIn('- "Accuscript" on a bill from East Coast Building Maintenance means Lakewood Rx. On a bill from '
                      'any other vendor, "Accuscript" alone does not decide the store.', p)
        self.assertIn("It is NOT evidence of the store", p)        # our own "Maple - …" review file names

    def test_an_exception_to_an_every_vendor_rule_reads_cleanly(self):
        rules = [{"reference": "Carepax", "store_matched": "BMW", "applies_to": "every vendor except these",
                  "vendors": ["Schwartz & Company", "Abbott"]},
                 {"reference": "Carepax", "store_matched": "Maple Pharmacy", "applies_to": "only these vendors",
                  "vendors": ["Abbott"]}]
        p = ce.build_system_prompt(stores=[{"store_name": "BMW", "back_office": True}], excluded_vendors=[],
                                   card_store_map={}, store_rules=rules)
        self.assertIn('- "Carepax" on a bill from Abbott means Maple Pharmacy.\n', p)   # no "doesn't decide" clause
        self.assertIn("Back office: it sells no goods", p)


class OutOfStockLines(unittest.TestCase):
    """2026-10-15 (Joel: both). Masterko's 'items out of stock' page ($162, never shipped), scanned on its own, was
    held as a sales order and would have become a $162 bill. And a Melissa & Doug order recorded as the bill
    ($1,485.48) included two lines 'On Backorder' ($120.24). On invoices the AI already leaves such lines out."""

    def test_the_ai_is_told(self):
        p = ce.build_system_prompt(stores=[{"store_name": "Maple Pharmacy"}], excluded_vendors=[], card_store_map={})
        self.assertIn("a document that lists ONLY such items (e.g. a delivery slip's separate 'items out of stock' "
                      "page, scanned on its own) is not a bill at all", p)
        self.assertIn("the printed total MINUS the extended amounts of the lines marked backordered / out of stock / "
                      "not shipped", p)
        self.assertIn("total_source stays 'stated' when you start from the printed total", p)
        self.assertIn("One exception, for sales orders and priced packing slips only", p)   # beside the read-the-total rule


class BackOfficeGoods(unittest.TestCase):
    def setUp(self):
        self._saved = ce._BACK_OFFICE
        ce._BACK_OFFICE = {"bmw", "lakewood rx"}

    def tearDown(self):
        ce._BACK_OFFICE = self._saved

    def check(self, store, account, **kw):
        rec = dict({"store_matched": store, "standard_account": account, "needs_human_review": False}, **kw)
        ce.apply_back_office_goods_gate(rec)
        return rec

    def test_cost_of_goods_on_a_back_office_goes_to_review(self):
        for store, account in (("BMW", "Purchase:Vitamins"), ("Lakewood Rx", "Purchase:Rx Generics"),
                               ("BMW", "Inventory:Vitamin Inventory"), ("BMW", "Purchase")):
            r = self.check(store, account)
            self.assertTrue(r["needs_human_review"], (store, account))
            self.assertIn("back office and sells no goods", r["review_reason"])

    def test_expenses_pharmacies_and_people_decisions_pass(self):
        for store, account, kw in (("BMW", "Computer Exp:Software", {}), ("Maple Pharmacy", "Purchase:Vitamins", {}),
                                   ("BMW", "Purchase Order Fees", {}), ("BMW", "Purchase:Vitamins", {"_reviewer_store": True})):
            self.assertFalse(self.check(store, account, **kw)["needs_human_review"], (store, account))
        rec = {"store_matched": "BMW", "standard_account": "Purchase:Vitamins", "needs_human_review": False}
        ce.apply_back_office_goods_gate(rec, reviewer_answered=True)
        self.assertFalse(rec["needs_human_review"], "a reviewer's answer stands")


class ExcludedVendorOrdersAreNotHeld(unittest.TestCase):
    def test_excluded_vendor_order_is_recorded_not_held(self):
        with mock.patch.object(ce, "load_excluded_vendors", return_value=["Parmed", "BluPax"]):
            c = {"invoices": [{"vendor_matched": "ParMed", "invoice_number": "1140057240", "is_sales_order": True},
                              {"vendor_matched": "Bened Life", "invoice_number": "126877", "is_sales_order": True}]}
            ce.apply_excluded_vendor_order_check(c)
        parmed, bened = c["invoices"]
        self.assertFalse(parmed["is_sales_order"])
        self.assertIn("excluded vendor: recorded right away, not held", parmed["notes"])
        self.assertTrue(bened["is_sales_order"], "a QuickBooks vendor's order is still held")


class CardVersusDocument(unittest.TestCase):
    def setUp(self):
        self._saved = ce.CARD_STORE_MAP
        ce.CARD_STORE_MAP = {"8400": "3M Pharmacy"}

    def tearDown(self):
        ce.CARD_STORE_MAP = self._saved

    def rec(self, store, **kw):
        return dict({"store_matched": store, "card_last4": "8400", "needs_human_review": False}, **kw)

    def test_document_names_another_store_goes_to_review(self):
        r = self.rec("Refuah Pharmacy")
        ce.apply_card_store_map(r)
        self.assertTrue(r["needs_human_review"])
        self.assertEqual(r["store_matched"], "3M Pharmacy")
        self.assertIn("points to Refuah Pharmacy, but card ending 8400 belongs to 3M Pharmacy", r["review_reason"])
        ce.apply_card_store_map(r)                       # the record-time re-run doesn't repeat it
        self.assertEqual(r["review_reason"].count("card ending 8400"), 1)

    def test_agreeing_or_unknown_store_is_not_flagged(self):
        for store in ("3M Pharmacy", "unknown", "", None):
            r = self.rec(store)
            ce.apply_card_store_map(r)
            self.assertFalse(r["needs_human_review"], store)
            self.assertEqual(r["store_matched"], "3M Pharmacy")

    def test_after_a_reviewers_answer_their_store_stands(self):
        c = {"_reviewer_answered": True, "invoices": [self.rec("Refuah Pharmacy", invoice_amount=10.0,
                                                               invoice_date="2026-10-01", total_source="stated")]}
        ce.apply_review_gates(c)
        inv = c["invoices"][0]
        self.assertEqual(inv["store_matched"], "Refuah Pharmacy")
        self.assertNotIn("card ending 8400", inv.get("review_reason") or "")

    def test_quick_fix_store_is_never_touched(self):
        r = self.rec("Refuah Pharmacy", _reviewer_store=True)
        ce.apply_card_store_map(r)
        self.assertEqual((r["store_matched"], r["needs_human_review"]), ("Refuah Pharmacy", False))


class _Proxy:
    def __init__(self, c): self.__dict__["_c"] = c
    def cursor(self, *a, **k): return self._c.cursor(*a, **k)
    def commit(self): pass
    def close(self): pass
    def rollback(self): pass
    def __setattr__(self, k, v): pass
    def __getattr__(self, n): return getattr(self._c, n)


class _DbCase(unittest.TestCase):
    def setUp(self):
        self._real_connect = db.connect
        self.real = self._real_connect()
        self.real.autocommit = False
        self.cur = self.real.cursor()
        db.connect = lambda: _Proxy(self.real)
        # The pipeline's DB handle without database.init_db(): its ALTER TABLEs would otherwise run inside
        # this test's transaction and hold table locks until the rollback.
        self._saved_db = ce._DB
        ce._DB = db
        self._saved = (ce.USE_DB, ce.VENDOR_STD, ce.SENDER_STORE_MAP, ce.CONTACT_STORE_MAP)
        ce.USE_DB, ce.VENDOR_STD = True, {}
        ce.SENDER_STORE_MAP, ce.CONTACT_STORE_MAP = {"scanner@maplerx.com": "Maple Pharmacy"}, {}
        ce._SENDER_HISTORY.clear()
        self.n = 0

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        db.connect = self._real_connect
        ce._DB = self._saved_db
        ce.USE_DB, ce.VENDOR_STD, ce.SENDER_STORE_MAP, ce.CONTACT_STORE_MAP = self._saved
        ce._SENDER_HISTORY.clear()

    def email(self, sender="billing@zzvendor.example"):
        self.n += 1
        fn = f"ZZTEST rule decisions {self.n}.eml"
        self.cur.execute("INSERT INTO emails (filename, status, from_address) VALUES (%s, 'processed', %s)", (fn, sender))
        return fn

    def invoice(self, store="Maple Pharmacy", number=None, amount=10.0, entered=False, fn=None, sender=None,
                vendor="ZZTEST Rule Vendor"):
        fn = fn or self.email(sender or "billing@zzvendor.example")
        self.cur.execute("INSERT INTO invoices (email_filename, vendor_matched, store_matched, invoice_number, amount, "
                         "invoice_date, entered_in_qb, notes, date_added) VALUES (%s,%s,%s,%s,%s,'2026-10-01',%s,'',"
                         "current_date) RETURNING id",
                         (fn, vendor, store, number or f"ZZ{self.n}", amount, entered))
        return self.cur.fetchone()[0], fn


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class SenderHistoryHint(_DbCase):
    def test_one_store_dominates(self):
        for _ in range(3):
            self.invoice("Maple Pharmacy", sender="billing@zzvendor.example")
        hint = ce._sender_history_hint("From: Billing <billing@zzvendor.example>\nTo: ap@lakewoodrx.com\n")
        self.assertIn("Maple Pharmacy 3", hint)
        self.assertIn("A tie-breaker only", hint)

    def test_mixed_history_one_record_own_mailboxes_and_scanners_get_none(self):
        for store in ("Maple Pharmacy", "Maple Pharmacy", "Refuah Pharmacy", "Refuah Pharmacy"):
            self.invoice(store, sender="ar@zzwholesaler.example")
        self.invoice("Maple Pharmacy", sender="once@zzonce.example")
        for _ in range(3):
            self.invoice("Maple Pharmacy", sender="amy@maplerx.com")
            self.invoice("Maple Pharmacy", sender="scanner@maplerx.com")
        for sender in ("ar@zzwholesaler.example", "once@zzonce.example", "amy@maplerx.com", "scanner@maplerx.com"):
            self.assertEqual(ce._sender_history_hint(f"From: <{sender}>\n"), "", sender)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class AnsweredRevision(_DbCase):
    def run_check(self, answered, prior_entered=False):
        prior_id, prior_fn = self.invoice(number="ZZREV1", amount=100.0, entered=prior_entered)
        history = [{"vendor_matched": "ZZTEST Rule Vendor", "store_matched": "Maple Pharmacy",
                    "invoice_number": "ZZREV1", "invoice_amount": 100.0, "source_file": prior_fn,
                    "source_email_id": prior_fn}]
        inv = {"vendor_matched": "ZZTEST Rule Vendor", "store_matched": "Maple Pharmacy", "invoice_number": "ZZREV1",
               "invoice_amount": 120.0, "invoice_date": "2026-10-02", "needs_human_review": False}
        result = {"filename": self.email(), "classification": {"_reviewer_answered": answered, "invoices": [inv]}}
        ce.apply_duplicate_checks([result], history, [])
        self.cur.execute("SELECT duplicate_ignored, duplicate_reason FROM invoices WHERE id = %s", (prior_id,))
        return inv, self.cur.fetchone()

    def test_answered_copy_kept_earlier_version_set_aside(self):
        inv, (prior_ignored, prior_reason) = self.run_check(answered=True)
        self.assertFalse(inv["is_duplicate_suspect"])
        self.assertTrue(prior_ignored)
        self.assertIn("answered in review", prior_reason)

    def test_earlier_version_in_quickbooks_is_flagged_saying_so(self):
        inv, (prior_ignored, _) = self.run_check(answered=True, prior_entered=True)
        self.assertTrue(inv["is_duplicate_suspect"])
        self.assertTrue(inv["duplicate_reason"].startswith("Already entered in QuickBooks, amount changed"))
        self.assertFalse(prior_ignored)

    def test_earlier_version_answered_in_the_same_run_is_flagged_not_left_live(self):
        # Schwartz #236738 (10/14 13:35): two versions answered together; the first isn't a saved record yet.
        history = [{"vendor_matched": "ZZTEST Rule Vendor", "store_matched": "Maple Pharmacy",
                    "invoice_number": "ZZREV2", "invoice_amount": 450.0, "source_file": "ZZTEST not saved yet.pdf",
                    "source_email_id": "ZZTEST not saved yet.pdf"}]
        inv = {"vendor_matched": "ZZTEST Rule Vendor", "store_matched": "Carepax", "invoice_number": "ZZREV2",
               "invoice_amount": 1800.0, "invoice_date": "2026-09-23", "needs_human_review": False}
        result = {"filename": self.email(), "classification": {"_reviewer_answered": True, "invoices": [inv]}}
        ce.apply_duplicate_checks([result], history, [])
        self.assertTrue(inv["is_duplicate_suspect"])
        self.assertIn("likely a REVISED", inv["duplicate_reason"])

    def test_not_answered_is_still_a_possible_duplicate(self):
        inv, (prior_ignored, _) = self.run_check(answered=False)
        self.assertTrue(inv["is_duplicate_suspect"])
        self.assertIn("likely a REVISED", inv["duplicate_reason"])
        self.assertFalse(prior_ignored)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class KeepCarriesEntered(_DbCase):
    def keep(self, other_amount=50.0, other_store="Maple Pharmacy"):
        entered, _ = self.invoice(number="ZZKEEP", amount=other_amount, store=other_store, entered=True)
        kept, _ = self.invoice(number="ZZKEEP", amount=50.0, store="Maple Pharmacy")
        res = web_db.keep_and_ignore_others("invoice", kept, [("invoice", entered)], "zz-test")
        self.cur.execute("SELECT entered_in_qb FROM invoices WHERE id = %s", (kept,))
        kept_entered = bool(self.cur.fetchone()[0])
        self.cur.execute("SELECT duplicate_ignored FROM invoices WHERE id = %s", (entered,))
        return res, kept_entered, bool(self.cur.fetchone()[0])

    def test_same_amount_and_store_carries_the_mark(self):
        res, kept_entered, other_ignored = self.keep()
        self.assertEqual((res["carried"], kept_entered, other_ignored), (True, True, True))

    def test_different_amount_or_store_does_not(self):
        for kw in ({"other_amount": 55.0}, {"other_store": "Refuah Pharmacy"}):
            res, kept_entered, other_ignored = self.keep(**kw)
            self.assertEqual((res["carried"], kept_entered, other_ignored), (False, False, True), kw)
            self.assertEqual(len(res["differs"]), 1)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class RunSavesOnlyTheLearningsItAdded(_DbCase):
    def test_a_learning_deleted_during_a_run_stays_deleted(self):
        self.cur.execute("INSERT INTO learnings (learned_rule, date_added, from_email) "
                         "VALUES ('ZZTEST rule deleted in Setup during the run', current_date, 'zz')")
        loaded = [{"Learned Rule": "ZZTEST rule deleted in Setup during the run", "Date Added": "2026-10-14"}]
        self.cur.execute("DELETE FROM learnings WHERE learned_rule = 'ZZTEST rule deleted in Setup during the run'")
        self.cur.execute("SELECT count(*) FROM learnings")
        before = self.cur.fetchone()[0]
        new = {"Learned Rule": "ZZTEST rule a reviewer taught this run", "Date Added": "2026-10-14", "From Email": "zz"}
        ce.save_learnings(loaded + [new], added=[new])
        self.cur.execute("SELECT learned_rule FROM learnings WHERE learned_rule LIKE 'ZZTEST rule%%'")
        self.assertEqual([r[0] for r in self.cur.fetchall()], ["ZZTEST rule a reviewer taught this run"])
        self.cur.execute("SELECT count(*) FROM learnings")
        self.assertEqual(self.cur.fetchone()[0], before + 1, "nothing else deleted or re-inserted")


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class LastQbEntryByStore(_DbCase):
    def test_last_sitting_per_store_counts_clicks_only(self):
        self.cur.execute("INSERT INTO stores (store_name, created_by) VALUES ('ZZTEST Store', 'zz')")
        marks = [("zz-test", 10), ("zz-test", 20), ("zz-test", 70), ("zz-test", 300),
                 ("joel via QB reconcile import", 1), ("auto-resolved on arrival", 2)]
        for who, minutes_ago in marks:
            rid, _ = self.invoice(store="ZZTEST Store")
            self.cur.execute("UPDATE invoices SET entered_in_qb = TRUE, entered_qb_by = %s, "
                             "entered_qb_at = now() - %s * interval '1 minute' WHERE id = %s", (who, minutes_ago, rid))
        row = next(r for r in web_db.last_qb_entry_by_store() if r["store"] == "ZZTEST Store")
        self.cur.execute("SELECT now() - interval '10 minutes'")
        expected = self.cur.fetchone()[0]
        self.assertLess(abs((row["last_at"] - expected).total_seconds()), 5, "the reconcile and auto marks don't count")
        self.assertEqual(row["count"], 3, "10, 20 and 70 minutes ago are one sitting; 5 hours ago is an earlier one")

    def test_ready_to_export_counts_qb_entry_records_only(self):
        self.cur.execute("INSERT INTO stores (store_name, created_by) VALUES ('ZZTEST Ready Store', 'zz')")
        self.invoice(store="ZZTEST Ready Store")                                     # ready
        rid, _ = self.invoice(store="ZZTEST Ready Store")                            # flagged: not ready
        self.cur.execute("UPDATE invoices SET duplicate_suspect = TRUE WHERE id = %s", (rid,))
        self.cur.execute("INSERT INTO payments (email_filename, vendor_matched, store_matched, amount, entered_in_qb) "
                         "VALUES (%s, 'ZZTEST Rule Vendor', 'ZZTEST Ready Store', 5, FALSE)", (self.email(),))
        row = next(r for r in web_db.last_qb_entry_by_store() if r["store"] == "ZZTEST Ready Store")
        self.assertEqual(row["ready"], 1, "the flagged invoice and the payment don't count")

    def test_a_store_with_no_clicks_shows_none(self):
        self.cur.execute("INSERT INTO stores (store_name, created_by) VALUES ('ZZTEST Quiet Store', 'zz')")
        row = next(r for r in web_db.last_qb_entry_by_store() if r["store"] == "ZZTEST Quiet Store")
        self.assertEqual((row["last_at"], row["count"]), (None, 0))


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class SplitPayments(_DbCase):
    """Anda ACH HJEO7TVORL (10/14): recorded as one $23,754.47 total and again as 18 invoice lines adding up to it."""
    def payment(self, amount, invoice=None, entered=False, ref="ZZREF77", suspect=False):
        fn = self.email()
        self.cur.execute("INSERT INTO payments (email_filename, vendor_matched, store_matched, reference_number, "
                         "invoice_number, amount, payment_date, entered_in_qb, duplicate_suspect, notes) "
                         "VALUES (%s,'ZZTEST Split Vendor','Maple Pharmacy',%s,%s,%s,'2026-09-23',%s,%s,'') RETURNING id",
                         (fn, ref, invoice, amount, entered, suspect))
        return self.cur.fetchone()[0]

    def state(self, pid):
        self.cur.execute("SELECT duplicate_ignored, duplicate_suspect, notes FROM payments WHERE id = %s", (pid,))
        return self.cur.fetchone()

    def test_total_kept_lines_set_aside_with_the_invoice_list(self):
        total = self.payment(100.00, suspect=True)
        lines = [self.payment(60.00, "ZZI1"), self.payment(30.00, "ZZI2"), self.payment(10.00, "ZZI3")]
        res = ce.resolve_split_payments("zz-test")
        mine = [r for r in res["resolved"] if r["keep_id"] == total]
        self.assertEqual(sorted(mine[0]["ignored_ids"]), sorted(lines))
        ignored, suspect, notes = self.state(total)
        self.assertEqual((bool(ignored), bool(suspect)), (False, False))
        self.assertIn("ZZI1 $60.00; ZZI2 $30.00; ZZI3 $10.00", notes)
        self.assertTrue(all(self.state(l)[0] for l in lines))
        self.assertFalse([r for r in ce.resolve_split_payments("zz-test")["resolved"] if r["keep_id"] == total],
                         "running it again changes nothing")

    def test_no_match_or_entered_lines_are_left_to_a_person(self):
        total = self.payment(100.00, ref="ZZREF78")
        self.payment(60.00, "ZZI4", ref="ZZREF78"); self.payment(30.00, "ZZI5", ref="ZZREF78")   # adds up to 90
        total2 = self.payment(50.00, ref="ZZREF79")
        self.payment(25.00, "ZZI6", ref="ZZREF79", entered=True); self.payment(25.00, "ZZI7", ref="ZZREF79")
        keeps = {r["keep_id"] for r in ce.resolve_split_payments("zz-test")["resolved"]}
        self.assertFalse({total, total2} & keeps)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class LedgerStoreFollowsTheRecord(_DbCase):
    def ledger_row(self, fn, number, amount, store):
        self.cur.execute("INSERT INTO invoice_history (vendor_matched, store_matched, invoice_number, invoice_amount, "
                         "source_file, source_email_id, date_added) VALUES ('ZZTEST Rule Vendor', %s, %s, %s, %s, %s, "
                         "current_date) RETURNING id", (store, number, amount, fn, fn))
        return self.cur.fetchone()[0]

    def test_a_store_edit_reaches_the_ledger(self):
        rid, fn = self.invoice(store="BMW", number="ZZLEDGER1", amount=694.0)
        hid = self.ledger_row(fn, "ZZLEDGER1", 694.0, "BMW")
        web_db.update_record_field("invoice", rid, "store", "Maple Pharmacy", "zz-test")
        self.cur.execute("SELECT store_matched FROM invoice_history WHERE id = %s", (hid,))
        self.assertEqual(self.cur.fetchone()[0], "Maple Pharmacy")

    def test_a_copy_matching_the_records_current_store_is_a_plain_duplicate(self):
        # Abbott #622312457: the ledger still says BMW, the record was moved to Maple, a new copy reads Maple.
        rid, fn = self.invoice(store="Maple Pharmacy", number="ZZLEDGER2", amount=694.0, entered=True)
        history = [{"vendor_matched": "ZZTEST Rule Vendor", "store_matched": "BMW", "invoice_number": "ZZLEDGER2",
                    "invoice_amount": 694.0, "source_file": fn, "source_email_id": fn}]
        inv = {"vendor_matched": "ZZTEST Rule Vendor", "store_matched": "Maple Pharmacy", "invoice_number": "ZZLEDGER2",
               "invoice_amount": 694.0, "invoice_date": "2026-08-26", "needs_human_review": False}
        ce.apply_duplicate_checks([{"filename": self.email(), "classification": {"invoices": [inv]}}], history, [])
        self.assertTrue(inv["duplicate_ignored"])
        self.assertIn("since moved to Maple Pharmacy", inv["duplicate_reason"])


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class HeldOrdersInRecordSearch(_DbCase):
    def test_a_held_order_shows_labelled_held(self):
        # This session's view definition (the web app applies WEB_DDL at startup); rolled back with the test.
        start = web_db.WEB_DDL.index("CREATE OR REPLACE VIEW all_records AS")
        end = web_db.WEB_DDL.index("WHERE so.status = 'held';", start) + len("WHERE so.status = 'held';")
        self.cur.execute(web_db.WEB_DDL[start:end])
        fn = self.email()
        for status in ("held", "released"):
            self.cur.execute("INSERT INTO pending_sales_orders (vendor_matched, store_matched, number, amount, order_date, "
                             "source_email, status, release_after) VALUES ('ZZTEST Held Vendor', 'Maple Pharmacy', "
                             "%s, 1386, '2026-09-17', %s, %s, '2026-10-17 11:46')", (f"ZZ-{status}", fn, status))
        rows = web_db.list_all_records(search="ZZ-")
        rows = rows if isinstance(rows, list) else rows[0]
        mine = [r for r in rows if r.get("vendor_matched") == "ZZTEST Held Vendor"]
        self.assertEqual([r["number"] for r in mine], ["ZZ-held"], "only while held")
        self.assertEqual(mine[0]["record_type"], "sales_order")
        self.assertIn("Held sales order: recorded on 10/17/26", mine[0]["notes"])


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class ExcludedFilterMatchesThePipeline(_DbCase):
    def test_filter_uses_whole_names(self):
        with mock.patch.object(ce, "load_excluded_vendors", return_value=["ZZTEST Anda"]):
            self.invoice(vendor="ZZTEST Anda")
            self.invoice(vendor="ZZTEST Standard Process")      # contains 'anda' and that's all
            self.invoice(vendor="ZZTEST Anda, Inc.")
            frag = web_db._excluded_vendor_sql("only", "vendor_matched")
            self.cur.execute(f"SELECT vendor_matched FROM invoices WHERE vendor_matched LIKE 'ZZTEST %%' AND {frag}")
            self.assertCountEqual([r[0] for r in self.cur.fetchall()], ["ZZTEST Anda", "ZZTEST Anda, Inc."])


if __name__ == "__main__":
    unittest.main()
