"""QuickBooks number matching (QB-07). A false match marks the wrong bill entered."""
import itertools
import unittest

import _base  # noqa: F401  (puts the project on sys.path)
import qb_match as qm

MATCH = [
    ("INV-000146", "146"),               # vendor prefix + leading zeros
    ("W00123", "123"),
    ("1618282-IN", "1618282"),           # suffix affix
    ("INV/2026/47830", "INV/202647830"), # punctuation inside one number
    ("161230-9-IN", "1612309"),
    ("86I0721", "8610721"),              # Lantev OCR misread I -> 1
    ("U13944", "U-13944"),
    ("R204825", "204825"),
]
NO_MATCH = [
    ("INV/2026/07773", "INV/2026/0773"), # Zahler: genuinely different
    ("2026/146", "146"),                 # year prefix isn't cosmetic
    ("SUB2026M08", "SUB2026M09"),        # monthly bill: letters split the digits (2026-10-12)
    ("S1234A01", "S1234A02"),
]


class NumberMatching(unittest.TestCase):

    def test_cosmetic_differences_match(self):
        for a, b in MATCH:
            self.assertTrue(qm._num_eq(a, b), (a, b))

    def test_different_numbers_do_not_match(self):
        for a, b in NO_MATCH:
            self.assertFalse(qm._num_eq(a, b), (a, b))

    def test_blank_never_matches(self):
        self.assertFalse(qm._num_eq("", ""))
        self.assertFalse(qm._num_eq(None, "146"))

    def test_equal_numbers_share_an_index_bucket(self):
        """_num_bucket must put any two numbers _num_eq calls equal in the same bucket."""
        nums = [n for pair in MATCH + NO_MATCH for n in pair]
        for a, b in itertools.product(nums, nums):
            if qm._num_eq(a, b):
                self.assertEqual(qm._num_bucket(a), qm._num_bucket(b), (a, b))


if __name__ == "__main__":
    unittest.main()
