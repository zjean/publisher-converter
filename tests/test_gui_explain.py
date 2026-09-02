"""Turning a ConversionError into something a Dutch reader can act on.

The table is matched by prefix, because most of convert.py's error
strings are f-strings: an equality lookup would miss the majority of them
and quietly leak English into a Dutch window.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from pubidml import convert
from pubidml.gui import explain


class ErrorTranslationTest(unittest.TestCase):
    def test_a_known_error_is_translated(self):
        dutch = explain.error("not a supported Publisher file (or corrupt)")
        self.assertIn("Publisher-bestand", dutch)
        self.assertNotIn("supported", dutch)

    def test_an_f_string_error_is_matched_on_its_fixed_prefix(self):
        # convert.py raises f"timed out after {PARSE_TIMEOUT_S}s", so the
        # number is not known to the table.
        dutch = explain.error(f"timed out after {convert.PARSE_TIMEOUT_S}s")
        self.assertIn("te lang", dutch)

    def test_every_f_string_error_convert_can_raise_is_matched(self):
        for message in (
            "could not launch parser: [Errno 2] No such file",
            "IDML write failed: disk full",
            "malformed event stream: unexpected token",
            "document contains no pages",
        ):
            with self.subTest(message=message):
                self.assertNotIn(
                    "zie het rapport", explain.error(message),
                    f"no mapping for {message!r}",
                )

    def test_an_unmapped_error_survives_verbatim_with_a_pointer(self):
        # Swallowing an unrecognised diagnostic to protect the Dutch
        # surface would cost the one clue a support conversation has.
        dutch = explain.error("something nobody has seen before")
        self.assertIn("something nobody has seen before", dutch)
        self.assertIn("zie het rapport", dutch)


class FailureLineTest(unittest.TestCase):
    def test_a_failure_line_names_the_file_and_the_reason(self):
        result = convert.Result(
            source=Path("/x/March 2004.pub"),
            error="not a supported Publisher file (or corrupt)",
        )
        line = explain.failure_line(result)
        self.assertTrue(line.startswith("March 2004.pub"))
        self.assertIn("beschadigd", line)


class SummaryTest(unittest.TestCase):
    def _ok(self):
        return convert.Result(
            source=Path("a.pub"), output=Path("a.idml"), pages=1, text_frames=1
        )

    def _failed(self):
        return convert.Result(source=Path("b.pub"), error="nope")

    def _review(self):
        return convert.Result(
            source=Path("c.pub"), output=Path("c.idml"), pages=1,
            text_frames=1, warnings=["iets"],
        )

    def test_the_counts_match_the_statuses(self):
        counts = explain.summary(
            [self._ok(), self._ok(), self._failed(), self._review()]
        )
        self.assertEqual(counts.ok, 2)
        self.assertEqual(counts.failed, 1)
        self.assertEqual(counts.review, 1)
        self.assertEqual(counts.total, 4)

    def test_an_empty_batch_counts_nothing_rather_than_dividing_by_zero(self):
        counts = explain.summary([])
        self.assertEqual(counts.total, 0)
        self.assertEqual(counts.ok, 0)
