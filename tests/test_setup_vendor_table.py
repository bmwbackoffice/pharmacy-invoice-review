"""Setup › Vendor names fits the window, has light borders and filters by a checkbox (2026-10-27, Joel: "Not all
columns fit on one page ... we should be able to filter. For example, I want to see all excluded from QuickBooks")."""
import os
import re
import unittest

import _base  # noqa: F401
import review_app
import web_db

HAVE_DB = bool(os.environ.get("DATABASE_URL"))


class SoftBreaks(unittest.TestCase):

    def test_long_names_can_wrap_at_their_separators_only(self):
        sb = review_app._soft_breaks
        self.assertEqual(str(sb("Administration:Insurance:63300")), "Administration:<wbr>Insurance:<wbr>63300")
        self.assertEqual(str(sb("CUSTOMIZEDSTICKERS.COM")), "CUSTOMIZEDSTICKERS.<wbr>COM")
        self.assertEqual(str(sb("Prestige Sales Inc.)")), "Prestige Sales Inc.)", "no break before a closing bracket")
        self.assertEqual(str(sb("<b>x</b>")), "&lt;b&gt;x&lt;/<wbr>b&gt;", "still escaped")
        self.assertEqual(sb(None), "")


def _vendor_row(rid, name, **kw):
    row = {c["name"]: (False if c["type"] == "checkbox" else None)
           for c in review_app.REFERENCE_PAGES["vendor_name_map"]["columns"]}
    row.update(id=rid, standard_vendor_name=name, created_by="zz", created_at=None, edited_by=None, edited_at=None)
    row.update(kw)
    return row


@unittest.skipUnless(HAVE_DB, "DATABASE_URL not set")
class VendorNamesPage(unittest.TestCase):

    def setUp(self):
        from starlette.testclient import TestClient
        fake = {"id": -717172, "username": "zz-setup", "display_name": "zz setup", "role": "admin",
                "active": True, "password_hash": "x"}
        self._saved = (web_db.get_user, web_db.verify_password, web_db.get_user_by_id, web_db.list_reference)
        web_db.get_user = lambda u: dict(fake)
        web_db.verify_password = lambda p, h: True
        web_db.get_user_by_id = lambda uid: dict(fake) if uid == fake["id"] else None
        self.rows = {
            "vendor_name_map": [
                _vendor_row(1, "Zz Excluded Wholesaler", exclude_from_qb=True, sales_tax_ok=True),
                _vendor_row(2, "Zz Plain Vendor", name_maple="Zz Plain Vendor", name_refuah="ZZ PLAIN:VENDOR"),
            ],
            "stores": [{"id": 1, "store_name": "Zz Store", "back_office": True, "created_by": "zz"},
                       {"id": 2, "store_name": "Zz Shop", "back_office": False, "created_by": "zz"}],
        }
        web_db.list_reference = lambda table: [dict(r) for r in self.rows.get(table, [])]
        self.cl = TestClient(review_app.app)
        self.cl.post("/login", data={"username": "zz-setup", "password": "x"})

    def tearDown(self):
        web_db.get_user, web_db.verify_password, web_db.get_user_by_id, web_db.list_reference = self._saved

    def _row(self, html, name):
        start = html.index(name, html.index("<tbody>"))
        return html[html.rindex("<tr", 0, start):html.index("</tr>", start)]

    def test_the_five_checkboxes_are_one_settings_column(self):
        html = self.cl.get("/reference/vendor_name_map").text
        head = html[html.index('<table class="reftbl'):html.index("</thead>")]
        self.assertIn('<th class="c-flags">Settings</th>', head)
        self.assertNotIn("c-checkbox", head)
        row = self._row(html, "Zz Excluded Wholesaler")
        self.assertIn('data-flags="exclude_from_qb sales_tax_ok"', row)
        self.assertIn('<span class="rv rv-flag f-exclude_from_qb" data-col="exclude_from_qb">Exclude from QB</span>', row)
        self.assertNotIn('data-col="trust_line_item_totals">', row.split("<template")[0], "unticked: no label")
        self.assertEqual(len(re.findall(r'<template data-col="[a-z_]+"><label class="flag-edit"><input type="checkbox"', row)), 5,
                         "Edit still has all five boxes")
        self.assertIn('class="ref-del"', row, "Delete shows while editing")

    def test_filter_buttons_for_each_checkbox(self):
        html = self.cl.get("/reference/vendor_name_map").text
        self.assertEqual(re.findall(r'class="fchip" data-flag="([a-z_]+)"', html),
                         ["exclude_from_qb", "exclude_from_statement_recon", "trust_line_item_totals",
                          "ignore_payment_confirmations", "sales_tax_ok"])
        self.assertIn('data-flags=""', self._row(html, "Zz Plain Vendor"))

    def test_a_store_spelling_that_is_the_standard_name_shows_as_same(self):
        row = self._row(self.cl.get("/reference/vendor_name_map").text, "Zz Plain Vendor")
        self.assertIn('<span class="rv rv-same" title="Zz Plain Vendor">same</span>', row)
        self.assertIn('<span class="rv">ZZ PLAIN:<wbr>VENDOR</span>', row, "a different spelling shows as written")
        self.assertIn('<input name="name_maple" value="Zz Plain Vendor"', row, "Edit has the real spelling")

    def test_a_page_with_one_checkbox_keeps_its_column(self):
        html = self.cl.get("/reference/stores").text
        self.assertIn('<th class="c-checkbox">', html)
        self.assertEqual(re.findall(r'class="fchip" data-flag="([a-z_]+)"', html), ["back_office"])
        self.assertIn('data-flags="back_office"', self._row(html, "Zz Store"))


if __name__ == "__main__":
    unittest.main()
