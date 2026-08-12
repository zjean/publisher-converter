"""Code-page repair.

This is the highest-blast-radius module in the project: a wrong guess
destroys text that was already correct, and the user has no way back
short of reconverting. The tuned constants are otherwise unfalsifiable —
nothing else tells you if a tweak breaks them — so both directions are
pinned here: text that must be repaired, and text that must be left
byte-identical.
"""

from __future__ import annotations

import unittest

from pubidml import model, textrepair


def mojibake(text: str, codec: str) -> str:
    """Reproduce libmspub's bug: encode with `codec`, decode as latin-1."""
    return text.encode(codec).decode("latin-1")


def document_with(*texts: str) -> model.Document:
    document = model.Document(pages=[model.Page()])
    frame = model.TextFrame()
    paragraph = model.Paragraph()
    for text in texts:
        paragraph.spans.append(model.Span(text=text))
    frame.story.paragraphs.append(paragraph)
    document.pages[0].items.append(frame)
    return document


RUSSIAN = "Русский текст с достаточной длиной для уверенного определения"
GREEK = "Ελληνικό κείμενο με αρκετό μήκος για ασφαλή αναγνώριση της κωδικοσελίδας"


class MustRepairTest(unittest.TestCase):
    def test_detects_cyrillic(self):
        self.assertEqual(textrepair.detect([mojibake(RUSSIAN, "cp1251")]), "cp1251")

    def test_detects_greek(self):
        # Greek scores 0.75 under cp1253 against 0.55 under the wrong
        # cp1251, which is why the accept margin exists at all.
        self.assertEqual(textrepair.detect([mojibake(GREEK, "cp1253")]), "cp1253")

    def test_round_trips_the_original_text(self):
        for original, codec in ((RUSSIAN, "cp1251"), (GREEK, "cp1253")):
            with self.subTest(codec=codec):
                damaged = mojibake(original, codec)
                self.assertNotEqual(damaged, original)
                self.assertEqual(textrepair.repair(damaged, codec), original)


class MustNotTouchTest(unittest.TestCase):
    """Heavily accented Latin text can drift past the ratio threshold alone."""

    SAFE = [
        "Français: être, où, déjà, ça, tête, naïve, Noël, œuvre, çà et là",
        "Deutsch: Grüße, Straße, Fußgängerübergang, schön, größer, Käse",
        "Português: informação, coração, São Paulo, avô, pães, você",
        "Español: mañana, corazón, niño, pingüino, ¿qué?, ¡olé!",
        "Plain ASCII text with no accents at all whatsoever.",
        "",
        "   ",
        "123 456 789",
    ]

    def test_detect_declines(self):
        for sample in self.SAFE:
            with self.subTest(sample=sample[:24]):
                self.assertIsNone(textrepair.detect([sample]))

    def test_repair_document_leaves_it_byte_identical(self):
        for sample in self.SAFE:
            with self.subTest(sample=sample[:24]):
                document = document_with(sample)
                textrepair.repair_document(document, "auto")
                self.assertEqual(
                    document.pages[0].items[0].story.paragraphs[0].spans[0].text, sample
                )

    def test_already_repaired_text_is_not_repaired_again(self):
        # Real Cyrillic does not encode to latin-1, so the premise fails and
        # the detector must decline rather than mangle it.
        self.assertIsNone(textrepair.detect([RUSSIAN]))


class ForcedCodecTest(unittest.TestCase):
    """An explicit --codepage must report honestly what it actually did."""

    def test_unknown_codec_reports_that_nothing_happened(self):
        document = document_with(mojibake(RUSSIAN, "cp1251"))
        result = textrepair.repair_document(document, "cp1251x")
        self.assertIsNone(result)
        self.assertTrue(any("left as-is" in w for w in document.warnings))
        self.assertFalse(
            any("re-decoded" in w for w in document.warnings),
            "must not claim a repair it did not perform",
        )

    def test_a_codec_that_changes_nothing_reports_that_too(self):
        document = document_with("Plain ASCII text")
        result = textrepair.repair_document(document, "cp1251")
        self.assertIsNone(result)
        self.assertTrue(any("left as-is" in w for w in document.warnings))

    def test_a_working_forced_codec_repairs_and_says_so(self):
        document = document_with(mojibake(RUSSIAN, "cp1251"))
        result = textrepair.repair_document(document, "cp1251")
        self.assertEqual(result, "cp1251")
        self.assertEqual(
            document.pages[0].items[0].story.paragraphs[0].spans[0].text, RUSSIAN
        )
        self.assertTrue(any("re-decoded as cp1251" in w for w in document.warnings))

    def test_none_disables_repair_entirely(self):
        damaged = mojibake(RUSSIAN, "cp1251")
        document = document_with(damaged)
        self.assertIsNone(textrepair.repair_document(document, None))
        self.assertEqual(
            document.pages[0].items[0].story.paragraphs[0].spans[0].text, damaged
        )
        self.assertEqual(document.warnings, [])


class EdgeCaseTest(unittest.TestCase):
    def test_document_with_no_text_is_handled(self):
        document = model.Document(pages=[model.Page()])
        self.assertIsNone(textrepair.repair_document(document, "auto"))

    def test_repair_reaches_text_inside_a_group(self):
        document = model.Document(pages=[model.Page()])
        frame = model.TextFrame()
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text=mojibake(RUSSIAN, "cp1251")))
        frame.story.paragraphs.append(paragraph)
        document.pages[0].items.append(model.Group(children=[frame]))

        self.assertEqual(textrepair.repair_document(document, "auto"), "cp1251")
        self.assertEqual(frame.story.paragraphs[0].spans[0].text, RUSSIAN)


if __name__ == "__main__":
    unittest.main()
