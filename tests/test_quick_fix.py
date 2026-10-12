"""Quick fix on the review item page (2026-10-12): one-click store / date / amount / enter-in-QB answers
that the next run applies in code, so the gate that flagged an item can't send it back for the same
reason when the model doesn't restate the value exactly."""
import json
import os
import unittest

from _base import PureTestCase, ce, inv
import review_app

STORES = ["Maple Pharmacy", "Madison Pharmacy", "Refuah Pharmacy", "Westgate Pharmacy", "3M Pharmacy",
          "BMW", "Lakewood Rx"]


class ParseFixes(PureTestCase):

    def test_all_lines(self):
        f = ce.parse_reviewer_fixes("Store: maple pharmacy\nDate: 2026-09-14\nAmount: $1,091.33 (confirmed)\n"
                                    "Enter in QuickBooks: yes\nIt's the Maple copy.", STORES)
        self.assertEqual(f, {"store": "Maple Pharmacy", "date": "2026-09-14", "amount": 1091.33, "enter": True})

    def test_bad_values_are_ignored(self):
        f = ce.parse_reviewer_fixes("Store: Brooklyn\nDate: 2026-02-31\nAmount: 0", STORES)
        self.assertEqual(f, {})

    def test_plain_answer_has_no_fixes(self):
        self.assertEqual(ce.parse_reviewer_fixes("this is the 3M copy, enter it", STORES), {})


class ApplyFixes(PureTestCase):

    def _c(self, *records):
        return {"invoices": list(records), "_reviewer_answered": True}

    def test_unknown_store_is_set_and_gates_pass(self):
        """GT-03: 'Store could not be identified' must not come back after the reviewer picks one."""
        r = inv("Bagel Nosh", "1", 10.0, store="unknown", invoice_date="2026-09-01")
        c = self._c(r)
        ce.apply_reviewer_fixes(c, "Store: Maple Pharmacy", STORES)
        ce.apply_review_gates(c)
        self.assertEqual(r["store_matched"], "Maple Pharmacy")
        self.assertFalse(r["needs_human_review"])
        self.assertIn("Set by a reviewer", r["notes"])

    def test_card_override_respects_the_reviewer(self):
        """GT-02: a mapped card would otherwise overwrite the store the reviewer chose."""
        saved = ce.CARD_STORE_MAP
        ce.CARD_STORE_MAP = {"1234": "BMW"}
        try:
            r = inv("Bagel Nosh", "1", 10.0, store="unknown", invoice_date="2026-09-01", card_last4="1234")
            c = self._c(r)
            ce.apply_reviewer_fixes(c, "Store: Maple Pharmacy", STORES)
            ce.apply_review_gates(c)
            self.assertEqual(r["store_matched"], "Maple Pharmacy")
        finally:
            ce.CARD_STORE_MAP = saved

    def test_store_rule_accepts_the_reviewers_store(self):
        """GT-04: confirming BMW on a Carepax-addressed Abbott bill must not loop back to review."""
        r = inv("Abbott", "1605", 10.0, store="BMW", store_raw="Carepax", invoice_date="2026-09-01")
        c = self._c(r)
        ce.apply_reviewer_fixes(c, "Store: BMW", STORES)
        ce.apply_review_gates(c)
        self.assertFalse(r["needs_human_review"])

    def test_date_and_confirmed_amount_pass_their_gates(self):
        """GT-05 / GT-06: a missing date and an added-up total."""
        r = inv("Bagel Nosh", "1", 95.0, invoice_date="", total_source="derived")
        c = self._c(r)
        ce.apply_reviewer_fixes(c, "Date: 2026-09-14\nAmount: 99.50 (confirmed)", STORES)
        ce.apply_review_gates(c)
        self.assertEqual((r["invoice_date"], r["invoice_amount"]), ("2026-09-14", 99.5))
        self.assertFalse(r["needs_human_review"])
        self.assertEqual(ce._total_source(r), "", "a confirmed amount isn't saved as 'printed total'")

    def test_enter_in_quickbooks_clears_a_wrong_skip(self):
        """GT-11: the reviewer confirms a Refuah-billed Easyfast bill is real."""
        r = inv("Easyfast", "E1", 99.0, store="Refuah Pharmacy", skip_qb_import=True, invoice_date="2026-09-01")
        c = self._c(r)
        ce.apply_reviewer_fixes(c, "Enter in QuickBooks: yes", STORES)
        self.assertFalse(r["skip_qb_import"])

    def test_enter_does_not_override_an_excluded_vendor(self):
        r = inv("McKesson", "M1", 99.0, skip_qb_import=True)
        ce.apply_reviewer_fixes(self._c(r), "Enter in QuickBooks: yes", STORES)
        self.assertTrue(r["skip_qb_import"])

    def test_several_records_only_fill_blanks_and_amount_is_skipped(self):
        a = inv("Bagel Nosh", "1", 10.0, store="", invoice_date="")
        b = inv("Bagel Nosh", "2", 20.0, store="Madison Pharmacy", invoice_date="2026-09-02")
        ce.apply_reviewer_fixes(self._c(a, b), "Store: Maple Pharmacy\nDate: 2026-09-14\nAmount: 5", STORES)
        self.assertEqual((a["store_matched"], a["invoice_date"], a["invoice_amount"]),
                         ("Maple Pharmacy", "2026-09-14", 10.0))
        self.assertEqual((b["store_matched"], b["invoice_date"], b["invoice_amount"]),
                         ("Madison Pharmacy", "2026-09-02", 20.0))

    def test_no_fix_lines_changes_nothing(self):
        r = inv("Bagel Nosh", "1", 10.0, store="unknown")
        self.assertEqual(ce.apply_reviewer_fixes(self._c(r), "looks fine", STORES), {})
        self.assertEqual(r["store_matched"], "unknown")


class WebHelpers(unittest.TestCase):

    def test_kinds_follow_the_real_reason_texts(self):
        k = review_app.quick_fix_kinds
        self.assertEqual(k("Store could not be identified — needs a human to assign the correct store"), ["store"])
        self.assertEqual(k("No invoice date found — needs a human to confirm"), ["date"])
        self.assertEqual(k("This amount was calculated from line items, not read from a printed total"), ["amount"])
        self.assertEqual(k("The AI marked this bill not for QuickBooks, apparently because ..."), ["enter"])
        self.assertEqual(k("... is a rule for Sageteq, Microsoft only (Setup › Store rules), so a human must "
                           "confirm the real store"), ["store"])
        self.assertEqual(k("Possible revision of an earlier invoice"), [])

    def test_compose_round_trips_through_the_parser(self):
        ans, err = review_app.compose_quick_fix_answer(
            "Store: BMW\nold free text", store="Maple Pharmacy", date="2026-09-14", amount="$1,091.33",
            enter=True, note="Maple copy", stores=STORES)
        self.assertEqual(err, [])
        self.assertEqual(ans.count("Store:"), 1, "an earlier fix line is replaced, not stacked")
        self.assertIn("old free text", ans)
        self.assertIn("Maple copy", ans)
        self.assertEqual(ce.parse_reviewer_fixes(ans, STORES),
                         {"store": "Maple Pharmacy", "date": "2026-09-14", "amount": 1091.33, "enter": True})

    def test_compose_rejects_bad_input(self):
        for kw in ({"store": "Brooklyn"}, {"date": "14/09/2026"}, {"amount": "-3"}, {}):
            _, err = review_app.compose_quick_fix_answer("", stores=STORES, **kw)
            self.assertTrue(err, kw)


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "DATABASE_URL not set")
class PageAndRoute(unittest.TestCase):
    """The card renders and Save fix writes the Answer -- on a temp review_queue, rolled back."""

    def setUp(self):
        import database as db, web_db
        from starlette.testclient import TestClient
        self.db, self.web_db = db, web_db
        self.real = db.connect()
        self.cur = self.real.cursor()
        self.cur.execute("CREATE TEMP TABLE review_queue (LIKE public.review_queue INCLUDING ALL)")
        self.cur.execute("INSERT INTO review_queue (id, filename, source_email, vendor_matched, store_matched, "
                         "invoice_number, amount, question) VALUES (777001, 'ZZ split.pdf', 'ZZ.eml', 'Bagel Nosh', "
                         "'unknown', '280611', 50.00, 'Store could not be identified — needs a human to assign "
                         "the correct store before this goes to QuickBooks.')")
        real = self.real

        class P:
            def cursor(s, *a, **k): return real.cursor(*a, **k)
            def commit(s): pass
            def close(s): pass
            def rollback(s): pass
            def __getattr__(s, n): return getattr(real, n)
        self._saved = (web_db.connect, review_app.current_user)
        web_db.connect = lambda: P()
        review_app.current_user = lambda request: {"id": -424242, "username": "zz-test", "role": "admin",
                                                   "display_name": "zz test", "active": True}
        self.cl = TestClient(review_app.app)

    def tearDown(self):
        self.real.rollback()
        self.real.close()
        self.web_db.connect, review_app.current_user = self._saved

    def test_card_shows_the_store_fix_first(self):
        html = self.cl.get("/review/777001").text
        self.assertIn("Quick fix", html)
        card = html[html.index('id="quick-fix"'):html.index('id="ai-assist"')]
        self.assertLess(card.index('name="store"'), card.index("More fixes"))

    def test_save_fix_writes_the_answer(self):
        r = self.cl.post("/review/777001/quick-fix", data={"store": "Maple Pharmacy", "note": "scan says Maple"},
                         follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.cur.execute("SELECT answer, reviewed_by FROM review_queue WHERE id = 777001")
        answer, by = self.cur.fetchone()
        self.assertTrue(answer.startswith("Store: Maple Pharmacy"))
        self.assertEqual(by, "zz-test")

    def test_bad_fix_shows_an_error(self):
        r = self.cl.post("/review/777001/quick-fix", data={"store": "Brooklyn"}, follow_redirects=False)
        self.assertIn("fix_error=", r.headers["location"])
        self.cur.execute("SELECT answer FROM review_queue WHERE id = 777001")
        self.assertIsNone(self.cur.fetchone()[0])


if __name__ == "__main__":
    unittest.main()
