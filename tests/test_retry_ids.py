"""--retry-id takes several review items at once (2026-10-14): the 10/14 learnings re-check stopped at its
45-minute cap with 30 items unread, four of them still carrying the old "sales order, processed as an invoice per
policy" flag. One run re-reads them all: --retry-id=3698,3699,3700,3701.
"""
import unittest

from _base import ce


class RetryIds(unittest.TestCase):
    def test_one_id(self):
        self.assertEqual(ce._parse_retry_ids("3698"), {"3698"})

    def test_several_ids_spaces_and_hashes(self):
        self.assertEqual(ce._parse_retry_ids("3698, #3699 ,3700,,3701"), {"3698", "3699", "3700", "3701"})

    def test_none_or_blank(self):
        self.assertEqual(ce._parse_retry_ids(None), set())
        self.assertEqual(ce._parse_retry_ids(" , #"), set())


if __name__ == "__main__":
    unittest.main()
