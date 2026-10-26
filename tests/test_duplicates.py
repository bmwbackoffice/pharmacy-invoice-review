"""Duplicate detection (spec area DU). Each test is a real incident."""
from _base import PureTestCase, ce, inv, ledger


class InvoiceDuplicates(PureTestCase):

    def test_same_invoice_from_another_email_is_auto_ignored(self):
        """DU-01: same vendor, number, amount and store from a second email -> duplicate."""
        status, *_ = ce.check_duplicate(inv("Bagel Nosh", "280611", 50.0),
                                        [ledger("Bagel Nosh", "280611", 50.0, "a.eml")], "b.eml", "b.eml")
        self.assertEqual(status, "duplicate")

    def test_different_amount_is_a_possible_revision(self):
        """DU-01: same vendor and number, different amount -> possible (likely revised)."""
        status, reason, *_ = ce.check_duplicate(inv("Bagel Nosh", "280611", 60.0),
                                                [ledger("Bagel Nosh", "280611", 50.0, "a.eml")], "b.eml", "b.eml")
        self.assertEqual(status, "possible")
        self.assertIn("REVISED", reason)

    def test_same_email_same_amount_is_self(self):
        """DU-02: a reprocess of the same email matches itself, never a duplicate."""
        status, *_ = ce.check_duplicate(inv("Bagel Nosh", "280611", 50.0),
                                        [ledger("Bagel Nosh", "280611", 50.0, "a.eml")], "a.eml", "a.eml")
        self.assertEqual(status, "self")

    def test_same_email_different_amount_is_possible(self):
        """DU-02: a duplicate-then-correction bundled in one scan is still caught."""
        status, reason, *_ = ce.check_duplicate(inv("Bagel Nosh", "280611", 60.0),
                                                [ledger("Bagel Nosh", "280611", 50.0, "a.eml")], "a.eml", "a.eml")
        self.assertEqual(status, "possible")
        self.assertIn("same email", reason)

    def test_legal_suffix_does_not_split_a_vendor(self):
        """DU-01 / DQ-01: 'Authentic Health' and 'Authentic Health Inc' are one vendor (#1351)."""
        self.assertEqual(ce._norm_vendor_dedupe("Authentic Health Inc"), ce._norm_vendor_dedupe("Authentic Health"))
        self.assertEqual(ce._norm_vendor_dedupe("Schwartz & Company CPAs PLLC"),
                         ce._norm_vendor_dedupe("Schwartz & Company CPAs"))
        status, *_ = ce.check_duplicate(inv("Authentic Health Inc", "1351", 80.0),
                                        [ledger("Authentic Health", "1351", 80.0, "a.eml")], "b.eml", "b.eml")
        self.assertEqual(status, "duplicate")

    def test_different_store_is_a_store_conflict(self):
        """DU-03: same invoice + amount under a different store (Garden of Life #52080970)."""
        status, reason, *_ = ce.check_duplicate(
            inv("Garden of Life", "52080970", 152.83, store="3M Pharmacy"),
            [ledger("Garden of Life", "52080970", 152.83, "vendor.eml", store="Refuah Pharmacy")],
            "scan.eml", "scan.eml")
        self.assertEqual(status, "store_conflict")
        self.assertIn("this copy has it under 3M Pharmacy", reason)

    def test_store_scan_beats_vendor_send(self):
        """DU-04: a store scan of an invoice the vendor already sent is the copy kept."""
        ce.SENDER_STORE_MAP = {"maplescanner@example.com": "Maple Pharmacy"}
        status, *_ = ce.check_duplicate(
            inv("Boiron", "FI26144209", 99.0, from_address="maplescanner@example.com"),
            [ledger("Boiron", "FI26144209", 99.0, "vendor.eml", from_address="ar@boiron.example")],
            "scan1.eml", "scan1.eml")
        self.assertEqual(status, "store_preferred")

    def test_second_store_scan_is_a_plain_duplicate(self):
        """DU-04: a SECOND scan isn't 'preferred' again (A&S #17496, Boiron #FI26144209 both stayed live)."""
        ce.SENDER_STORE_MAP = {"maplescanner@example.com": "Maple Pharmacy"}
        history = [ledger("Boiron", "FI26144209", 99.0, "vendor.eml", from_address="ar@boiron.example"),
                   ledger("Boiron", "FI26144209", 99.0, "scan1.eml", from_address="maplescanner@example.com")]
        status, *_ = ce.check_duplicate(
            inv("Boiron", "FI26144209", 99.0, from_address="maplescanner@example.com"),
            history, "scan2.eml", "scan2.eml")
        self.assertEqual(status, "duplicate")


class StoreConflictHandling(PureTestCase):

    def test_store_conflict_without_an_answer_goes_to_a_person(self):
        """DU-03 (2026-10-12): the later copy no longer silently wins. Without the DB the prior can't be
        looked up, so only the new copy is flagged -- it must be suspect, not clean, and not ignored."""
        new = inv("Lantev Distributing Corp", "9GH5739", 413.30, store="3M Pharmacy")
        history = [ledger("Lantev Distributing Corp", "9GH5739", 413.30, "first.eml", store="Maple Pharmacy")]
        ce.apply_duplicate_checks([{"filename": "second.eml", "classification": {"invoices": [new]}}], history, [])
        self.assertTrue(new["is_duplicate_suspect"])
        self.assertFalse(new["duplicate_ignored"])
        self.assertEqual(len(history), 1, "the new copy must not join the ledger")


class PaymentDuplicates(PureTestCase):

    def test_one_reference_paying_two_invoices_is_two_payments(self):
        """DU-06: four Micro Merchant Systems payments shared one reference but settled different invoices."""
        prior = {"vendor_matched": "Micro Merchant Systems", "reference_number": "R100", "amount": 50.0,
                 "invoice_number": "INV-1", "source_file": "a.eml", "source_email_id": "a.eml"}
        pay = {"vendor_matched": "Micro Merchant Systems", "reference_number": "R100", "payment_amount": 75.0,
               "invoice_number": "INV-2"}
        status, *_ = ce.check_duplicate_payment(pay, [prior], "b.eml", "b.eml")
        self.assertEqual(status, "none")

    def test_applied_invoice_is_read_from_notes(self):
        """DU-06: the applied invoice written only in the notes is recovered."""
        self.assertEqual(ce._applied_invoice_from_notes("Payment applied against invoice TRINV-688744."),
                         "TRINV-688744")
        self.assertEqual(ce._applied_invoice_from_notes("Payment against invoice #TRINV-685027"), "TRINV-685027")
        self.assertIsNone(ce._applied_invoice_from_notes("Thanks for your business"))

    def test_payment_is_never_auto_ignored_on_amount_alone(self):
        """DU-06: no reference -> at most 'possible', never an auto-ignore."""
        prior = {"vendor_matched": "JCP&L", "reference_number": "", "amount": 100.0, "payment_date": "2026-09-01",
                 "source_file": "a.eml", "source_email_id": "a.eml"}
        pay = {"vendor_matched": "JCP&L", "reference_number": "", "payment_amount": 100.0, "payment_date": "2026-09-01"}
        status, *_ = ce.check_duplicate_payment(pay, [prior], "b.eml", "b.eml")
        self.assertEqual(status, "possible")


class CreditMemoDuplicates(PureTestCase):
    """DU-15: credit memos have no ledger; Prime Packaging #AR092424 was recorded twice."""

    def _cm(self, amount=1200.0, **kw):
        d = {"vendor_matched": "Prime Packaging", "credit_memo_number": "AR092424", "credit_amount": amount,
             "needs_human_review": False}
        d.update(kw)
        return {"credit_memos": [d], "_reviewer_answered": False}

    def _seed(self, email="first.eml", entered=True):
        key = (ce._dedupe_vendor_key("Prime Packaging"), ce._norm("AR092424"))
        ce._CREDIT_MEMO_POOL = {key: [{"id": 104, "vendor_matched": "Prime Packaging",
                                       "credit_memo_number": "AR092424", "amount": 1200.0,
                                       "email_filename": email, "entered_in_qb": entered}]}

    def test_second_copy_goes_to_review(self):
        self._seed()
        c = self._cm()
        ce.apply_credit_memo_duplicate_gate(c, "second.eml")
        cm = c["credit_memos"][0]
        self.assertTrue(cm["needs_human_review"])
        self.assertIn("second copy", cm["review_reason"])
        self.assertIn("#104", cm["review_reason"])
        self.assertIn("already entered in QuickBooks", cm["review_reason"])

    def test_different_amount_reads_as_revision(self):
        self._seed()
        c = self._cm(amount=1100.0)
        ce.apply_credit_memo_duplicate_gate(c, "second.eml")
        self.assertIn("revised credit memo", c["credit_memos"][0]["review_reason"])

    def test_same_email_does_not_match_itself(self):
        self._seed(email="first.eml")
        c = self._cm()
        ce.apply_credit_memo_duplicate_gate(c, "first.eml")
        self.assertFalse(c["credit_memos"][0]["needs_human_review"])

    def test_reviewer_answer_does_not_loop(self):
        self._seed()
        c = self._cm()
        c["_reviewer_answered"] = True
        ce.apply_credit_memo_duplicate_gate(c, "second.eml")
        self.assertFalse(c["credit_memos"][0]["needs_human_review"])

    def test_excluded_vendor_is_skipped(self):
        """7 of the 8 live same-number pairs were McKesson etc. -- never entered, so no review."""
        key = (ce._dedupe_vendor_key("McKesson"), ce._norm("7650750838"))
        ce._CREDIT_MEMO_POOL = {key: [{"id": 3, "vendor_matched": "McKesson", "credit_memo_number": "7650750838",
                                       "amount": 95.04, "email_filename": "a.eml", "entered_in_qb": False}]}
        c = {"credit_memos": [{"vendor_matched": "McKesson", "credit_memo_number": "7650750838",
                               "credit_amount": 95.04, "needs_human_review": False}], "_reviewer_answered": False}
        ce.apply_credit_memo_duplicate_gate(c, "b.eml")
        self.assertFalse(c["credit_memos"][0]["needs_human_review"])

    def test_two_copies_in_one_run(self):
        a, b = self._cm(), self._cm()
        ce.apply_credit_memo_duplicate_gate(a, "one.eml")
        ce.apply_credit_memo_duplicate_gate(b, "two.eml")
        self.assertFalse(a["credit_memos"][0]["needs_human_review"])
        self.assertTrue(b["credit_memos"][0]["needs_human_review"])

    def test_sentence_appears_once(self):
        """GT-10: running a gate twice never doubles its sentence."""
        self._seed()
        c = self._cm()
        ce.apply_credit_memo_duplicate_gate(c, "second.eml")
        ce.apply_credit_memo_duplicate_gate(c, "second.eml")
        self.assertEqual(c["credit_memos"][0]["review_reason"].count("Credit memo #"), 1)


class MisreadNumbers(PureTestCase):
    """DU-16, 2026-10-15: numbers 'one character apart' for the misread-number check, from the waiting copies
    whose twins were already entered in QB."""

    def test_one_character_apart(self):
        for a, b in (("797203", "797208"),            # Maxi Health: one digit changed
                     ("3551", "35551"),               # Masterko: one digit dropped
                     ("9304241", "93G4241"),          # Lantev: G read as 0
                     ("8610704", "86I0704"),          # Lantev: I read as 1
                     ("R204977-IN", "R204977"),       # Reisman's: a lettered suffix
                     ("INV12345", "12345")):          # a lettered prefix
            self.assertTrue(ce._numbers_one_apart(a, b), (a, b))
            self.assertTrue(ce._numbers_one_apart(b, a), (b, a))

    def test_different_numbers(self):
        for a, b in (("7984", "7898"),                # Talent Bridging: weekly bills, same amount
                     ("FI24150750", "FI2614901"),     # Boiron: two bills
                     ("12345", "12345678"),           # extra digits, no letter
                     ("123", "124"),                  # too short to judge
                     ("A-1001", "a1001"),             # the same number (the normal check's job)
                     ("", "1234")):
            self.assertFalse(ce._numbers_one_apart(a, b), (a, b))


if __name__ == "__main__":
    import unittest
    unittest.main()
