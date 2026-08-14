"""Build a probe .idml that asks how Affinity sizes and places a table.

Each page holds one real table from the corpus, drawn over a magenta
rectangle at exactly the position and size Publisher gives it. The two are
written to the same coordinates, so **any magenta showing past the table,
or any table showing past the magenta, is the reader disagreeing with the
file.**

    python3 research/probe_table_placement.py [source.pub] [--all] [--plain]

Defaults to the first table of `files/cgk/1336 kerkbode.pub` -- four
pages, one per treatment, which is the whole question in one look.
`--all` does every table in the file; `--plain` writes each table once, as
the converter really writes it, with no treatments applied.

### Settled so far

- **Position is right.** A synthetic table -- 288 x 144pt, three equal
  rows -- lands square on its rectangle. Frames go where they are put.
- **Pinning the first baseline is harmful.** `FirstBaselineOffset =
  "FixedHeight"` with `MinimumFirstBaselineOffset = "0"` reads like the
  right thing for a table, which has no baseline to offset. Affinity
  answers it by lifting the whole table *a full frame height* off its
  position. Tried, measured, taken back out.
- **Rows grow.** Real tables render taller than the height Publisher
  states. On the corpus's first table the six rows that carry text absorb
  about 32pt between them and push the empty seventh row clean out of the
  magenta. The text wraps to more lines than Publisher laid out -- font
  substitution, most likely -- and `SingleRowHeight` is being read as a
  minimum rather than a height.

### What this round asks

Which way of saying "this row is this tall" Affinity actually honours.
The same table repeats once per treatment, each on its own rectangle:

  A  as written now              SingleRowHeight only
  B  growth switched off         + AutoGrow="false"
  C  ceiling stated              + MaximumHeight = the row height
  D  both                        + AutoGrow="false" and MaximumHeight

Read it off:

  - B, C or D sits inside its magenta where A overflows
                          -> that is the attribute to write, and the
                             table keeps Publisher's geometry.
  - The winning one clips or hides cell text
                          -> it works but costs content. Prefer it only
                             with a warning naming the tables affected,
                             the way every other loss here is named.
  - All four overflow identically
                          -> row height cannot be fixed from IDML in
                             Affinity. Then the honest fix is the other
                             direction: grow the *frame* to fit the table
                             so nothing is hidden, and say so in the
                             report.
  - Black gridlines everywhere
                          -> unrelated and already known: we write no cell
                             strokes, so [Basic Table] supplies its own.
                             See backlog.md 9.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import convert, idml, model, pubfile, textrepair  # noqa: E402

DEFAULT_SOURCE = Path("files/cgk/1336 kerkbode.pub")
MAGENTA = (255, 0, 255)

# (label, AutoGrow, state a MaximumHeight)
TREATMENTS = [
    ("A  as written now", None, False),
    ("B  AutoGrow=false", "false", False),
    ("C  MaximumHeight stated", None, True),
    ("D  AutoGrow=false + MaximumHeight", "false", True),
]


def real_tables(source: Path):
    """Every table the converter would write, with the page it sits on.

    Driven through the same passes `convert` runs, so the geometry here is
    the geometry that ships -- not a reconstruction of it.
    """
    document = convert.parse_document(source)
    textrepair.repair_document(document, "auto")
    structure = pubfile.read_structure(source)
    convert._apply_master_pages(document, structure)
    convert._apply_cell_insets(document, structure)

    found = []
    for index, page in enumerate(document.pages):
        for item in model._walk(page.items):
            if isinstance(item, model.Table):
                found.append((index, page.width, page.height, item))
    return found


def probe_document(source: Path, plain: bool, every: bool):
    """One page per table per treatment, plus what each page is showing."""
    document = model.Document(title=f"Table probe - {source.name}")
    legend = []

    tables = real_tables(source)
    for origin, width, height, table in (tables if every else tables[:1]):
        for label, auto_grow, ceiling in ([TREATMENTS[0]] if plain else TREATMENTS):
            page = model.Page(width=width, height=height)
            page.items.append(
                model.Rectangle(
                    x=table.x, y=table.y, width=table.width, height=table.height,
                    style=model.GraphicStyle(fill=MAGENTA),
                )
            )
            # The same table object on every page: the writer reads it and
            # never holds on to it, so one instance is enough.
            page.items.append(table)
            caption = (
                f"{label}   -   source page {origin + 1}, "
                f"{table.column_count}c x {table.row_count}r, "
                f"{table.width:.1f} x {table.height:.1f}pt"
            )
            page.items.append(_caption(caption, width, height))
            document.pages.append(page)
            legend.append((auto_grow, ceiling))

    return document, legend


def _caption(text: str, page_width: float, page_height: float) -> model.TextFrame:
    frame = model.TextFrame(
        x=18.0, y=max(4.0, page_height - 26.0), width=page_width - 36.0, height=18.0
    )
    paragraph = model.Paragraph()
    paragraph.spans.append(model.Span(text=text, size_pt=7.0, font="Helvetica"))
    frame.story.paragraphs.append(paragraph)
    return frame


def build(source: Path, destination: Path, plain: bool, every: bool) -> None:
    document, legend = probe_document(source, plain, every)
    if not document.pages:
        raise SystemExit(f"{source} has no tables to probe")

    writer = idml.IdmlWriter(document, image_dir_name="probe_images")
    writer.write(destination)
    if not plain:
        _apply_treatments(destination, legend)
    _report(destination, document, legend, plain)


def _apply_treatments(package: Path, legend) -> None:
    """Vary the Row attributes the writer does not vary on its own.

    Story parts are numbered in the order they were written, which is the
    order the pages were built, so the nth table story takes the nth
    treatment.
    """
    with zipfile.ZipFile(package) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}

    tables = [
        name for name in parts
        if name.startswith("Stories/") and b"<Table " in parts[name]
    ]
    tables.sort(key=lambda name: int("".join(c for c in name if c.isdigit())))
    if len(tables) != len(legend):
        raise SystemExit(f"expected {len(legend)} table stories, found {len(tables)}")

    for name, (auto_grow, ceiling) in zip(tables, legend):
        root = ET.fromstring(parts[name])
        for row in root.iter("Row"):
            if auto_grow is not None:
                row.set("AutoGrow", auto_grow)
            if ceiling:
                row.set("MaximumHeight", row.get("SingleRowHeight"))
        parts[name] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        # mimetype must stay first and stored, as it was written.
        if "mimetype" in parts:
            archive.writestr("mimetype", parts.pop("mimetype"), zipfile.ZIP_STORED)
        for name, payload in parts.items():
            archive.writestr(name, payload)


def _report(package: Path, document, legend, plain: bool) -> None:
    print(f"wrote {package}  ({len(document.pages)} page(s))")
    with zipfile.ZipFile(package) as archive:
        tables = sorted(
            (n for n in archive.namelist()
             if n.startswith("Stories/") and b"<Table " in archive.read(n)),
            key=lambda n: int("".join(c for c in n if c.isdigit())),
        )
        for index, name in enumerate(tables):
            row = next(ET.fromstring(archive.read(name)).iter("Row"))
            label = "as written" if plain else TREATMENTS[index % len(TREATMENTS)][0]
            print(
                f"  page {index + 1:>2}  {label:34} "
                f"SingleRowHeight={row.get('SingleRowHeight')} "
                f"AutoGrow={row.get('AutoGrow', '(unset)')} "
                f"MaximumHeight={row.get('MaximumHeight', '(unset)')}"
            )


if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent
    flags = {"--plain", "--all"}
    arguments = [a for a in sys.argv[1:] if a not in flags]
    source = Path(arguments[0]) if arguments else repo / DEFAULT_SOURCE
    output = repo / "converted" / "probe"
    output.mkdir(parents=True, exist_ok=True)
    build(
        source,
        output / "probe-table-placement.idml",
        "--plain" in sys.argv,
        "--all" in sys.argv,
    )
