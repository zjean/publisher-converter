"""WMF record interpretation: the drawing records become model shapes.

The vocabulary is closed. Every WMF record in the whole sample corpus is
one of 22 types and only six of them draw, so this covers the format as
the corpus actually uses it rather than the format in the abstract. What
is not covered is counted and reported, never quietly dropped.
"""

from __future__ import annotations

import struct
import unittest

from pubidml import model, wmf

META_EOF = 0x0000
META_SAVEDC = 0x001E
META_SETPOLYFILLMODE = 0x0106
META_RESTOREDC = 0x0127
META_SELECTOBJECT = 0x012D
META_DELETEOBJECT = 0x01F0
META_SETWINDOWORG = 0x020B
META_SETWINDOWEXT = 0x020C
META_LINETO = 0x0213
META_MOVETO = 0x0214
META_CREATEPENINDIRECT = 0x02FA
META_CREATEBRUSHINDIRECT = 0x02FC
META_POLYGON = 0x0324
META_POLYLINE = 0x0325
META_ELLIPSE = 0x0418
META_RECTANGLE = 0x041B
META_POLYPOLYGON = 0x0538
META_ARC = 0x0817  # deliberately unsupported: nothing in the corpus uses it

PS_SOLID, PS_NULL = 0, 5
BS_SOLID, BS_NULL = 0, 1


def record(function: int, payload: bytes = b"") -> bytes:
    if len(payload) % 2:
        payload += b"\x00"
    return struct.pack("<IH", (6 + len(payload)) // 2, function) + payload


def build(*records: bytes) -> bytes:
    """A standard (non-placeable) WMF wrapping the given records."""
    body = b"".join(records) + record(META_EOF)
    header = struct.pack("<HHHIHIH", 1, 9, 0x0300, (18 + len(body)) // 2, 0, 0, 0)
    return header + body


# Parameter order is reversed for several records; that is the format, not
# a mistake, and getting it wrong still produces plausible geometry.
def window_org(x: int, y: int) -> bytes:
    return record(META_SETWINDOWORG, struct.pack("<hh", y, x))


def window_ext(w: int, h: int) -> bytes:
    return record(META_SETWINDOWEXT, struct.pack("<hh", h, w))


def polygon(*points: tuple, function: int = META_POLYGON) -> bytes:
    payload = struct.pack("<H", len(points))
    for x, y in points:
        payload += struct.pack("<hh", x, y)
    return record(function, payload)


def polypolygon(*rings: tuple) -> bytes:
    payload = struct.pack("<H", len(rings))
    for ring in rings:
        payload += struct.pack("<H", len(ring))
    for ring in rings:
        for x, y in ring:
            payload += struct.pack("<hh", x, y)
    return record(META_POLYPOLYGON, payload)


def rectangle(left: int, top: int, right: int, bottom: int) -> bytes:
    return record(META_RECTANGLE, struct.pack("<hhhh", bottom, right, top, left))


def ellipse(left: int, top: int, right: int, bottom: int) -> bytes:
    return record(META_ELLIPSE, struct.pack("<hhhh", bottom, right, top, left))


def create_brush(style: int, colour: int) -> bytes:
    return record(META_CREATEBRUSHINDIRECT, struct.pack("<HIH", style, colour, 0))


def create_pen(style: int, width: int, colour: int) -> bytes:
    return record(META_CREATEPENINDIRECT, struct.pack("<HhhI", style, width, width, colour))


def select(index: int) -> bytes:
    return record(META_SELECTOBJECT, struct.pack("<H", index))


def delete(index: int) -> bytes:
    return record(META_DELETEOBJECT, struct.pack("<H", index))


def colour_ref(red: int, green: int, blue: int) -> int:
    return red | (green << 8) | (blue << 16)


# A 100x100 logical window drawn into a 100x100pt frame at the origin, so
# logical units and points line up and the arithmetic stays readable.
UNIT_WINDOW = (window_org(0, 0), window_ext(100, 100))


def convert(*records: bytes, x=0.0, y=0.0, width=100.0, height=100.0):
    return wmf.to_items(build(*records), x, y, width, height)


class GeometryTest(unittest.TestCase):
    def test_a_polygon_becomes_a_closed_polygon_shape(self):
        art = convert(*UNIT_WINDOW, polygon((0, 0), (50, 0), (50, 50)))
        shape = art.items[0]
        self.assertIsInstance(shape, model.Polygon)
        self.assertTrue(shape.closed)
        self.assertEqual(shape.points, [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0)])

    def test_a_polyline_stays_open(self):
        art = convert(*UNIT_WINDOW, polygon((0, 0), (50, 50), function=META_POLYLINE))
        self.assertFalse(art.items[0].closed)

    def test_a_polygons_bounding_box_is_its_frame(self):
        art = convert(*UNIT_WINDOW, polygon((10, 20), (40, 20), (40, 60)))
        shape = art.items[0]
        self.assertEqual(
            (shape.x, shape.y, shape.width, shape.height), (10.0, 20.0, 30.0, 40.0)
        )

    def test_a_rectangle_reads_its_reversed_parameters(self):
        # The record stores bottom, right, top, left -- in that order.
        art = convert(*UNIT_WINDOW, rectangle(10, 20, 70, 50))
        shape = art.items[0]
        self.assertIsInstance(shape, model.Rectangle)
        self.assertEqual(
            (shape.x, shape.y, shape.width, shape.height), (10.0, 20.0, 60.0, 30.0)
        )

    def test_an_ellipse_becomes_an_ellipse(self):
        art = convert(*UNIT_WINDOW, ellipse(0, 0, 40, 20))
        shape = art.items[0]
        self.assertIsInstance(shape, model.Ellipse)
        self.assertEqual((shape.width, shape.height), (40.0, 20.0))

    def test_moveto_and_lineto_become_one_open_run(self):
        art = convert(
            *UNIT_WINDOW,
            record(META_MOVETO, struct.pack("<hh", 10, 5)),
            record(META_LINETO, struct.pack("<hh", 30, 25)),
            record(META_LINETO, struct.pack("<hh", 50, 45)),
        )
        shape = art.items[0]
        self.assertFalse(shape.closed)
        self.assertEqual(shape.points, [(5.0, 10.0), (25.0, 30.0), (45.0, 50.0)])

    def test_a_polypolygon_reads_all_counts_before_any_coordinates(self):
        # The trap: counts for every ring come first, then the points for
        # every ring. Reading them interleaved still yields plausible
        # geometry, which is what makes it dangerous.
        art = convert(
            *UNIT_WINDOW,
            polypolygon(((0, 0), (10, 0), (10, 10)), ((20, 20), (40, 20), (40, 40), (20, 40))),
        )
        self.assertEqual(len(art.items), 2)
        self.assertEqual(art.items[0].points, [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)])
        self.assertEqual(
            art.items[1].points,
            [(20.0, 20.0), (40.0, 20.0), (40.0, 40.0), (20.0, 40.0)],
        )


class MappingTest(unittest.TestCase):
    def test_the_logical_window_is_scaled_onto_the_frame(self):
        art = convert(
            window_org(0, 0), window_ext(200, 100),
            polygon((0, 0), (200, 0), (200, 100)),
            width=100.0, height=50.0,
        )
        self.assertEqual(
            art.items[0].points, [(0.0, 0.0), (100.0, 0.0), (100.0, 50.0)]
        )

    def test_the_window_origin_is_subtracted(self):
        # Real artwork uses a negative origin, which is why its coordinates
        # look out of range until this is applied.
        art = convert(
            window_org(-100, -100), window_ext(200, 200),
            polygon((-100, -100), (100, -100), (100, 100)),
            width=200.0, height=200.0,
        )
        self.assertEqual(
            art.items[0].points, [(0.0, 0.0), (200.0, 0.0), (200.0, 200.0)]
        )

    def test_the_frames_position_on_the_page_is_added(self):
        art = convert(
            *UNIT_WINDOW, polygon((0, 0), (100, 100)), x=300.0, y=400.0
        )
        self.assertEqual(art.items[0].points, [(300.0, 400.0), (400.0, 500.0)])

    def test_without_a_window_the_geometry_is_fitted_to_the_frame(self):
        # No SetWindowExt: fall back to the drawing's own bounds so the art
        # still lands inside its frame instead of at some arbitrary scale.
        art = convert(polygon((10, 10), (30, 10), (30, 30)), width=40.0, height=40.0)
        xs = [p[0] for p in art.items[0].points]
        ys = [p[1] for p in art.items[0].points]
        self.assertEqual((min(xs), max(xs), min(ys), max(ys)), (0.0, 40.0, 0.0, 40.0))


class StyleTest(unittest.TestCase):
    def test_a_selected_brush_fills_the_shape(self):
        art = convert(
            *UNIT_WINDOW,
            create_brush(BS_SOLID, colour_ref(255, 128, 0)),
            select(0),
            polygon((0, 0), (10, 0), (10, 10)),
        )
        self.assertEqual(art.items[0].style.fill, (255, 128, 0))

    def test_a_selected_pen_strokes_the_shape(self):
        art = convert(
            *UNIT_WINDOW,
            create_pen(PS_SOLID, 2, colour_ref(0, 0, 255)),
            select(0),
            polygon((0, 0), (10, 0), (10, 10)),
        )
        self.assertEqual(art.items[0].style.stroke, (0, 0, 255))
        self.assertGreater(art.items[0].style.stroke_width, 0.0)

    def test_a_null_pen_means_no_stroke(self):
        art = convert(
            *UNIT_WINDOW,
            create_pen(PS_NULL, 0, colour_ref(0, 0, 0)),
            select(0),
            polygon((0, 0), (10, 0), (10, 10)),
        )
        self.assertIsNone(art.items[0].style.stroke)

    def test_a_null_brush_means_no_fill(self):
        art = convert(
            *UNIT_WINDOW,
            create_brush(BS_NULL, colour_ref(255, 0, 0)),
            select(0),
            polygon((0, 0), (10, 0), (10, 10)),
        )
        self.assertIsNone(art.items[0].style.fill)

    def test_a_deleted_slot_is_reused_by_the_next_object(self):
        # The object table is slot-based: delete frees a slot and the next
        # create takes the lowest free one. Appending instead would leave
        # every later SelectObject pointing at the wrong object.
        art = convert(
            *UNIT_WINDOW,
            create_brush(BS_SOLID, colour_ref(1, 1, 1)),    # slot 0
            create_brush(BS_SOLID, colour_ref(2, 2, 2)),    # slot 1
            delete(0),
            create_brush(BS_SOLID, colour_ref(3, 3, 3)),    # slot 0 again
            select(0),
            polygon((0, 0), (10, 0), (10, 10)),
        )
        self.assertEqual(art.items[0].style.fill, (3, 3, 3))

    def test_restoredc_puts_back_the_previous_selection(self):
        art = convert(
            *UNIT_WINDOW,
            create_brush(BS_SOLID, colour_ref(9, 9, 9)),
            select(0),
            record(META_SAVEDC),
            create_brush(BS_SOLID, colour_ref(7, 7, 7)),
            select(1),
            record(META_RESTOREDC),
            polygon((0, 0), (10, 0), (10, 10)),
        )
        self.assertEqual(art.items[0].style.fill, (9, 9, 9))

    def test_paint_order_is_preserved(self):
        art = convert(
            *UNIT_WINDOW,
            create_brush(BS_SOLID, colour_ref(1, 1, 1)), select(0),
            polygon((0, 0), (50, 0), (50, 50)),
            create_brush(BS_SOLID, colour_ref(2, 2, 2)), select(1),
            polygon((10, 10), (40, 10), (40, 40)),
        )
        self.assertEqual(
            [s.style.fill for s in art.items], [(1, 1, 1), (2, 2, 2)]
        )


class ReportingTest(unittest.TestCase):
    def test_supported_records_are_counted(self):
        art = convert(*UNIT_WINDOW, polygon((0, 0), (10, 0)), rectangle(0, 0, 5, 5))
        self.assertEqual(art.converted, 2)
        self.assertEqual(art.unsupported, 0)

    def test_a_drawing_record_we_cannot_read_is_counted_not_ignored(self):
        art = convert(
            *UNIT_WINDOW,
            polygon((0, 0), (10, 0)),
            record(META_ARC, struct.pack("<8h", *range(8))),
        )
        self.assertEqual(art.converted, 1)
        self.assertEqual(art.unsupported, 1)

    def test_state_records_are_not_counted_as_drawing(self):
        art = convert(
            *UNIT_WINDOW,
            record(META_SETPOLYFILLMODE, struct.pack("<H", 1)),
            polygon((0, 0), (10, 0)),
        )
        self.assertEqual((art.converted, art.unsupported), (1, 0))


class RobustnessTest(unittest.TestCase):
    def test_a_metafile_with_no_drawing_records_yields_nothing(self):
        self.assertIsNone(convert(*UNIT_WINDOW))

    def test_a_non_wmf_payload_yields_nothing(self):
        for data in (b"", b"nonsense", bytes(40), b"\xff" * 64):
            with self.subTest(length=len(data)):
                self.assertIsNone(wmf.to_items(data, 0.0, 0.0, 10.0, 10.0))

    def test_a_zero_length_record_does_not_hang(self):
        data = bytearray(build(*UNIT_WINDOW, polygon((0, 0), (10, 10))))
        struct.pack_into("<I", data, 18, 0)
        self.assertIsNone(wmf.to_items(bytes(data), 0.0, 0.0, 10.0, 10.0))

    def test_a_truncated_point_array_is_survived(self):
        good = polygon((0, 0), (10, 0), (10, 10))
        truncated = good[:-6]  # lose the last point, keep the claimed count
        data = build(*UNIT_WINDOW, truncated)
        # Either it declines or it returns what it could read; it must not
        # raise and must not invent coordinates.
        art = wmf.to_items(data, 0.0, 0.0, 10.0, 10.0)
        if art is not None:
            for shape in art.items:
                self.assertTrue(all(isinstance(p, tuple) for p in shape.points))

    def test_a_degenerate_frame_does_not_divide_by_zero(self):
        art = convert(*UNIT_WINDOW, polygon((0, 0), (10, 10)), width=0.0, height=0.0)
        self.assertIsNotNone(art)


if __name__ == "__main__":
    unittest.main()
