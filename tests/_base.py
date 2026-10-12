"""Shared setup for the regression tests. Each test pins down a REAL incident (named in its
docstring) so a later change that brings the bug back fails here instead of in QuickBooks.

Pure tests never touch the database: PureTestCase switches the classifier to Excel mode
(USE_DB False), empties the vendor tables and sender map, and gives a fixed exclusion list.
Run everything from the project folder:   python -m unittest discover -s tests -v
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import classify_emails as ce  # noqa: E402

# The exclusion list the pure tests use: our own pharmacies (bills between stores) plus a wholesaler.
EXCLUDED = ["3M Pharmacy", "Refuah Pharmacy", "Westgate Pharmacy", "Madison Pharmacy", "McKesson"]


class PureTestCase(unittest.TestCase):
    """Classifier module state reset for a DB-free test, restored afterwards."""

    def setUp(self):
        self._saved = {k: getattr(ce, k) for k in
                       ("USE_DB", "VENDOR_STD", "SENDER_STORE_MAP", "STORE_SCOPE_RULES",
                        "load_excluded_vendors", "_CREDIT_MEMO_POOL")}
        ce.USE_DB = False
        ce.VENDOR_STD = {}
        ce.SENDER_STORE_MAP = {}
        ce.STORE_SCOPE_RULES = None                      # the seeded defaults
        ce.load_excluded_vendors = lambda: list(EXCLUDED)
        ce._CREDIT_MEMO_POOL = {}
        ce._DEDUPE_VENDOR_CACHE.clear()

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(ce, k, v)
        ce._DEDUPE_VENDOR_CACHE.clear()


def inv(vendor, number, amount, store="Maple Pharmacy", **kw):
    d = {"vendor_matched": vendor, "invoice_number": number, "invoice_amount": amount,
         "store_matched": store, "needs_human_review": False}
    d.update(kw)
    return d


def ledger(vendor, number, amount, source, store="Maple Pharmacy", **kw):
    d = {"vendor_matched": vendor, "invoice_number": number, "invoice_amount": amount,
         "store_matched": store, "source_file": source, "source_email_id": source}
    d.update(kw)
    return d
