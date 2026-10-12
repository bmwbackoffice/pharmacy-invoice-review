"""Read-only users (role 'viewer') and the Payments search fields (2026-10-12)."""
import os
import unittest

import _base  # noqa: F401
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class ReadOnlyRole(unittest.TestCase):
    """A read-only user can open pages but every change is refused before it reaches its route."""

    def _login(self, role):
        import review_app
        from starlette.testclient import TestClient
        fake = {"id": -515151, "username": "zz-viewer", "display_name": "zz viewer", "role": role,
                "active": True, "password_hash": "x"}
        self._saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id,
                       web_db.submit_review_answer, web_db.set_record_ignored, web_db.get_user_prefs,
                       web_db.set_user_pref)
        self.calls = []
        web_db.get_user = lambda username: dict(fake)
        web_db.verify_password = lambda pw, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        web_db.get_user_prefs = lambda uid: {}                       # never touch real prefs
        web_db.set_user_pref = lambda *a, **k: None
        web_db.submit_review_answer = lambda *a, **k: self.calls.append("answer")      # tripwires
        web_db.set_record_ignored = lambda *a, **k: self.calls.append("ignore")
        cl = TestClient(review_app.app)
        r = cl.post("/login", data={"username": "zz-viewer", "password": "x"}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        return cl

    def tearDown(self):
        (web_db.get_user, web_db.verify_password, web_db.get_user_by_id, web_db.submit_review_answer,
         web_db.set_record_ignored, web_db.get_user_prefs, web_db.set_user_pref) = self._saved

    def test_viewer_can_look(self):
        cl = self._login("viewer")
        for path in ("/dashboard", "/review", "/payments", "/qb", "/browse"):
            r = cl.get(path)
            self.assertEqual(r.status_code, 200, path)
        html = cl.get("/payments").text
        self.assertIn("Read-only access", html)
        self.assertIn('body.read-only form[method="post" i]:not(.pref-form)', html)
        self.assertIn("body.read-only .entered-btn", html)
        self.assertIn("(read-only)", html)

    def test_viewer_changes_are_refused(self):
        cl = self._login("viewer")
        r = cl.post("/review/1/answer", data={"answer": "x"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        self.assertIn("read-only access", r.text)
        r = cl.post("/records/payment/1/ignore", data={"ignored": "1"}, follow_redirects=False)
        self.assertEqual(r.status_code, 403)
        r = cl.post("/review/1/draft-learning", json={"rough": "x"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("error", r.json())
        self.assertEqual(self.calls, [], "no change reached the database layer")

    def test_reviewer_is_not_blocked(self):
        cl = self._login("reviewer")
        cl.post("/review/1/answer", data={"answer": "x"}, follow_redirects=False)
        self.assertEqual(self.calls, ["answer"], "a reviewer's change goes through")

    def test_admin_can_create_a_read_only_user(self):
        import review_app
        self.assertIn("viewer", web_db.ROLES)
        cl = self._login("admin")
        html = cl.get("/admin/users").text
        self.assertIn('<option value="viewer">read-only</option>', html)
        self.assertIn("/chat/ask", review_app._READ_ONLY_ALLOWED)


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class PaymentSearch(unittest.TestCase):
    """Each new filter finds a real payment and the wrong value excludes it. Read-only."""

    @classmethod
    def setUpClass(cls):
        conn = web_db.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT id, vendor_matched, reference_number, amount, date_added FROM payments "
                        "WHERE duplicate_ignored IS NOT TRUE AND skip_qb_import IS NOT TRUE "
                        "AND duplicate_suspect IS NOT TRUE AND length(reference_number) >= 4 AND amount > 0 "
                        "AND length(vendor_matched) >= 4 ORDER BY id DESC LIMIT 1")
            cls.p = cur.fetchone()
        conn.close()

    def _ids(self, **kw):
        return {r["record_id"] for r in web_db.list_payments(**kw)}

    def test_each_filter(self):
        pid, vendor, ref, amount, added = self.p
        amount = float(amount)
        self.assertIn(pid, self._ids(vendor=vendor[1:-1].lower()))
        self.assertIn(pid, self._ids(reference="-".join(ref)))            # punctuation ignored
        self.assertIn(pid, self._ids(q=ref))
        self.assertIn(pid, self._ids(q=vendor.upper()))
        self.assertIn(pid, self._ids(amount_min=f"${amount:,.2f}", amount_max=f"{amount:.2f}"))
        self.assertNotIn(pid, self._ids(amount_min=str(amount + 1)))
        self.assertIn(pid, self._ids(added_from=str(added), added_to=str(added)))
        self.assertNotIn(pid, self._ids(reference="zzz-no-such-ref-999"))
        self.assertEqual(self._ids(amount_min="not a number"), self._ids(), "an unreadable amount is ignored")


if __name__ == "__main__":
    unittest.main()
