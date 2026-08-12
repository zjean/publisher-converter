"""Build a probe .idml that exercises the real master-spread writer.

No .pub in the sample set produces a master spread. Every scrap of master
content in the corpus is a page-number footer, and those deliberately
stay on their own page -- one copy on a master cannot read 1 on one page
and 2 on the next. So the code path that emits a MasterSpread is covered
by unit tests and by nothing else, which is exactly the position the
group-handling code was in when it turned out to be broken.

This drives `idml.IdmlWriter` itself, not a hand-built package, so what
Affinity opens is what the converter would really write.

    python3 research/probe_masterspread.py

Writes converted/probe/probe-masterspread.idml. Open it and read off:

  - "Running header from the master" and the rule beneath it on all three
    pages, plus each page's own line -> master spreads work end to end.
  - Own lines but no header                -> the master is ignored.
  - Header only on page one                -> it is being treated as page
    content rather than master content.
  - Refuses to open                        -> the MasterSpread part is
    malformed; a bug here, not an Affinity limitation.

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


def probe_document() -> model.Document:
    document = model.Document(title="MasterSpread probe")

    master = model.Master(name="A", width=612.0, height=792.0)
    header = model.TextFrame(x=72.0, y=60.0, width=468.0, height=24.0)
    paragraph = model.Paragraph()
    paragraph.spans.append(
        model.Span(text="Running header from the master", size_pt=14.0, font="Helvetica")
    )
    header.story.paragraphs.append(paragraph)
    master.items.append(header)
    master.items.append(
        model.Rectangle(
            x=72.0, y=88.0, width=468.0, height=2.0,
            style=model.GraphicStyle(fill=(0, 0, 0)),
        )
    )
    document.masters.append(master)

    for number in (1, 2, 3):
        page = model.Page(width=612.0, height=792.0, master="A")
        frame = model.TextFrame(x=72.0, y=300.0, width=468.0, height=40.0)
        own = model.Paragraph()
        own.spans.append(
            model.Span(text=f"Own content: page {number}", size_pt=18.0, font="Helvetica")
        )
        frame.story.paragraphs.append(own)
        page.items.append(frame)
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
        masters = [n for n in names if n.startswith("MasterSpreads/")]
        designmap = ET.fromstring(archive.read("designmap.xml"))
        referenced = [e.get("src") for e in designmap if e.get("src")]
        applied = [
            next(ET.fromstring(archive.read(n)).iter("Page")).get("AppliedMaster")
            for n in names if n.startswith("Spreads/")
        ]

    print(f"wrote {destination}")
    print(f"  master parts: {masters}")
    print(f"  every designmap reference resolves: {all(r in names for r in referenced)}")
    print(f"  AppliedMaster per page: {applied}")
    print(f"  fonts collected through the master: {document.fonts}")
    print("  open it and report whether the header shows on all three pages")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-masterspread.idml"))
