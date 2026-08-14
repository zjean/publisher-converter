"""Build a probe .idml that exercises object opacity.

Two things arrive as transparency and neither used to survive:

  - `draw:opacity`, a shape's own, on four shapes in the corpus. It was
    written as `FillTint`, which is not what it means -- a tint mixes the
    colour with the paper, so a 78% fill came out pale rather than
    see-through, and looked right only over white.
  - a gradient's `svg:stop-opacity`, on 42 shapes. IDML gradient stops
    have no opacity at all, so this can only be stated as the whole object
    being see-through. Every case in the corpus is a two-stop ramp with
    both stops at 60%, which makes that exact rather than an
    approximation.

Both now become a `BlendingSetting` inside `TransparencySetting`, and
whether Affinity honours it is the question. It matters more than it
sounds: if it is ignored, a 60% panel comes out solid and hides whatever
it was laid over, which in the newsletters is the page background.

Every row is a coloured shape over a black rule, so transparency is
visible as the rule showing through rather than as a guess about
lightness -- a pale tint and a faded colour look identical against white,
which is exactly the confusion that produced the old `FillTint`.

    python3 research/probe_opacity.py

Writes converted/probe/probe-opacity.idml. Open it and read off:

  - A hides the rule, B/C/D show it through, more of it each row
                                  -> opacity works end to end.
  - Every row hides the rule      -> BlendingSetting is ignored. Restore
                                     the FillTint stand-in for solid
                                     fills and warn on the gradients.
  - Rows show the rule but the
    colours look unchanged        -> a blend mode is being applied
                                     instead of opacity; check BlendMode.
  - D solid while B and C fade    -> gradient fills ignore object
                                     opacity, and the 42 see-through
                                     ramps cannot be carried this way.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# The two real values in the corpus, plus a third that cannot be mistaken.
ROWS = [
    ("A - opaque (control, must hide the rule)", 1.0, False),
    ("B - 78% opacity, the corpus's own faded fill", 0.78, False),
    ("C - 25% opacity (unmistakable)", 0.25, False),
    ("D - 60% gradient, both stops, as the corpus has it", 0.6, True),
]

RAMP = (
    model.GradientStop(location=32.0, color=(0xA8, 0xBA, 0xD4), opacity=0.6),
    model.GradientStop(location=49.0, color=(0xDF, 0xE6, 0xEF), opacity=0.6),
)


def probe_document() -> model.Document:
    document = model.Document(title="Object opacity probe")
    page = model.Page(width=612.0, height=792.0)

    for index, (label, opacity, gradient) in enumerate(ROWS):
        top = 108.0 + index * 162.0

        # The rule goes down first, so anything see-through shows it.
        page.items.append(
            model.Rectangle(
                x=144.0, y=top + 30.0, width=324.0, height=12.0,
                style=model.GraphicStyle(fill=(0x00, 0x00, 0x00)),
            )
        )
        page.items.append(
            model.Rectangle(
                x=180.0, y=top, width=216.0, height=72.0,
                style=model.GraphicStyle(
                    fill=RAMP[0].color if gradient else (0xCC, 0x22, 0x22),
                    fill_opacity=opacity,
                    gradient=model.Gradient(stops=RAMP, angle=90.0) if gradient else None,
                ),
            )
        )

        caption = model.TextFrame(x=144.0, y=top + 82.0, width=360.0, height=18.0)
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text=label, size_pt=10.0, font="Helvetica"))
        caption.story.paragraphs.append(paragraph)
        page.items.append(caption)

    document.pages.append(page)
    return document


def build(destination: Path) -> None:
    document = probe_document()
    writer = idml.IdmlWriter(document, image_dir_name="probe_images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        for name in names:
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))
        spread = next(n for n in names if n.startswith("Spreads/"))
        root = ET.fromstring(archive.read(spread))
        written = [
            (node.get("Opacity"), node.get("BlendMode"))
            for node in root.iter("BlendingSetting")
        ]
        tints = [r.get("FillTint") for r in root.iter("Rectangle")]

    print(f"wrote {destination}")
    print("  row  opacity  blend mode")
    for label, (opacity, blend) in zip("BCD", written):
        print(f"    {label}  {opacity:>7}  {blend}")
    print(f"  FillTint attributes written: {[t for t in tints if t]} (must be empty)")
    print("  open it and report whether A/B/C/D differ as labelled")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-opacity.idml"))
