"""Metafile classification and helper-tool resolution.

`inspect` decides whether the tool warns or stays quiet about lost
artwork, so it governs whether the report can be trusted at all.
"""

from __future__ import annotations

import os
import struct
import tempfile
import unittest
from pathlib import Path

from pubidml import metafile

EMF_SIGNATURE = 0x464D4520


def emf(*records: tuple) -> bytes:
    """Build a minimal but structurally valid EMF from (type, size) records."""
    header = bytearray(88)
    struct.pack_into("<II", header, 0, 1, 88)          # EMR_HEADER, header size
    struct.pack_into("<4i", header, 24, 0, 0, 100, 100)  # rclFrame
    struct.pack_into("<I", header, 40, EMF_SIGNATURE)
    struct.pack_into("<I", header, 52, len(records) + 1)  # nRecords
    body = bytearray()
    for record_type, size in records:
        chunk = bytearray(size)
        struct.pack_into("<II", chunk, 0, record_type, size)
        body += chunk
    return bytes(header + body)


PUBLISHER_STUB = emf((14, 20))                    # header + EMR_EOF only
REAL_ARTWORK = emf((54, 40), (55, 40), (14, 20))  # two drawing records + EOF


class InspectTest(unittest.TestCase):
    def test_publishers_empty_placeholder_is_recognised_as_empty(self):
        info = metafile.inspect(PUBLISHER_STUB)
        self.assertEqual(info.kind, "emf")
        self.assertTrue(info.valid)
        self.assertEqual(info.drawing_records, 0)
        self.assertTrue(info.is_empty)

    def test_real_artwork_is_not_empty(self):
        info = metafile.inspect(REAL_ARTWORK)
        self.assertTrue(info.valid)
        self.assertEqual(info.drawing_records, 2)
        self.assertFalse(info.is_empty)

    def test_state_only_records_do_not_count_as_drawing(self):
        # SAVEDC/SELECTOBJECT/CREATEPEN/RESTOREDC paint nothing; a metafile
        # of nothing but these is still a placeholder.
        info = metafile.inspect(emf((17, 12), (21, 12), (22, 28), (18, 12), (14, 20)))
        self.assertEqual(info.drawing_records, 0)
        self.assertTrue(info.is_empty)

    def test_garbage_is_invalid_and_therefore_empty(self):
        for data in (b"", b"hello", b"\x00" * 200, b"\xff" * 88):
            with self.subTest(length=len(data)):
                info = metafile.inspect(data)
                self.assertFalse(info.valid)
                self.assertTrue(info.is_empty)

    def test_a_zero_size_record_does_not_hang(self):
        # A record claiming size 0 would loop forever without the guard.
        broken = bytearray(emf((54, 40), (14, 20)))
        struct.pack_into("<I", broken, 88 + 4, 0)
        info = metafile.inspect(bytes(broken))
        self.assertTrue(info.valid)

    def test_placeable_wmf_is_detected(self):
        data = struct.pack("<I", metafile.WMF_PLACEABLE_KEY) + bytes(60)
        self.assertEqual(metafile.inspect(data).kind, "wmf")


class ToolResolutionTest(unittest.TestCase):
    """A helper found in the working directory is not a system tool.

    On Windows shutil.which searches the current directory before PATH, so
    an archive shipping its own magick.exe next to the .pub files would get
    it executed by anyone converting in place.
    """

    def test_a_tool_in_the_working_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as work:
            planted = Path(work) / "pub2idml-fake-tool"
            planted.write_text("#!/bin/sh\nexit 0\n")
            planted.chmod(0o755)

            previous = os.getcwd()
            os.chdir(work)
            try:
                # Reachable only because it sits in the working directory.
                self.assertIsNone(metafile._resolve_tool("pub2idml-fake-tool"))
                # And still refused when the working directory is on PATH.
                os.environ["PATH"] = work + os.pathsep + os.environ["PATH"]
                self.assertIsNone(metafile._resolve_tool("pub2idml-fake-tool"))
            finally:
                os.chdir(previous)

    def test_a_real_system_tool_still_resolves(self):
        found = metafile._resolve_tool("sh" if os.name != "nt" else "cmd")
        self.assertIsNotNone(found)
        self.assertTrue(os.path.isabs(found))

    def test_a_missing_tool_is_none(self):
        self.assertIsNone(metafile._resolve_tool("definitely-not-installed-xyzzy"))


class RasterisationTest(unittest.TestCase):
    def test_an_empty_metafile_is_never_sent_to_a_converter(self):
        self.assertIsNone(metafile.to_png(PUBLISHER_STUB, 100.0, 100.0))

    def test_wmf_is_declined(self):
        data = struct.pack("<I", metafile.WMF_PLACEABLE_KEY) + bytes(60)
        self.assertIsNone(metafile.to_png(data, 100.0, 100.0))


if __name__ == "__main__":
    unittest.main()
