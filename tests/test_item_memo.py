"""Short item memo first in the QuickBooks memo, for vendors ticked "Add item memo" (2026-10-30, Joel: "can we
have short memo's added for Promotise invoices? ... put the memo first and then the separator we use for the 2
links, then put the links as usual")."""
import unittest

import _base  # noqa: F401
import classify_emails as ce
import review_app
import web_db


class MemoOrder(unittest.TestCase):
    def test_item_memo_first(self):
        self.assertEqual(review_app._qb_memo("http://h/l/A", "http://h/l/B", "Classic Click Pen"),
                         "Classic Click Pen      <<|>>      http://h/l/A      <<|>>      E:   http://h/l/B")

    def test_no_item_memo_unchanged(self):
        self.assertEqual(review_app._qb_memo("http://h/l/A", "http://h/l/B", ""),
                         review_app._qb_memo("http://h/l/A", "http://h/l/B"))

    def test_memo_cell_uses_row_item_memo(self):
        r = {"file_path": "", "gmail_link": "https://mail.google.com/x", "item_memo": "Lab Coat"}
        self.assertTrue(review_app._qb_cell_value("Memo", r, lambda u: u).startswith("Lab Coat      <<|>>      "))


class CleanItemMemo(unittest.TestCase):
    def test_cleaning(self):
        self.assertEqual(ce._clean_item_memo("  Lab   Coat "), "Lab Coat")
        self.assertEqual(ce._clean_item_memo(None), "")
        self.assertEqual(ce._clean_item_memo("null"), "")
        self.assertNotIn("<<|>>", ce._clean_item_memo("Pens <<|>> Bags"))
        self.assertLessEqual(len(ce._clean_item_memo("x" * 200)), 80)

    def test_prompt_asks_for_it(self):
        with open(ce.__file__, encoding="utf-8") as f:
            self.assertIn('"item_memo": "SHORT label of what was bought', f.read())


class SetupColumn(unittest.TestCase):
    def test_vendor_names_has_the_box(self):
        ref = web_db._REF["vendor_name_map"]
        self.assertIn("add_item_memo", ref["cols"])
        self.assertIn("add_item_memo", ref["bool_cols"])


if __name__ == "__main__":
    unittest.main()
