"""Ask whether a stated first baseline lets a headline fill its own band.

`probe_wordart_fit.py` settled what empties the dropped initial's box, and
it is not the leading, the scale, the alignment or the wrap. Sweeping the
frame's height at a fixed size and the size at a fixed frame gave two
brackets that close on one number:

    frame height must be at least  usWinAscent x point size

    Pristina, per em     hhea ascent 0.750   typo ascent 0.814
                         win ascent 0.864    head yMax 0.864
    bracket measured     more than 0.814, at most 0.8643

Cell 3 asked for exactly the typo ascent and drew nothing; cell 4 asked
for exactly the win ascent and drew. So Affinity places a frame's first
baseline one win-ascent below its top, and oversets the line -- draws
nothing at all -- when that baseline would fall past the frame's bottom.
The `Leading` we state is not consulted for it. A WordArt band is by
definition shorter than that: the band is what the glyphs *ink*, 0.69 of
an em for this D, and the win ascent is 0.86.

Applied to the corpus, that rule accounts for the reported symptom and
predicts two more of the same:

    1337 kerkbode   D            Pristina          band 40.06  needs 50.04
                    Meditatie    Monotype Corsiva  band 28.74  needs 31.47
                    Financien    Monotype Corsiva  band 20.42  needs 25.42

and clears every other headline in the file, including *Uit de pastorie*
on page 11, which is the one already known to draw.

**What this probe is for.** Three fixes are available and they are not
equally good:

  - State the first baseline, and the band can stay the band. IDML carries
    `FirstBaselineOffset` on the frame, and `idml.py` already records that
    Affinity honours it for tables (`FixedHeight` there, with a documented
    quirk). If a stated offset is honoured here, nothing about the band,
    the size or the wrap has to change -- and the offset can be set to
    where Publisher actually draws the baseline, which its PDF export
    gives exactly: the ink fills the band, so the baseline sits
    yMax/(yMax-yMin) of the band's height below its top, 39.21pt of the
    40.06 for this D.
  - Grow the frame to what the win ascent asks. Then the frame is no
    longer the band, and the frame is what the body copy flows around --
    a dropped initial's wrap would grow by 5pt top and bottom and push
    text it should not. Cell K tests whether a negative `TextWrapOffset`
    can pull the wrap back to the band and rescue that.
  - Shrink the type until the win ascent fits. Honest, and wrong by 20%:
    this D would be set at 46.35pt where Publisher draws it at 57.86.

Vertical alignment is crossed with the offset because the two interact --
InDesign ignores a first-baseline offset in a vertically centred frame,
and whether Affinity does the same is unmeasured. Cell F of the first
probe could not say: that frame was too short either way.

Unlike the other probes here, this one hand-patches the package after
`idml.IdmlWriter` has written it. `FirstBaselineOffset` is not in the
model, and putting it there before knowing whether it works would be
writing the answer before the question.

**Answered, on Affinity Publisher / macOS. C is the fix.**

    A  as shipped                     nothing drawn
    B  leading offset, leading = band nothing drawn (the baseline lands
                                      exactly on the frame's bottom)
    C  leading offset, leading 39.22  draws, ink inside the box
    D  fixed height 39.22, top        draws, above the box
    E  fixed height 39.22, centred    draws, half out of the top
    F  cap height, top                nothing drawn
    G  x height, top                  draws, a little out of the top
    H  ascent stated explicitly       nothing drawn
    I  em box height, top             nothing drawn
    J  frame grown to the win ascent  draws, sitting on the box's bottom
    K  the same, wrap pulled back in  draws, sitting on the box's bottom
    L  size fitted to the band        draws, sitting on the box's bottom

So a stated first baseline *is* honoured, `LeadingOffset` places it where
asked, and the band can stay the band. `FixedHeight` is not usable: it
lifts the headline off its position, which is the same answer
`probe_table_placement.py` got from it. Growing the frame (J) or shrinking
the type (L) both draw, and both put the letter at the bottom of the band
instead of filling it -- so C is right on placement as well as on
visibility, and it is the one the converter now writes.

    python3 research/probe_wordart_baseline.py

Writes converted/probe/probe-wordart-baseline.idml. Every box is stroked
and is *exactly the band*, and Publisher fills that band with the letter's
ink top to bottom -- so for each cell that draws, the thing to report is
whether the ink fills the box, sits high, sits low, or hangs outside it.

  - A empty, and any of B-G drawing        -> a stated first baseline is
                                              honoured, and the band can
                                              stay the band. Whichever of
                                              them also fills the box is
                                              the fix.
  - B-G all empty                          -> `FirstBaselineOffset` is
                                              ignored and the frame has to
                                              grow. Then J says where the
                                              letter lands and K whether
                                              the wrap can be kept.
  - the TopAlign cells drawing where the
    CenterAlign ones do not                -> centring overrides the
                                              offset, as in InDesign, and
                                              the headline has to be top
                                              aligned to a measured
                                              baseline instead.
  - J drawing with the ink filling the box  -> growing the frame keeps the
                                              letter in place, and only
                                              the wrap needs answering.
  - K's wrap sitting where J's does         -> a negative wrap offset is
                                              ignored, and a grown frame
                                              cannot keep Publisher's wrap.
  - L drawing but visibly small             -> confirms the third fix is
                                              the one to avoid.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# The band and the run exactly as `1337 kerkbode.idml` states them.
BAND_W, BAND_H = 44.923622047244095, 40.0632283464567
POINT_SIZE = 57.903664
LEADING = 40.063228
H_SCALE = 121.198147
BROWN = (0x80, 0x33, 0x00)

# Pristina's D inks from yMin -83... no: from -30 to 1387 of its 2048 em,
# so of everything it inks, yMax/(yMax-yMin) sits above the baseline. The
# band *is* the ink, measured against Publisher's own PDF, so this is
# where Publisher's baseline falls inside the band.
INK_ABOVE_BASELINE = BAND_H * 1387.0 / 1417.0

# usWinAscent x the point size: what the frame has to clear, measured.
WIN_ASCENT = 0.8643
REQUIRED_H = WIN_ASCENT * POINT_SIZE
# And the size that would fit the band instead of growing the frame.
FITTED_SIZE = BAND_H / WIN_ASCENT

LABEL_FONT = "Arial"
LABEL_SIZE = 8.0

CELL_W, CELL_H = 168.0, 150.0
MARGIN_X, MARGIN_Y = 54.0, 63.0
COLUMNS = 3
SLOT_H = 72.0


def _cells():
    """(letter, what it asks, baseline, valign, leading, height, size, wrap).

    `baseline` is (FirstBaselineOffset, MinimumFirstLineOffset) or None to
    leave the attribute off, which is what the converter writes today.
    `wrap` is the TextWrapOffset to state on all four sides.
    """
    return [
        ("A", "as shipped (control)",
         None, "center", LEADING, BAND_H, POINT_SIZE, 0.0),
        ("B", "leading offset, top",
         ("LeadingOffset", None), "top", LEADING, BAND_H, POINT_SIZE, 0.0),
        ("C", f"leading {INK_ABOVE_BASELINE:.2f} as offset, top",
         ("LeadingOffset", None), "top", INK_ABOVE_BASELINE, BAND_H,
         POINT_SIZE, 0.0),
        ("D", f"fixed {INK_ABOVE_BASELINE:.2f}, top",
         ("FixedHeight", INK_ABOVE_BASELINE), "top", LEADING, BAND_H,
         POINT_SIZE, 0.0),
        ("E", f"fixed {INK_ABOVE_BASELINE:.2f}, centred",
         ("FixedHeight", INK_ABOVE_BASELINE), "center", LEADING, BAND_H,
         POINT_SIZE, 0.0),
        ("F", "cap height, top",
         ("CapHeightOffset", None), "top", LEADING, BAND_H, POINT_SIZE, 0.0),
        ("G", "x height, top",
         ("XHeight", None), "top", LEADING, BAND_H, POINT_SIZE, 0.0),
        ("H", "ascent stated explicitly, top",
         ("AscentOffset", None), "top", LEADING, BAND_H, POINT_SIZE, 0.0),
        ("I", "em box height, top",
         ("EmBoxHeight", None), "top", LEADING, BAND_H, POINT_SIZE, 0.0),
        ("J", f"frame {REQUIRED_H:.1f} (win ascent), centred",
         None, "center", LEADING, REQUIRED_H, POINT_SIZE, 0.0),
        ("K", f"frame {REQUIRED_H:.1f}, wrap pulled "
              f"{(REQUIRED_H - BAND_H) / 2.0:.1f} in",
         None, "center", LEADING, REQUIRED_H, POINT_SIZE,
         -(REQUIRED_H - BAND_H) / 2.0),
        ("L", f"size {FITTED_SIZE:.2f} to fit the band",
         None, "center", LEADING, BAND_H, FITTED_SIZE, 0.0),
    ]


def _initial(x, y, height, valign, leading, size):
    frame = model.TextFrame(
        x=x, y=y, width=BAND_W, height=height,
        vertical_align=valign,
        wrap_text=True,
        # Stroked, which the real one is not: an empty box has to be
        # tellable from a box that never arrived, and the stroke is also
        # the reference the ink is meant to fill.
        style=model.GraphicStyle(stroke=(0x99, 0x99, 0x99), stroke_width=0.5),
    )
    paragraph = model.Paragraph(align="center", line_spacing_pt=leading)
    paragraph.spans.append(
        model.Span(
            text="D", font="Pristina", size_pt=size, color=BROWN,
            italic=True, horizontal_scale=H_SCALE,
        )
    )
    frame.story.paragraphs.append(paragraph)
    return frame


def _caption(x, y, text):
    frame = model.TextFrame(x=x, y=y, width=CELL_W - 10.0, height=30.0)
    paragraph = model.Paragraph()
    paragraph.spans.append(
        model.Span(text=text, font=LABEL_FONT, size_pt=LABEL_SIZE)
    )
    frame.story.paragraphs.append(paragraph)
    return frame


def probe_document() -> model.Document:
    document = model.Document(title="WordArt first baseline probe")
    page = model.Page(width=612.0, height=792.0)
    for index, cell in enumerate(_cells()):
        letter, what, _baseline, valign, leading, height, size, _wrap = cell
        column, row = index % COLUMNS, index // COLUMNS
        left = MARGIN_X + column * CELL_W
        top = MARGIN_Y + row * CELL_H
        page.items.append(
            _initial(left, top + (SLOT_H - height) / 2.0, height, valign,
                     leading, size)
        )
        page.items.append(_caption(left, top + SLOT_H + 8.0, f"{letter} — {what}"))
    document.pages.append(page)
    return document


def _patch(spread: bytes) -> bytes:
    """Put each cell's first baseline and wrap offset on its frame.

    Hand-patched rather than modelled, and keyed on the frames holding the
    letter in the order they were emitted, which is the order of `_cells`.
    """
    root = ET.fromstring(spread)
    cells = _cells()
    index = 0
    for frame in root.iter("TextFrame"):
        preference = frame.find("TextFramePreference")
        if preference is None:
            continue
        # A caption frame states no wrap and no vertical justification we
        # set; the letter's frames are the ones carrying a wrap.
        wrap = frame.find("TextWrapPreference")
        if wrap is None:
            continue
        letter, _what, baseline, _valign, _leading, _h, _size, offset = cells[index]
        if baseline is not None:
            kind, minimum = baseline
            preference.set("FirstBaselineOffset", kind)
            if minimum is not None:
                preference.set("MinimumFirstLineOffset", f"{minimum:.6f}")
        if offset:
            for node in wrap.iter("TextWrapOffset"):
                for side in ("Top", "Left", "Bottom", "Right"):
                    node.set(side, f"{offset:.6f}")
        index += 1
        if index >= len(cells):
            break
    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def build(destination: Path) -> None:
    document = probe_document()
    writer = idml.IdmlWriter(document, image_dir_name="probe_images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)

    with zipfile.ZipFile(destination) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    spread_name = next(n for n in parts if n.startswith("Spreads/"))
    parts[spread_name] = _patch(parts[spread_name])
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        # mimetype first and stored, as the writer does.
        archive.writestr(
            zipfile.ZipInfo("mimetype"), parts.pop("mimetype"),
            compress_type=zipfile.ZIP_STORED,
        )
        for name, payload in parts.items():
            archive.writestr(name, payload)

    with zipfile.ZipFile(destination) as archive:
        for name in archive.namelist():
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))
        root = ET.fromstring(archive.read(spread_name))

    print(f"wrote {destination}")
    print(f"  band {BAND_W:.2f} x {BAND_H:.2f}, Pristina {POINT_SIZE:.2f}pt")
    print(f"  win ascent asks {REQUIRED_H:.2f}pt of frame; Publisher's "
          f"baseline sits {INK_ABOVE_BASELINE:.2f}pt down")
    print("  cell  frame   size   valign       first baseline")
    written = [
        (
            frame.find("TextFramePreference").get("VerticalJustification"),
            frame.find("TextFramePreference").get("FirstBaselineOffset") or "-",
            frame.find("TextFramePreference").get("MinimumFirstLineOffset") or "-",
            next(iter(frame.iter("TextWrapOffset"))).get("Top"),
        )
        for frame in root.iter("TextFrame")
        if frame.find("TextWrapPreference") is not None
    ]
    for cell, row in zip(_cells(), written):
        valign, kind, minimum, wrap_top = row
        print(f"    {cell[0]:2s} {cell[5]:6.2f} {cell[6]:6.2f}  {valign:11s}  "
              f"{kind:14s} min {minimum:>10s}  wrap {wrap_top}")
    print("  open it and report, per cell, whether the letter draws and")
    print("  whether its ink fills the stroked box")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-wordart-baseline.idml"))
