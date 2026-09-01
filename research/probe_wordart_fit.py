"""Find what height a WordArt frame has to have for its letter to draw.

`probe_wordart_initial.py` narrowed the empty dropped initial to two
levers and ruled out everything else. Of its twelve cells only two drew
the letter: the one with a frame half again as tall, and the one set in
Monotype Corsiva instead of Pristina. Changing the leading, the
horizontal scale, the vertical alignment, the text wrap, the tracking and
the italic all left the box as empty as the control.

So the frame is too short for what the font asks at that size, and the
leading we state is not what Affinity measures the line by. That leaves
one number to find: **what does the frame's height have to clear?** The
obvious candidates are all metrics the font states, and they differ enough
to tell apart:

    Pristina, per em      hhea ascent 0.750   descent 0.447   gap 0
                          typo ascent 0.814   descent 0.441   gap 0
                          win  ascent 0.864   descent 0.447

    Monotype Corsiva      hhea ascent 0.790   descent 0.303   gap 0.029
                          typo ascent 0.689   descent 0.259   gap 0.122
                          win  ascent 0.790   descent 0.303

At the shipped 57.90pt in a 40.06pt band, Pristina's typo ascent alone is
47.1pt and Corsiva's is 39.9pt -- which is the only pair of numbers in
that table that falls either side of the frame, and so the only candidate
that explains Corsiva drawing where Pristina does not. This probe tests it
by sweeping both sides of the comparison independently.

There is one result it has to account for and cannot yet: cell L of the
first probe asked for 30pt in the same 40.06pt frame, where every one of
those metrics fits several times over, and drew nothing. Either the point
size is not in the comparison at all -- which would mean the frame has to
clear something absolute about the font -- or L was misread. Rows 6 to 10
below settle that: they sweep the size from 49.2pt down to 20pt in the
band exactly as it is, and if the letter never appears then the size is
not what decides.

**Answered, on Affinity Publisher / macOS: cells 4, 5, 7, 8, 9 and 10.**
Both sweeps bracket one constant, from either side:

  - Cell 3's frame was exactly the typo ascent, 47.13pt, and drew nothing.
    Cell 4's was exactly the win ascent, 50.05pt, and drew. So the frame
    must clear more than 0.8140 of an em and at most 0.8643.
  - Cell 6 was sized so the typo ascent equalled the band, and is empty;
    cell 7 at 45pt draws. Same window from the other direction.
  - Only `usWinAscent` and `head`'s yMax fall inside it, and for Pristina
    they are the same number.
  - Cell 11 is empty, which the rule needs: Corsiva asks 45.74pt of a
    40.06pt band. Cell K of the first probe reported the same case as
    drawing; ten cells agree here, so that reading was a slip.

So: **the frame's height must be at least `usWinAscent x point size`**.
Affinity places a frame's first baseline one win ascent below its top and
oversets the line when that baseline falls past the frame's bottom, and
the `Leading` we state is not consulted for it. What to do about it is
`probe_wordart_baseline.py`.

    python3 research/probe_wordart_fit.py

Writes converted/probe/probe-wordart-fit.idml. Open it and report which
cells show the letter, and whether the ones that do look like a script
face or like something substituted. What each answer settles:

  - The first row where 1-5 draws gives the height the frame must clear
    at 57.90pt. Compare it against the table above: 43.4 points to the
    hhea ascent, 47.1 to the typo ascent, 50.0 to the win ascent, 69.3 to
    a whole hhea line.
  - The first row where 6-10 draws gives the size the band can hold, and
    the two answers together say whether the rule is a ratio (they will
    agree on one fraction of an em) or something else.
  - If 6-10 are all empty while 1-5 draw from some height on, the size is
    not in the comparison: the frame has to clear an absolute demand of
    the font, and growing the frame is the only fix available.
  - If 11 draws and 12 does not, the substituted default is worse than
    Corsiva, which matters for every headline set in a face the reader
    lacks.
  - If the letters that draw look nothing like a script face, Pristina is
    not reaching Affinity at all and the metrics above are not the ones
    being applied -- everything else here would then be measuring the
    substitute.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import model  # noqa: E402
from pubidml import idml  # noqa: E402

# The band and the run exactly as `1337 kerkbode.idml` states them.
BAND_W, BAND_H = 44.923622047244095, 40.0632283464567
POINT_SIZE = 57.903664
LEADING = 40.063228
H_SCALE = 121.198147
BROWN = (0x80, 0x33, 0x00)

# Pristina's own vertical metrics, per em, read off the installed face.
# Multiplied by the shipped point size these are the heights a reader
# might be asking the frame to clear.
PRISTINA_HHEA_ASCENT = 0.750
PRISTINA_TYPO_ASCENT = 0.814
PRISTINA_WIN_ASCENT = 0.8643

LABEL_FONT = "Arial"
LABEL_SIZE = 8.5

CELL_W, CELL_H = 168.0, 150.0
MARGIN_X, MARGIN_Y = 54.0, 63.0
COLUMNS = 3

# The tallest frame any cell uses, so every cell's box can be centred in
# the same slot and a taller box reads as the same headline in more room.
SLOT_H = 72.0


def _cells():
    """(letter, what it asks, font, point size, frame height)."""
    return [
        # Sweep the frame, holding the run at what the converter writes.
        ("1", "band as shipped (control)", "Pristina", POINT_SIZE, BAND_H),
        ("2", f"frame {PRISTINA_HHEA_ASCENT * POINT_SIZE:.1f} (hhea ascent)",
         "Pristina", POINT_SIZE, PRISTINA_HHEA_ASCENT * POINT_SIZE),
        ("3", f"frame {PRISTINA_TYPO_ASCENT * POINT_SIZE:.1f} (typo ascent)",
         "Pristina", POINT_SIZE, PRISTINA_TYPO_ASCENT * POINT_SIZE),
        ("4", f"frame {PRISTINA_WIN_ASCENT * POINT_SIZE:.1f} (win ascent)",
         "Pristina", POINT_SIZE, PRISTINA_WIN_ASCENT * POINT_SIZE),
        ("5", f"frame {BAND_H * 1.5:.1f} (1.5x, drew before)",
         "Pristina", POINT_SIZE, BAND_H * 1.5),
        # Sweep the size, holding the frame at the band exactly.
        ("6", "size 49.2 (typo ascent = band)", "Pristina",
         BAND_H / PRISTINA_TYPO_ASCENT, BAND_H),
        ("7", "size 45", "Pristina", 45.0, BAND_H),
        ("8", "size 40", "Pristina", 40.0, BAND_H),
        ("9", "size 30 (drew nothing before)", "Pristina", 30.0, BAND_H),
        ("10", "size 20", "Pristina", 20.0, BAND_H),
        # And the font, at the band and the size exactly as shipped.
        ("11", "Monotype Corsiva (drew before)", "Monotype Corsiva",
         POINT_SIZE, BAND_H),
        ("12", "no font stated", None, POINT_SIZE, BAND_H),
    ]


def _initial(x: float, y: float, height: float, font, size: float):
    """One copy of the frame `convert._wordart_frame` builds for that D."""
    frame = model.TextFrame(
        x=x, y=y, width=BAND_W, height=height,
        vertical_align="center",
        wrap_text=True,
        # Stroked, which the real one is not: an empty box has to be
        # tellable from a box that never arrived.
        style=model.GraphicStyle(stroke=(0x99, 0x99, 0x99), stroke_width=0.5),
    )
    paragraph = model.Paragraph(align="center", line_spacing_pt=LEADING)
    paragraph.spans.append(
        model.Span(
            text="D",
            font=font,
            size_pt=size,
            color=BROWN,
            italic=True,
            horizontal_scale=H_SCALE,
        )
    )
    frame.story.paragraphs.append(paragraph)
    return frame


def _caption(x: float, y: float, text: str) -> model.TextFrame:
    frame = model.TextFrame(x=x, y=y, width=CELL_W - 12.0, height=24.0)
    paragraph = model.Paragraph()
    paragraph.spans.append(
        model.Span(text=text, font=LABEL_FONT, size_pt=LABEL_SIZE)
    )
    frame.story.paragraphs.append(paragraph)
    return frame


def probe_document() -> model.Document:
    document = model.Document(title="WordArt frame fit probe")
    page = model.Page(width=612.0, height=792.0)

    for index, (letter, what, font, size, height) in enumerate(_cells()):
        column, row = index % COLUMNS, index // COLUMNS
        left = MARGIN_X + column * CELL_W
        top = MARGIN_Y + row * CELL_H
        page.items.append(
            _initial(left, top + (SLOT_H - height) / 2.0, height, font, size)
        )
        page.items.append(
            _caption(left, top + SLOT_H + 8.0, f"{letter} — {what}")
        )

    document.pages.append(page)
    return document


def build(destination: Path) -> None:
    document = probe_document()
    writer = idml.IdmlWriter(document, image_dir_name="probe_images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)

    with zipfile.ZipFile(destination) as archive:
        for name in archive.namelist():
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))

    print(f"wrote {destination}")
    print(f"  band as shipped: {BAND_W:.2f} x {BAND_H:.2f} at {POINT_SIZE:.2f}pt, "
          f"leading {LEADING:.2f}")
    print("  cell  frame height   size   font")
    for letter, _what, font, size, height in _cells():
        print(f"    {letter:2s}  {height:12.2f} {size:6.2f}   "
              f"{font or '(none stated)'}")
    print("  open it and report which cells show the letter,")
    print("  and whether the ones that do look like a script face")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-wordart-fit.idml"))
