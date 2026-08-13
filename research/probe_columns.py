"""Build a probe .idml that exercises multi-column text frames.

No .pub in the sample set has a multi-column text box. libmspub emits
`fo:column-count` only when the file recorded one, and across the whole
corpus -- nine files, 300-odd text objects -- it never appears once, while
`fo:column-gap` appears on nearly every one. The newsletters get their
two-column look from separate linked text boxes instead, which is a
different mechanism entirely. So the column-writing path is covered by
unit tests and by nothing else.

Two things needed answering, and only Affinity could answer them:

  1. Does Affinity honour TextColumnCount on import?
  2. Does it honour TextColumnGutter, or substitute its own default?

Both answered yes, on Affinity Publisher for macOS: the counts arrive and
C and D come out visibly different, which is the only proof that the
gutter is applied rather than defaulted. Affinity's UI rounds the gutter
to one decimal, so the 2mm gap reads as 5.7pt against the 5.6664 in the
file -- display only. Re-run this probe if that ever stops being true.

Question 2 is why frames C and D exist. They have the same column count
and differ only in gutter -- 2mm, Publisher's default, against a
deliberately absurd 36pt. If they come out looking the same, the gutter is
being ignored and Publisher's column widths cannot be reproduced without
splitting frames by hand.

This drives `idml.IdmlWriter` itself, not a hand-built package, so what
Affinity opens is what the converter would really write.

    python3 research/probe_columns.py

Writes converted/probe/probe-columns.idml. Open it and read off:

  - A one wide block, B three columns, C two narrow-gapped columns,
    D two wide-gapped columns          -> columns work end to end.
  - Every frame one wide block         -> TextColumnCount is ignored;
                                          the README limitation stands.
  - C and D identical                  -> the count is honoured but the
                                          gutter is not.
  - Text flows across before down      -> the frame is being read as a
                                          grid rather than as columns.
  - Refuses to open                    -> TextFramePreference is
                                          malformed; a bug here, not an
                                          Affinity limitation.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# Publisher's default column spacing: 2mm, which is what every text object
# in the sample corpus reports.
PUBLISHER_GAP_PT = 0.0787 * 72.0

# Numbered tokens rather than lorem ipsum: the fill order of a column is
# only readable if the words are in a sequence the eye can follow.
BODY = " ".join(str(n) for n in range(1, 181))


def labelled_frame(label: str, columns: int, gap: float, **box) -> model.TextFrame:
    frame = model.TextFrame(columns=columns, column_gap=gap, **box)

    heading = model.Paragraph()
    heading.spans.append(model.Span(text=label, size_pt=11.0, font="Helvetica"))
    body = model.Paragraph()
    body.spans.append(model.Span(text=BODY, size_pt=9.0, font="Helvetica"))

    frame.story.paragraphs += [heading, body]
    return frame


def probe_document() -> model.Document:
    document = model.Document(title="Text column probe")
    page = model.Page(width=612.0, height=792.0)

    page.items.append(
        labelled_frame(
            "A - one column (control)", 1, 0.0,
            x=72.0, y=54.0, width=468.0, height=96.0,
        )
    )
    page.items.append(
        labelled_frame(
            "B - three columns, 2mm gutter", 3, PUBLISHER_GAP_PT,
            x=72.0, y=168.0, width=468.0, height=168.0,
        )
    )
    page.items.append(
        labelled_frame(
            "C - two columns, 2mm gutter", 2, PUBLISHER_GAP_PT,
            x=72.0, y=354.0, width=468.0, height=168.0,
        )
    )
    page.items.append(
        labelled_frame(
            "D - two columns, 36pt gutter (must not match C)", 2, 36.0,
            x=72.0, y=540.0, width=468.0, height=168.0,
        )
    )

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
        written = [
            (p.get("TextColumnCount"), p.get("TextColumnGutter"))
            for p in ET.fromstring(archive.read(spread)).iter("TextFramePreference")
        ]

    print(f"wrote {destination}")
    print("  frame  count  gutter")
    for label, (count, gutter) in zip("ABCD", written):
        print(f"    {label}      {count}      {gutter if gutter else '(none)'}")
    print("  open it and report whether A/B/C/D differ as labelled")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-columns.idml"))
