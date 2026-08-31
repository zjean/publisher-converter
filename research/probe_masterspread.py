"""Build probe .idml packages that exercise the real master-spread writer.

No .pub in the sample set produces a master spread. Every scrap of master
content in the corpus is a page-number footer, and those deliberately
stay on their own page -- one copy on a master cannot read 1 on one page
and 2 on the next. So the code path that emits a MasterSpread is covered
by unit tests and by nothing else, which is exactly the position the
group-handling code was in when it turned out to be broken.

This drives `idml.IdmlWriter` itself, not a hand-built package, so what
Affinity opens is what the converter would really write.

    python3 research/probe_masterspread.py

Writes two packages into converted/probe/:

  probe-masterspread.idml
      Three single pages on one master. Read off:

      - "Running header from the master" and the rule beneath it on all
        three pages, plus each page's own line -> master spreads work.
      - Own lines but no header       -> the master is ignored.
      - Header only on page one       -> it is being treated as page
                                         content rather than master
                                         content.
      - Refuses to open               -> the MasterSpread part is
                                         malformed; a bug here, not an
                                         Affinity limitation.

  probe-masterspread-facing.idml
      The same master on six A5 pages laid out facing, which is the
      booklet case and the one that matters. The master's mark is set
      18pt from the page's own left edge, which puts it tight to the
      spine on a right-hand page and tight to the outer trim on a
      left-hand one -- a Publisher master is not mirrored and neither is
      this. What that buys is a half-page error being unmissable, since
      half of A5 lands the mark in open space mid-page. Read off:

      - The bar 18pt inside the left edge of all six pages
                                      -> the facing geometry is right.
      - The bar adrift by ~210pt, and adrift the other way on the
        left-hand page than on the right
                                      -> the master page is not on the
                                         side of the spine its pages are.
      - The bar on rectos only, or on versos only
                                      -> a two-page master spread is not
                                         being mapped side to side.

Deliberately not testing the auto-page-number marker. IDML is XML and
XML 1.0 forbids C0 control characters, so whatever InDesign uses for that
marker it is not the raw character it is usually described as. Guessing
it would make a blank result ambiguous between a wrong marker and a wrong
master, and the marker is not needed anyway: page-number frames stay on
their page carrying a number already resolved from the .pub.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402


def _labelled_frame(text: str, x: float, y: float, width: float, size: float):
    frame = model.TextFrame(x=x, y=y, width=width, height=size * 2.0)
    paragraph = model.Paragraph()
    paragraph.spans.append(model.Span(text=text, size_pt=size, font="Helvetica"))
    frame.story.paragraphs.append(paragraph)
    return frame


def single_page_document() -> model.Document:
    """Three US Letter pages, one master, no facing layout."""
    document = model.Document(title="MasterSpread probe")

    master = model.Master(name="A", width=612.0, height=792.0)
    master.items.append(
        _labelled_frame("Running header from the master", 72.0, 60.0, 468.0, 14.0)
    )
    master.items.append(
        model.Rectangle(
            x=72.0, y=88.0, width=468.0, height=2.0,
            style=model.GraphicStyle(fill=(0, 0, 0)),
        )
    )
    document.masters.append(master)

    for number in (1, 2, 3):
        page = model.Page(width=612.0, height=792.0, master="A")
        page.items.append(
            _labelled_frame(f"Own content: page {number}", 72.0, 300.0, 468.0, 18.0)
        )
        document.pages.append(page)

    return document


def facing_document() -> model.Document:
    """Six A5 pages laid out facing, on one master applied to every page.

    The master's mark sits 18pt inside the page's own left edge. The
    writer puts a page of the master on each side of the spine and writes
    the content once per side (`idml.IdmlWriter._master_sides`); if either
    half of that is wrong the mark lands a half page out, and in opposite
    directions on the two sides of a spread.
    """
    width, height = 421.0, 595.0
    document = model.Document(title="MasterSpread probe (facing)")

    master = model.Master(name="A", width=width, height=height)
    master.items.append(
        _labelled_frame("18pt from the left edge >", 18.0, 40.0, 200.0, 10.0)
    )
    master.items.append(
        model.Rectangle(
            x=18.0, y=64.0, width=6.0, height=height - 128.0,
            style=model.GraphicStyle(fill=(200, 0, 0)),
        )
    )
    document.masters.append(master)

    for number in range(1, 7):
        page = model.Page(width=width, height=height, master="A")
        page.items.append(
            _labelled_frame(f"Own content: page {number}", 40.0, 280.0, 340.0, 18.0)
        )
        document.pages.append(page)

    return document


def build(destination: Path, document: model.Document, facing: bool) -> None:
    writer = idml.IdmlWriter(
        document, image_dir_name=f"{destination.stem}_images", facing_pages=facing
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        for name in names:
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))
        parts = sorted(n for n in names if n.startswith("MasterSpreads/"))
        designmap = ET.fromstring(archive.read("designmap.xml"))
        referenced = [e.get("src") for e in designmap if e.get("src")]

        master_pages = [
            (page.get("ItemTransform"), page.get("MasterPageTransform"))
            for part in parts
            for page in ET.fromstring(archive.read(part)).iter("Page")
        ]
        applied = [
            (page.get("Name"), page.get("AppliedMaster"),
             page.get("ItemTransform").split()[4])
            for name in sorted(n for n in names if n.startswith("Spreads/"))
            for page in ET.fromstring(archive.read(name)).iter("Page")
        ]

    print(f"wrote {destination}")
    print(f"  master parts: {parts}")
    print(f"  every designmap reference resolves: {all(r in names for r in referenced)}")
    print("  master page ItemTransform / MasterPageTransform:")
    for transform, carried in master_pages:
        print(f"    {transform}   {carried}")
    print("  page -> master, and the page's own spread offset:")
    for number, master, offset in applied:
        print(f"    page {number}: {master} at x={offset}")
    print(f"  fonts collected through the master: {document.fonts}")
    # A master page and every page applying it must sit at the same offset,
    # since MasterPageTransform above is written as the identity.
    master_offsets = {transform.split()[4] for transform, _ in master_pages}
    page_offsets = {offset for _number, _master, offset in applied}
    print(f"  master offsets {sorted(master_offsets)} cover page offsets "
          f"{sorted(page_offsets)}: {page_offsets <= master_offsets}")


if __name__ == "__main__":
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "converted/probe")
    build(root / "probe-masterspread.idml", single_page_document(), facing=False)
    print()
    build(root / "probe-masterspread-facing.idml", facing_document(), facing=True)
