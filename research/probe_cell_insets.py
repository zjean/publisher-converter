"""Build a probe .idml that exercises per-cell insets on a table.

Publisher records four insets per table cell and libmspub drops all four
-- its own source marks that part of the record "width/height of content +
margins?" and skips it -- so `pubfile` reads them out of the .pub
directly. That half is proven against the corpus: 18 tables, 974 cells,
every one matched to the chunk it came from. What the corpus cannot prove
is the other half.

Two things needed answering, and only Affinity could answer them:

  1. Does Affinity honour LeftInset/TopInset/RightInset/BottomInset on a
     Cell, or substitute its own default?
  2. Does an inset of zero survive, or is a zero read as "unset" and
     replaced?

Question 2 is the one that matters most in practice. The newsletters get
their two-column look from layout tables whose gutter column is an eighth
of an inch wide -- 9pt, narrower than two default insets put together --
so a default applied there leaves no room for text at all.

Rows A to D differ only in their insets, and each row says what it should
look like. This drives `idml.IdmlWriter` itself, not a hand-built
package, so what Affinity opens is what the converter would really write.

    python3 research/probe_cell_insets.py

Writes converted/probe/probe-cell-insets.idml. Open it and read off:

  - A tight, B loose, C hugging its left edge, D hugging its right
                                       -> insets work end to end.
  - Every row identically padded        -> the attributes are ignored;
                                          Publisher's padding cannot be
                                          carried and the README
                                          limitation stands.
  - A padded like B                     -> zero is being read as unset,
                                          and a cell with no insets of
                                          its own cannot be written as
                                          one that has none.
  - The gutter column shows no text     -> the default is still winning
                                          somewhere; check the Cell
                                          attributes in the package.
  - Refuses to open                     -> the attribute names are wrong;
                                          a bug here, not a limitation.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# What the corpus actually holds: Publisher's own 0.04in default, the
# 0.75pt one its imported tables use, and nothing at all.
PUBLISHER_DEFAULT_PT = 0.04 * 72.0
HAIRLINE_PT = 0.75

# An eighth of an inch: the gutter column the newsletters' layout tables
# use, and the case a reader's default padding destroys.
GUTTER_PT = 0.125 * 72.0

ROWS = [
    ("A - no insets at all (must sit tight to every edge)", model.CellInsets()),
    (
        "B - 0.04in all round, Publisher's default (visibly padded)",
        model.CellInsets(
            left=PUBLISHER_DEFAULT_PT, top=PUBLISHER_DEFAULT_PT,
            right=PUBLISHER_DEFAULT_PT, bottom=PUBLISHER_DEFAULT_PT,
        ),
    ),
    (
        "C - 24pt right inset only (text hugs the left edge)",
        model.CellInsets(right=24.0),
    ),
    (
        "D - 24pt left inset only (text hugs the right edge)",
        model.CellInsets(left=24.0),
    ),
]


def cell(row: int, column: int, text: str, insets: model.CellInsets) -> model.TableCell:
    item = model.TableCell(row=row, column=column, insets=insets)
    paragraph = model.Paragraph()
    paragraph.spans.append(model.Span(text=text, size_pt=9.0, font="Helvetica"))
    item.story.paragraphs.append(paragraph)
    return item


def probe_document() -> model.Document:
    document = model.Document(title="Table cell inset probe")
    page = model.Page(width=612.0, height=792.0)

    # Three columns, the middle one a Publisher-width gutter carrying text
    # of its own, so the narrow case is visible rather than argued about.
    table = model.Table(
        x=72.0, y=72.0,
        width=396.0 + GUTTER_PT,
        height=4 * 72.0,
        column_widths=[264.0, GUTTER_PT, 132.0],
        row_heights=[72.0, 72.0, 72.0, 72.0],
    )

    for index, (label, insets) in enumerate(ROWS):
        table.cells += [
            cell(index, 0, label, insets),
            cell(index, 1, "iii", model.CellInsets()),
            cell(index, 2, f"{index}: right-hand cell", insets),
        ]

    page.items.append(table)
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
        story = next(n for n in names if n.startswith("Stories/"))
        written = [
            (
                node.get("Name"),
                node.get("LeftInset"), node.get("TopInset"),
                node.get("RightInset"), node.get("BottomInset"),
            )
            for node in ET.fromstring(archive.read(story)).iter("Cell")
            if node.get("Name", "").startswith("0:")
        ]

    print(f"wrote {destination}")
    print("  row  left  top  right  bottom")
    for label, (_name, left, top, right, bottom) in zip("ABCD", written):
        print(f"    {label}  {left:>5} {top:>4} {right:>6} {bottom:>7}")
    print("  open it and report whether A/B/C/D differ as labelled")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-cell-insets.idml"))
