"""Deterministic review gates (spec area GT). Each test is a real incident or rule."""
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
