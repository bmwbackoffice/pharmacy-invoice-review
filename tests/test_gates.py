"""Deterministic review gates (spec area GT). Each test is a real incident or rule."""
from pathlib import Path
from unittest import mock

from _base import PureTestCase, ce, inv


class BasicGates(PureTestCase):

    def test_missing_date_flags(self):
        """GT-05."""
        r = inv("Bagel Nosh", "1", 10.0, invoice_date="")
        ce.apply_date_review_gate(r, "invoice_date")
        self.assertTrue(r["needs_human_review"])

    def test_missing_or_negative_amount_flags(self):
        """GT-06."""
        for amount in (None, 0, -5):
            r = inv("Bagel Nosh", "1", amount)
            ce.apply_amount_review_gate(r, "invoice_amount")
            self.assertTrue(r["needs_human_review"], amount)

    def test_derived_total_flags(self):
        """GT-06: an amount added up from line items needs a person."""
        r = inv("Bagel Nosh", "1", 10.0, total_source="derived")
        ce.apply_amount_review_gate(r, "invoice_amount")
        self.assertTrue(r["needs_human_review"])
        r = inv("Bagel Nosh", "1", 10.0)                 # missing total_source is a no-op
        ce.apply_amount_review_gate(r, "invoice_amount")
        self.assertFalse(r["needs_human_review"])

    def test_unknown_store_flags_but_not_for_excluded_vendor(self):
        """GT-03."""
        r = inv("Bagel Nosh", "1", 10.0, store="unknown")
        ce.apply_store_review_gate(r)
        self.assertTrue(r["needs_human_review"])
        r = inv("McKesson", "1", 10.0, store="")
        ce.apply_store_review_gate(r)
        self.assertFalse(r["needs_human_review"])
        p = {"vendor_matched": "McKesson", "store_matched": "", "needs_human_review": False}
        ce.apply_store_review_gate(p, is_payment=True)
        self.assertTrue(p["needs_human_review"], "payments are always gated")

    def test_reason_sentence_appears_once(self):
        """GT-10: the gates run twice; 'No invoice date found' used to show twice."""
        r = inv("Bagel Nosh", "1", 10.0, invoice_date="")
        ce.apply_date_review_gate(r, "invoice_date")
        ce.apply_date_review_gate(r, "invoice_date")
        self.assertEqual(r["review_reason"].count("No invoice date found"), 1)


class StoreScopeRules(PureTestCase):

    def test_carepax_leak_onto_another_vendor_flags(self):
        """GT-04: 'Carepax = BMW' (a Sageteq rule) leaked onto Abbott #1605."""
        r = inv("Abbott", "1605", 10.0, store="BMW", store_raw="Carepax, 1 Main St")
        ce.apply_scoped_learning_gate(r)
        self.assertTrue(r["needs_human_review"])

    def test_carepax_for_sageteq_is_fine(self):
        r = inv("Sageteq", "77", 10.0, store="BMW", store_raw="Madison / Carepax")
        ce.apply_scoped_learning_gate(r)
        self.assertFalse(r["needs_human_review"])

    def test_112_hillside_is_westgate_except_braun(self):
        """GT-04: '112 Hillside' means Westgate for every vendor except Braun."""
        r = inv("Bagel Nosh", "1", 10.0, store="Maple Pharmacy", store_raw="112 Hillside Ave")
        ce.apply_scoped_learning_gate(r)
        self.assertTrue(r["needs_human_review"])
        r = inv("Braun", "1", 10.0, store="Maple Pharmacy", store_raw="112 Hillside Ave")
        ce.apply_scoped_learning_gate(r)
        self.assertFalse(r["needs_human_review"])


class UnlistedSkipGate(PureTestCase):
    """GT-11: 24 real bills were excluded because the store BILLED (3M, Refuah...) is on the list."""

    def test_real_bill_to_an_excluded_store_goes_to_review(self):
        r = inv("Easyfast", "E1", 99.0, store="Refuah Pharmacy", skip_qb_import=True)
        c = {"invoices": [r], "_reviewer_answered": False}
        ce.apply_unlisted_skip_gate(r, "invoice", c)
        self.assertFalse(r["skip_qb_import"])
        self.assertTrue(r["needs_human_review"])
        self.assertIn("store it's billed to (Refuah Pharmacy)", r["review_reason"])

    def test_excluded_vendor_keeps_its_skip(self):
        r = inv("McKesson", "M1", 99.0, skip_qb_import=True)
        ce.apply_unlisted_skip_gate(r, "invoice", {"invoices": [r]})
        self.assertTrue(r["skip_qb_import"])
        self.assertFalse(r["needs_human_review"])

    def test_cost_split_is_left_alone(self):
        """The same number for several stores in one email is a multi-store cost split."""
        a = inv("Optimum", "CS1", 50.0, store="Maple Pharmacy", skip_qb_import=True)
        b = inv("Optimum", "CS1", 50.0, store="BMW", skip_qb_import=True)
        c = {"invoices": [a, b]}
        ce.apply_unlisted_skip_gate(a, "invoice", c)
        self.assertTrue(a["skip_qb_import"])

    def test_reviewer_answer_is_trusted(self):
        r = inv("Easyfast", "E1", 99.0, store="Refuah Pharmacy", skip_qb_import=True)
        ce.apply_unlisted_skip_gate(r, "invoice", {"invoices": [r], "_reviewer_answered": True})
        self.assertTrue(r["skip_qb_import"])


class Routing(PureTestCase):

    def test_excluded_vendor_flags_are_not_queued_but_payments_are(self):
        """RT-01 / RT-02."""
        self.assertFalse(ce.routes_to_review("invoice", inv("McKesson", "1", 1.0, needs_human_review=True)))
        self.assertTrue(ce.routes_to_review("payment", {"vendor_matched": "McKesson", "needs_human_review": True}))
        self.assertFalse(ce.routes_to_review("invoice", inv("Bagel Nosh", "1", 1.0, needs_human_review=True,
                                                             is_sales_order=True)))


if __name__ == "__main__":
    import unittest
    unittest.main()


class RecordlessEmailsAndIgnoredConfirmations(PureTestCase):
    """2026-10-13: 17 payments sat on Payments with no amount or date. The AI typed each email as a payment
    but extracted no payment, and the pipeline saved an EMPTY payment row that skipped every check. Those
    emails now go to review. And utility payment confirmations (NJNG, JCP&L AutoPay) are ignored when the
    vendor is ticked "Ignore payment confirmations" in Setup."""

    def setUp(self):
        super().setUp()
        k = ce._norm_vendor_std_key
        ce.VENDOR_STD = {"lookup": {k("NJNG"): "NJ Natural Gas", k("NJ Natural Gas"): "NJ Natural Gas",
                                    k("Herbal Health USA"): "Herbal Health USA"},
                         "ignored_confirmations": {"NJ Natural Gas"}, "trusted_totals": set(),
                         "vendor_names": {}, "account_names": {}, "vendor_account": {}}

    @staticmethod
    def email(etype, vendor, payments=None, **kw):
        c = {"email_type": etype, "vendor_matched": vendor, "store_matched": "3M Pharmacy",
             "needs_human_review": False, "review_reason": "", "invoices": [], "credit_memos": [],
             "payments": payments or []}
        c.update(kw)
        return c

    @staticmethod
    def pay(vendor, amount=55.0, date="2026-09-17"):
        return {"vendor_matched": vendor, "store_matched": "Lakewood Rx", "reference_number": "1",
                "payment_date": date, "payment_amount": amount, "needs_human_review": False}

    def test_recordless_payment_email_goes_to_review_not_an_empty_row(self):
        c = self.email("payment", "Herbal Health USA")         # Paid Invoice #2653: no amount anywhere
        ce.apply_review_gates(c)
        self.assertTrue(c["needs_human_review"])
        self.assertIn("looks like a payment", c["review_reason"])
        ce.apply_review_gates(c)                               # gates run twice: sentence once
        self.assertEqual(c["review_reason"].count("looks like a payment"), 1)
        rows, _ = ce.build_result_rows([{"filename": "x.eml", "classification": c}])
        self.assertEqual(rows, [], "a flagged whole email has no record row")
        self.assertTrue(ce.result_has_review({"classification": c}))

    def test_recordless_invoice_email_goes_to_review_too(self):
        c = self.email("invoice", "Herbal Health USA")
        ce.apply_review_gates(c)
        self.assertIn("looks like an invoice", c["review_reason"])

    def test_informational_email_is_left_alone(self):
        for etype in ("statement", "correspondence", "shipment_notice"):
            c = self.email(etype, "Herbal Health USA")
            ce.apply_review_gates(c)
            self.assertFalse(c["needs_human_review"], etype)

    def test_no_empty_record_even_unflagged(self):
        """Excel mode or any path where the gate didn't run: still no empty payment row."""
        c = self.email("payment", "Herbal Health USA")
        rows, _ = ce.build_result_rows([{"filename": "x.eml", "classification": c}])
        self.assertEqual([r["Record Type"] for r in rows], ["informational"])
        self.assertEqual(rows[0]["Email Type"], "payment", "the email keeps its own type")

    def test_reviewer_answer_is_trusted(self):
        c = self.email("payment", "Herbal Health USA", _reviewer_answered=True)
        ce.apply_review_gates(c)
        self.assertFalse(c["needs_human_review"], "the model followed the person; no loop")

    def test_ignored_vendor_payments_are_dropped_not_reviewed(self):
        c = self.email("payment", "NJNG", payments=[self.pay("NJNG", amount=None, date="")])
        ce.apply_review_gates(c)
        self.assertEqual(c["payments"], [])
        self.assertFalse(c["needs_human_review"], "no amount/date gate, no record-less flag")
        rows, _ = ce.build_result_rows([{"filename": "x.eml", "classification": c}])
        self.assertEqual([r["Record Type"] for r in rows], ["informational"])

    def test_recordless_payment_email_from_ignored_vendor_is_informational(self):
        c = self.email("payment", "NJNG")
        ce.apply_review_gates(c)
        self.assertFalse(c["needs_human_review"])

    def test_other_vendors_payments_are_kept(self):
        c = self.email("payment", "NJNG", payments=[self.pay("NJNG"), self.pay("Herbal Health USA")])
        ce.apply_review_gates(c)
        self.assertEqual([p["vendor_matched"] for p in c["payments"]], ["Herbal Health USA"])

    def test_exact_spelling_only(self):
        """Never a near miss: 'NJNG Services' isn't a listed spelling, so it's kept."""
        c = self.email("payment", "x", payments=[self.pay("NJNG Services")])
        ce.apply_review_gates(c)
        self.assertEqual(len(c["payments"]), 1)

    def test_nothing_ignored_without_the_setting(self):
        ce.VENDOR_STD["ignored_confirmations"] = set()
        c = self.email("payment", "NJNG", payments=[self.pay("NJNG")])
        ce.apply_review_gates(c)
        self.assertEqual(len(c["payments"]), 1)


class UnknownStoreScans(PureTestCase):
    """2026-10-15: a Westgate scan (a SureCost purchase order) came in typed "unknown" with nothing read
    from it, and was filed as informational: no one would ever have looked at it. A store scans a
    document because it's a bill or a receipt, so a scan from one of our store mailboxes (Setup ›
    Senders) that the AI can't tell goes to review, where a person dismisses it in a click if there's
    nothing to enter. The same email from anyone else stays informational."""

    REASON = "scan came from one of our stores"

    @staticmethod
    def email(etype, **kw):
        c = {"email_type": etype, "vendor_matched": "", "store_matched": "Westgate Pharmacy",
             "needs_human_review": False, "review_reason": "", "invoices": [], "credit_memos": [],
             "payments": []}
        c.update(kw)
        return c

    def test_unknown_scan_goes_to_review(self):
        for etype in ("unknown", "Unknown", "", None):
            c = self.email(etype, _from_store_scanner=True)
            ce.apply_review_gates(c)
            self.assertTrue(c["needs_human_review"], etype)
            self.assertIn(self.REASON, c["review_reason"])
            ce.apply_review_gates(c)                           # gates run twice: sentence once
            self.assertEqual(c["review_reason"].count(self.REASON), 1)
            self.assertTrue(ce.result_has_review({"classification": c}))
            rows, _ = ce.build_result_rows([{"filename": "x.eml", "classification": c}])
            self.assertEqual(rows, [], "a flagged whole email has no record row")

    def test_unknown_email_from_anyone_else_is_informational(self):
        c = self.email("unknown")
        ce.apply_review_gates(c)
        self.assertFalse(c["needs_human_review"])

    def test_a_scan_the_ai_could_type_is_left_alone(self):
        """A shipment notice (an unpriced packing slip), statement or letter: the AI told what it is."""
        for etype in ("shipment_notice", "statement", "correspondence"):
            c = self.email(etype, _from_store_scanner=True)
            ce.apply_review_gates(c)
            self.assertFalse(c["needs_human_review"], etype)

    def test_a_scan_with_a_record_is_judged_on_its_record(self):
        c = self.email("unknown", _from_store_scanner=True,
                       invoices=[inv("Henry Schein", "77", 40.0, store="Westgate Pharmacy", invoice_date="2026-10-14")])
        ce.apply_review_gates(c)
        self.assertNotIn(self.REASON, c["review_reason"] or "")

    def test_reviewer_answer_is_trusted(self):
        c = self.email("unknown", _from_store_scanner=True, _reviewer_answered=True)
        ce.apply_review_gates(c)
        self.assertFalse(c["needs_human_review"], "the model followed the person (e.g. nothing to enter); no loop")

    def test_reading_an_email_marks_scans_from_store_mailboxes(self):
        """The flag comes from the email's From address and Setup › Senders, set where each email is read."""
        ce.SENDER_STORE_MAP = {"westgatescanner@maplerx.com": "Westgate Pharmacy"}

        def run(sender):
            text = f"From: Scanner <{sender}>\nTo: invoices@maplerx.com\nSubject: Scan to E-mail Server Job\n"
            classified = {"filename": "scan.eml", "raw_response": "{}", "error": None,
                          "classification": self.email("unknown")}
            with mock.patch.object(ce, "read_email_content", return_value=(text, [], None)), \
                    mock.patch.object(ce, "_received_date_str", return_value=""), \
                    mock.patch.object(ce, "classify_with_retry", return_value=classified), \
                    mock.patch.object(ce, "assign_invoice_files", return_value=[]), \
                    mock.patch.object(ce, "upload_record_files"), \
                    mock.patch.object(ce, "save_review_files", return_value=[]):
                return ce.process_source_email(None, Path("scan.eml"), "")

        res = run("WestgateScanner@maplerx.com")
        self.assertTrue(ce.result_has_review(res))
        self.assertIn(self.REASON, res["classification"]["review_reason"])
        res = run("someone@vendor.com")
        self.assertFalse(ce.result_has_review(res))


class ExcludedVendorCarryover(PureTestCase):
    """LC-08, 2026-10-13: every run re-read review items from excluded vendors so excluded invoices clear
    themselves. A PAYMENT item never clears (payments aren't excluded), so BluPax's one-page receipt of 16
    card payments was re-read on every run and re-flagged all 16 each time: 1 -> 17 -> ~250 items."""

    def test_payment_items_are_not_re_read(self):
        rows = [{"Vendor Matched": "McKesson", "Email Type": "invoice"},
                {"Vendor Matched": "McKesson", "Email Type": "credit_memo"},
                {"Vendor Matched": "McKesson", "Email Type": "payment"},
                {"Vendor Matched": "Bagel Nosh", "Email Type": "invoice"}]
        got = ce.excluded_vendor_items_to_clear(rows)
        self.assertEqual([(r["Vendor Matched"], r["Email Type"]) for r in got],
                         [("McKesson", "invoice"), ("McKesson", "credit_memo")])
