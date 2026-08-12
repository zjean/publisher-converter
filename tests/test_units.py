"""Length conversion.

A wrong factor here does not crash — it produces a subtly wrong layout,
which is the hardest kind of defect to notice in the output.
"""

from __future__ import annotations

import unittest

from pubidml import units


class ToPointsTest(unittest.TestCase):
    def test_known_units(self):
        cases = [
            ("1in", 72.0),
            ("8.2677in", 595.2744),
            ("12pt", 12.0),
            ("3.5cm", 99.2125984),
            ("25.4mm", 72.0),
            ("1pc", 12.0),
            ("96px", 72.0),
            ("1440twip", 72.0),
            ("-2in", -144.0),
            ("+0.5in", 36.0),
            (".5in", 36.0),
            ("1e1pt", 10.0),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertAlmostEqual(units.to_points(value), expected, places=6)

    def test_a_bare_number_is_inches(self):
        # librevenge stamps double-valued properties with its RVNG_INCH
        # default, so unitless values arrive already in inches.
        self.assertAlmostEqual(units.to_points("2"), 144.0)
        self.assertAlmostEqual(units.to_points(2), 144.0)
        self.assertAlmostEqual(units.to_points(2.0), 144.0)

    def test_whitespace_is_tolerated(self):
        self.assertAlmostEqual(units.to_points("  12pt  "), 12.0)

    def test_bad_input_falls_back_to_the_default(self):
        for bad in (None, "", "abc", "12 pt extra", "in", "--3in", "12PT"):
            with self.subTest(bad=bad):
                self.assertEqual(units.to_points(bad, 99.0), 99.0)

    def test_unknown_unit_falls_back(self):
        self.assertEqual(units.to_points("5furlong", 0.0), 0.0)


class ToFloatTest(unittest.TestCase):
    def test_discards_a_bogus_unit_suffix(self):
        # libmspub passes rotation as a plain double, so librevenge stamps
        # it with RVNG_INCH and it arrives looking like "90.0000in".
        for value, expected in (("90.0000in", 90.0), ("-46in", -46.0), ("270", 270.0)):
            with self.subTest(value=value):
                self.assertAlmostEqual(units.to_float(value), expected)

    def test_bad_input_falls_back(self):
        for bad in (None, "", "abc"):
            with self.subTest(bad=bad):
                self.assertEqual(units.to_float(bad, 0.0), 0.0)


class PercentTest(unittest.TestCase):
    def test_both_notations(self):
        self.assertAlmostEqual(units.percent("50%"), 0.5)
        self.assertAlmostEqual(units.percent("0.5"), 0.5)
        self.assertAlmostEqual(units.percent("100%"), 1.0)

    def test_bad_input_falls_back(self):
        self.assertEqual(units.percent(None, 1.0), 1.0)
        self.assertEqual(units.percent("abc", 1.0), 1.0)


class FmtTest(unittest.TestCase):
    def test_trims_trailing_zeros_without_losing_the_value(self):
        self.assertEqual(units.fmt(72.0), "72")
        self.assertEqual(units.fmt(72.5), "72.5")
        self.assertEqual(units.fmt(0.0), "0")
        self.assertEqual(units.fmt(1.234567), "1.234567")
        # Negative zero survives as "-0", which IDML reads as zero. Recorded
        # rather than corrected: it is harmless and the output is stable.
        self.assertEqual(units.fmt(-0.0), "-0")

    def test_never_returns_an_empty_string(self):
        for value in (0, 0.0, -0.0, 1e-9):
            with self.subTest(value=value):
                self.assertTrue(units.fmt(value))


if __name__ == "__main__":
    unittest.main()
