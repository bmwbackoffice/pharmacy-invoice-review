"""Setup merged from 9 menu entries into 3 tabbed pages: Vendors, Stores, Rules (2026-10-12)."""
import os
import re
import unittest

import _base  # noqa: F401
import review_app
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class Groups(unittest.TestCase):

    def test_every_setup_table_is_in_a_group(self):
        grouped = {h for _, tabs in review_app.SETUP_GROUPS for h, _, also in tabs} | \
                  {a for _, tabs in review_app.SETUP_GROUPS for _, _, also in tabs for a in also}
        for table in review_app.REFERENCE_PAGES:
            self.assertIn(f"/reference/{table}", grouped, table)
        self.assertIn("/cards", grouped)

    def test_lookup(self):
        g = review_app.setup_group
        self.assertEqual(g("/reference/account_name_map")[0], "Vendors")
        self.assertEqual(g("/reference/contact_store_map")[0], "Stores")
        self.assertEqual(g("/reference/store_scope_rules")[0], "Rules")
        self.assertEqual(g("/reference/card_store_map")[2], "/cards", "Card → store highlights the Cards tab")
        self.assertIsNone(g("/review"))


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class Pages(unittest.TestCase):

    def setUp(self):
        from starlette.testclient import TestClient
        fake = {"id": -717171, "username": "zz-setup", "display_name": "zz setup", "role": "admin",
                "active": True, "password_hash": "x"}
        self._saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id)
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        self.cl = TestClient(review_app.app)
        self.cl.post("/login", data={"username": "zz-setup", "password": "x"})

    def tearDown(self):
        web_db.get_user, web_db.verify_password, web_db.get_user_by_id = self._saved

    def _tabs(self, html):
        nav = re.search(r'<nav class="setup-tabs".*?</nav>', html, re.S).group(0)
        on = re.search(r'class="on"[^>]*>([^<]+)</a>', nav).group(1)
        return nav, on

    def test_vendor_tab_page(self):
        html = self.cl.get("/reference/account_name_map").text
        nav, on = self._tabs(html)
        self.assertIn("Setup › Vendors", nav)
        self.assertEqual(on, "Account names")
        for label in ("Vendor names", "Vendor → account"):
            self.assertIn(label, nav)

    def test_cards_page_is_in_stores(self):
        nav, on = self._tabs(self.cl.get("/cards").text)
        self.assertIn("Setup › Stores", nav)
        self.assertEqual(on, "Cards")

    def test_menu_has_three_setup_entries_and_highlights_the_group(self):
        html = self.cl.get("/reference/store_scope_rules").text
        menu = html[html.index('id="topnav"'):html.index("</nav>", html.index('id="topnav"'))]
        setup = menu[menu.index(">Setup<"):]
        links = re.findall(r'<a href="(/reference/[a-z_]+|/cards)" class="([^"]*)">([^<]+)</a>', setup)
        self.assertEqual([l[2] for l in links], ["Vendors", "Stores", "Rules"])
        self.assertEqual([l[2] for l in links if "active" in l[1]], ["Rules"])

    def test_other_pages_have_no_tabs(self):
        self.assertNotIn('class="setup-tabs"', self.cl.get("/dashboard").text)


if __name__ == "__main__":
    unittest.main()
