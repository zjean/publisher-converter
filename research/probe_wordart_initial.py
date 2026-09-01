"""Build a probe .idml that asks why a dropped initial draws nothing.

The reported symptom, on `1337 kerkbode.pub` page 3: the dropped initial
*D* arrives as an empty box. The frame is there and selectable, the letter
is not drawn at all -- which is what a reader does with text it has
decided will not fit.

What the file itself says has been measured and is not in doubt. The band
is right: Publisher's own PDF export of the same page draws the letter's
ink inside 0.06pt of where the converter puts the frame. The point size is
right too: fitted against the glyph's exact outline, Publisher draws it at
57.86pt where the converter writes 57.90. Two things about the run are
*not* right, and only Affinity can say which of them empties the box:

  - The horizontal scale. Publisher draws that D at 95.7% of its natural
    width; the converter writes 121.2%, because `_wordart_fit` fits the
    glyph advances to the whole band and the band is the bounding box of
    *slanted* text -- a synthetic italic, since Pristina ships no italic
    face. At 121.2% the composed line is exactly as wide as the frame,
    with nothing to spare.
  - The leading. It is set to the band's height exactly, so the line box
    is exactly as tall as the frame, again with nothing to spare, while
    the type itself is half as tall again as the band it inks (Pristina at
    57.9pt asks 43.4pt for its ascent alone and 75.9pt for ascent and
    descent together).

Both leave a reader no slack at all, in one direction each. The headline
*Meditatie* on the same page has the same zero slack and draws fine, and
so does *Uit de pastorie* on page 11 -- which is what makes this worth
probing rather than reasoning about. Neither of those is set in Pristina,
neither is a single glyph, and both carry tracking; the initial carries
none. That is four differences and one symptom.

So: twelve copies of that exact frame, each with one thing changed and
nothing else. Every cell drives `idml.IdmlWriter` itself, so what Affinity
opens is what the converter would really write, and each box is stroked so
an empty one can be told from a missing one.

**Answered, on Affinity Publisher / macOS: only J and K draw the letter.**
Ten of the twelve cells are empty, including every one that changes the
run rather than the frame. So:

  - The leading is not the cause. B asks for 90% of the band and C asks
    for nothing at all; both are as empty as the control.
  - The horizontal scale is not the cause either, at 121.2% or at
    Publisher's own 95.7% (D, E).
  - Neither is the vertical centring (F), the frame's own text wrap (G,
    which page 11 had already ruled out), the tracking the other
    headlines carry (H), or the synthetic italic (I).
  - What does draw is a frame half again as tall (J) and the same frame
    with Monotype Corsiva in it (K). So the cause is the frame's height
    against what the *font* asks for at that size -- and only those two
    levers move it.
  - L is the odd one: 30pt in the same frame, which every reading of a
    line-fitting rule says should draw, and it does not. That is what
    `probe_wordart_fit.py` goes after next.

    python3 research/probe_wordart_initial.py

Writes converted/probe/probe-wordart-initial.idml. Open it and report
which cells show the letter. What each answer settles:

  - A empty, everything else the same    -> nothing here is the cause;
                                            look outside the run.
  - A empty, B or C shows the letter     -> the leading is the cause: a
                                            line box exactly as tall as
                                            its frame does not fit, and
                                            the band's height cannot be
                                            spent on it in full.
  - A empty, D or E shows the letter     -> the horizontal scale is the
                                            cause: a line exactly as wide
                                            as its frame does not fit
                                            either. E is also the value
                                            Publisher actually draws.
  - A empty, F shows the letter          -> vertical centring is what
                                            oversets it; top alignment is
                                            the safe request.
  - A empty, H shows the letter          -> the slack tracking happens to
                                            leave is what saves the other
                                            headlines, and the fix is to
                                            stop asking for a line that
                                            exactly fills its frame.
  - A empty, J shows the letter          -> the frame is simply too small
                                            for what is asked of it, and
                                            the band cannot be the frame.
  - A empty, K shows the letter          -> it is Pristina's own metrics,
                                            not the geometry: the fix has
                                            to answer to the font.
  - A empty, L shows the letter          -> the point size is what does
                                            not fit, and fitting the ink
                                            to the band overshoots what a
                                            reader will place.
  - G differs from A                     -> the frame's own text wrap is
                                            pushing its own text out (a
                                            known InDesign behaviour, and
                                            `IgnoreWrap` is the answer).
                                            Page 11 says otherwise, so
                                            this is the control on that.
  - I differs from A                     -> the synthetic italic is what
                                            overflows, and the slant has
                                            to be measured rather than
                                            left to the reader.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# The frame and the run exactly as `1337 kerkbode.idml` states them, read
# back off the converted package rather than retyped.
BAND_W, BAND_H = 44.923622047244095, 40.0632283464567
POINT_SIZE = 57.903664
LEADING = 40.063228
H_SCALE = 121.198147
FONT = "Pristina"
BROWN = (0x80, 0x33, 0x00)

# What Publisher itself draws, fitted against the glyph's exact outline in
# its own PDF export of the same page: 55.355pt of x against 57.857pt of y.
PUBLISHERS_SCALE = 95.68

# Publisher's other headlines all state a spacing multiple of 1.2, which
# reaches IDML as tracking; the initial states none. This is that value,
# to ask whether the slack it leaves is what saves them.
CORSIVA_TRACKING = 78.0

LABEL_FONT = "Arial"
LABEL_SIZE = 8.5

# Cell geometry. The box sits at the cell's top-left; the label runs under
# it, because a label above would be read as belonging to the cell before.
CELL_W, CELL_H = 168.0, 132.0
MARGIN_X, MARGIN_Y = 54.0, 72.0
COLUMNS = 3


def _variants():
    """(letter, what changed, frame width, frame height, tweak) per cell.

    `tweak` is handed the frame and the one span it holds and may change
    either; returning nothing is the control.
    """

    def nothing(frame, span, paragraph):
        pass

    def shorter_leading(frame, span, paragraph):
        paragraph.line_spacing_pt = BAND_H * 0.9

    def no_leading(frame, span, paragraph):
        paragraph.line_spacing_pt = None

    def no_scale(frame, span, paragraph):
        span.horizontal_scale = None

    def publishers_scale(frame, span, paragraph):
        span.horizontal_scale = PUBLISHERS_SCALE

    def top_aligned(frame, span, paragraph):
        frame.vertical_align = "top"

    def no_wrap(frame, span, paragraph):
        frame.wrap_text = False

    def with_tracking(frame, span, paragraph):
        span.tracking = CORSIVA_TRACKING

    def upright(frame, span, paragraph):
        span.italic = False

    def corsiva(frame, span, paragraph):
        span.font = "Monotype Corsiva"

    def smaller(frame, span, paragraph):
        span.size_pt = 30.0

    return [
        ("A", "as shipped (control)", BAND_W, BAND_H, nothing),
        ("B", "leading 90% of the band", BAND_W, BAND_H, shorter_leading),
        ("C", "no leading stated", BAND_W, BAND_H, no_leading),
        ("D", "no horizontal scale", BAND_W, BAND_H, no_scale),
        ("E", f"scale {PUBLISHERS_SCALE}% (Publisher's)", BAND_W, BAND_H,
         publishers_scale),
        ("F", "aligned top, not centred", BAND_W, BAND_H, top_aligned),
        ("G", "no text wrap", BAND_W, BAND_H, no_wrap),
        ("H", f"tracking {CORSIVA_TRACKING:.0f} added", BAND_W, BAND_H,
         with_tracking),
        ("I", "upright, not italic", BAND_W, BAND_H, upright),
        ("J", "frame 1.5x, same centre", BAND_W * 1.5, BAND_H * 1.5, nothing),
        ("K", "Monotype Corsiva", BAND_W, BAND_H, corsiva),
        ("L", "point size 30", BAND_W, BAND_H, smaller),
    ]


def _initial(x: float, y: float, width: float, height: float, tweak) -> model.TextFrame:
    """One copy of the frame `convert._wordart_frame` builds for that D."""
    frame = model.TextFrame(
        x=x, y=y, width=width, height=height,
        vertical_align="center",
        wrap_text=True,
        # Stroked, which the real one is not: an empty box has to be
        # tellable from a box that never arrived.
        style=model.GraphicStyle(stroke=(0x99, 0x99, 0x99), stroke_width=0.5),
    )
    paragraph = model.Paragraph(align="center", line_spacing_pt=LEADING)
    span = model.Span(
        text="D",
        font=FONT,
        size_pt=POINT_SIZE,
        color=BROWN,
        italic=True,
        horizontal_scale=H_SCALE,
    )
    paragraph.spans.append(span)
    frame.story.paragraphs.append(paragraph)
    tweak(frame, span, paragraph)
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
    document = model.Document(title="WordArt dropped initial probe")
    page = model.Page(width=612.0, height=792.0)

    variants = _variants()
    for index, (letter, what, width, height, tweak) in enumerate(variants):
        column, row = index % COLUMNS, index // COLUMNS
        left = MARGIN_X + column * CELL_W
        top = MARGIN_Y + row * CELL_H
        # Centres match across cells even where the frame is larger, so a
        # taller box is visibly the same headline in a bigger frame.
        page.items.append(
            _initial(
                left + (BAND_W * 1.5 - width) / 2.0,
                top + (BAND_H * 1.5 - height) / 2.0,
                width, height, tweak,
            )
        )
        page.items.append(
            _caption(left, top + BAND_H * 1.5 + 8.0, f"{letter} — {what}")
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
        spread = next(n for n in archive.namelist() if n.startswith("Spreads/"))
        root = ET.fromstring(archive.read(spread))
        stories = {
            name.split("Story_")[1].removesuffix(".xml"): ET.fromstring(
                archive.read(name)
            )
            for name in archive.namelist()
            if name.startswith("Stories/")
        }

    print(f"wrote {destination}")
    print("  cell  frame            size   leading  scale   tracking  font")
    letters = [letter for letter, *_ in _variants()]
    index = 0
    for frame in root.iter("TextFrame"):
        story = stories.get(frame.get("ParentStory"))
        if story is None:
            continue
        run = story.find(".//CharacterStyleRange")
        if run is None or story.find(".//Content") is None:
            continue
        if story.find(".//Content").text != "D":
            continue
        anchors = [
            [float(v) for v in point.get("Anchor").split()]
            for point in frame.iter("PathPointType")
        ]
        width = max(a[0] for a in anchors) - min(a[0] for a in anchors)
        height = max(a[1] for a in anchors) - min(a[1] for a in anchors)
        leading = run.find(".//Leading")
        applied = run.find(".//AppliedFont")
        print(
            f"    {letters[index]:2s}  {width:6.2f} x {height:6.2f}  "
            f"{float(run.get('PointSize')):6.2f}  "
            f"{(leading.text if leading is not None else '-'):>8s}  "
            f"{(run.get('HorizontalScale') or '-'):>7s}  "
            f"{(run.get('Tracking') or '-'):>8s}  "
            f"{applied.text if applied is not None else '-'}"
        )
        index += 1
    print("  open it and report which cells show the letter")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-wordart-initial.idml"))
