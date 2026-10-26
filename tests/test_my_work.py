"""My work inbox (2026-10-12): one list across the queues, built from the same helpers they use."""
import os
import unittest
from datetime import datetime, timedelta, timezone

import _base  # noqa: F401
import review_app
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))
NOW = datetime.now(timezone.utc)


class Inbox(unittest.TestCase):
    """Fixed sample data in place of the queue helpers, so the test doesn't depend on today's queue."""

    def setUp(self):
        names = ("list_open_reviews", "list_open_statement_exceptions", "list_duplicate_candidates",
                 "qb_list_candidates", "list_flags", "list_change_requests", "dashboard_counts",
                 "list_correspondence_follow_ups", "list_qb_corrections")
        self._saved = {n: getattr(web_db, n) for n in names}
        web_db.list_open_reviews = lambda: [
            {"id": 1, "vendor_matched": "Bagel Nosh", "invoice_number": "280611", "amount": 50,
             "question": "Store could not be identified — needs a human", "flagged_at": NOW - timedelta(days=1),
             "assigned_to": "amy"},
            {"id": 2, "vendor_matched": "Boiron", "invoice_number": "9", "amount": None,
             "question": "Possible revision", "flagged_at": NOW - timedelta(days=5), "assigned_to": None},
            {"id": 3, "vendor_matched": "Lantev", "invoice_number": "7", "amount": 1,
             "question": "x", "flagged_at": NOW - timedelta(days=2), "assigned_to": "yumi"},
            {"id": 4, "vendor_matched": "Costco", "invoice_number": "5", "amount": 2,
             "question": "No invoice date found", "flagged_at": NOW - timedelta(days=3), "assigned_to": "amy"},
        ]
        web_db.list_open_statement_exceptions = lambda: [
            {"vendor_matched": "Nutri-Supreme", "status_label": "2 missing invoices",
             "flagged_at": NOW - timedelta(days=42), "assigned_to": None}]
        web_db.list_duplicate_candidates = lambda: [
            {"records": [{"vendor_matched": "Prodermix", "number": "104507"}, {"vendor_matched": "Prodermix"}]}]
        web_db.qb_list_candidates = lambda status="pending": []
        web_db.list_flags = lambda **k: [{"status": "pending", "note": "wrong store", "submitted_by": "yumi",
                                          "created_at": NOW}, {"status": "dismissed", "note": "old"}]
        web_db.list_change_requests = lambda **k: [{"status": "pending", "title": "Export to PDF",
                                                    "submitted_by": "amy", "created_at": NOW}]
        web_db.dashboard_counts = lambda: {"qb_invoices": 170, "qb_credits": 9, "payments": 353}
        # Sources My work gained later (correspondence follow-ups 10/19, QB corrections 10/21): empty here,
        # so today's live data doesn't leak into the counts.
        web_db.list_correspondence_follow_ups = lambda *a, **k: []
        web_db.list_qb_corrections = lambda *a, **k: []

    def tearDown(self):
        for n, f in self._saved.items():
            setattr(web_db, n, f)

    def test_placement_and_order(self):
        w = review_app.my_work({"username": "amy", "role": "reviewer"})
        self.assertEqual([i["href"] for i in w["mine"]], ["/review/4", "/review/1"], "amy's two, oldest first")
        self.assertEqual(w["others"], 1, "yumi's item is counted, not listed")
        kinds = [i["kind"] for i in w["anyone"]]
        self.assertEqual(kinds[0], "Statement", "42 days old comes first")
        self.assertIn("Review", kinds)
        self.assertIn("Duplicate", kinds)
        self.assertNotIn("Flag", kinds, "flags and requests are for admins")
        self.assertEqual([(e["count"]) for e in w["entry"]], [179, 353])

    def test_quick_fix_chip_and_text(self):
        w = review_app.my_work({"username": "amy", "role": "reviewer"})
        by_href = {i["href"]: i for i in w["mine"] + w["anyone"]}
        self.assertTrue(by_href["/review/1"]["quick_fix"])
        self.assertTrue(by_href["/review/4"]["quick_fix"])
        self.assertFalse(by_href["/review/2"]["quick_fix"])
        self.assertEqual(by_href["/review/1"]["title"], "Bagel Nosh #280611 · $50.00")
        self.assertEqual(by_href["/review/1"]["age"], "1 day")

    def test_admin_also_sees_open_flags_and_requests(self):
        w = review_app.my_work({"username": "joel", "role": "admin"})
        kinds = [i["kind"] for i in w["anyone"]]
        self.assertEqual(kinds.count("Flag"), 1, "only the pending flag, not the dismissed one")
        self.assertEqual(kinds.count("Request"), 1)
        self.assertEqual(w["others"], 3, "nothing is joel's")

    def test_age_label(self):
        self.assertEqual(review_app._age_label(None), "")
        self.assertEqual(review_app._age_label(NOW - timedelta(minutes=5)), "new")
        self.assertEqual(review_app._age_label(NOW - timedelta(hours=3)), "3 hr")
        self.assertEqual(review_app._age_label(NOW - timedelta(days=2, hours=1)), "2 days")


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class Page(unittest.TestCase):

    def test_page_and_menu(self):
        from starlette.testclient import TestClient
        fake = {"id": -818181, "username": "zz-inbox", "display_name": "zz", "role": "reviewer",
                "active": True, "password_hash": "x"}
        saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id)
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        try:
            cl = TestClient(review_app.app)
            cl.post("/login", data={"username": "zz-inbox", "password": "x"})
            r = cl.get("/my-work")
            self.assertEqual(r.status_code, 200)
            for text in ("Assigned to you", "Waiting for anyone", "Ready for QuickBooks"):
                self.assertIn(text, r.text)
            menu = r.text[r.text.index('id="topnav"'):]
            self.assertLess(menu.index(">My work<"), menu.index(">Dashboard<"), "My work is first in the menu")
        finally:
            web_db.get_user, web_db.verify_password, web_db.get_user_by_id = saved


if __name__ == "__main__":
    unittest.main()
