"""Build a probe .idml that asks whose rules a converted table draws.

We reference `TableStyle/$ID/[Basic Table]` and `CellStyle/$ID/[None]`
without defining either, and we write no cell strokes at all, so the
reader supplies the lot. Affinity's answer is a line around every cell --
seen unasked in `research/probe_cell_insets.py` -- and the corpus's tables
are layout grids, so that line prints a visible grid across an article.

Before any of Publisher's real weights and colours can be carried
(`actions.md` §9 needs a styled sample for those), one thing has to be
known that needs no sample at all:

  **does a per-cell edge stroke stated in the package beat the reader's
  default, and if so which way of stating "no line" does it?**

IDML offers two ways to say a cell edge has no rule -- a weight of zero
and a stroke colour of `Swatch/None` -- and a reader may honour either,
both or neither. It also matters whether an override lands *per edge*,
because that is the shape a styled Publisher table needs: `actions.md` §9
gives one cell a border on all four sides and its neighbour a border on
its top edge alone.

One page per treatment, the same 3 x 3 table on each, cells `1` to `9` so
this file can be held against §9's Publisher control directly. Treatment E
is what makes the rest readable: it asks for something impossible to miss,
so a page that does not change under it says the attributes never arrived,
rather than that zero was read as unset.

    python3 research/probe_cell_rules.py

Writes converted/probe/probe-cell-rules.idml. Open it and read off:

  - E magenta, B/C/D unruled     -> overrides win. Write the zeros for
                                    every table matched in the .pub and
                                    the reader's grid is gone; the
                                    surviving one of B/C/D says which
                                    attribute to write.
  - E magenta, B/C/D still ruled -> overrides arrive but zero is read as
                                    unset, exactly as the inset probe
                                    feared and disproved for insets.
                                    Then a table can only be unruled by
                                    defining `[Basic Table]` ourselves,
                                    which is the other lever in
                                    `backlog.md` §9.
  - E unchanged from A           -> no per-cell edge attribute reaches
                                    Affinity at all. Nothing to write
                                    here; the style definition is the
                                    only lever, and Publisher's real
                                    weights will need it too.
  - F rules one edge only        -> granularity is per edge, so §9's
                                    per-edge sample maps straight across.
  - F rules the whole cell, or
    nothing                      -> a stated edge does not stand alone;
                                    a styled table will need all four
                                    edges written whatever it states.
  - Refuses to open              -> the attribute names are wrong; a bug
                                    here, not a limitation.

Row 1 is the row F touches, and its neighbours state nothing, so F also
shows how Affinity settles an edge two cells share and disagree about.

### Answered, on Affinity Publisher for macOS

- **A** draws a line around every cell, which is the default this was
  asking about and the state every converted table ships in today.
- **B, C and D draw no lines at all.** All three spellings of "no rule"
  are honoured: a weight of zero, a stroke colour of `Swatch/None`, and
  the two together. A zero is not read as unset here, the same answer
  the insets gave.
- **E** draws 4pt magenta on all four edges of all nine cells, so a
  per-cell override beats the reader's default outright.
- **F** draws exactly one magenta line, the edge the first and second rows
  share, from a top edge stated on the lower cell alone. Granularity is
  per edge and a stated edge stands alone -- the cell above says nothing
  and does not override it.

So the reader's grid can be removed, and Publisher's real weights and
colours can be carried per edge once `actions.md` §9's styled sample says
what they are. What is still not known is whether a plain Publisher table
prints lines in the first place, which is §9's control file, not this
probe.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

MAGENTA = (255, 0, 255)
# How `idml._collect_resources` names a colour swatch. Checked against the
# package after writing rather than trusted: a reference that resolves to
# nothing would read on the page as "the override was ignored", which is
# one of the conclusions this probe is meant to reach honestly.
MAGENTA_REF = "Color/C_%02X%02X%02X" % MAGENTA

EDGES = ("Top", "Left", "Bottom", "Right")

# Loud on purpose: 4pt is §9's own weight, and magenta is a colour no
# default supplies.
LOUD = {
    "StrokeWeight": "4",
    "StrokeColor": MAGENTA_REF,
    "StrokeType": "StrokeStyle/$ID/Solid",
}

# (label, edges to state, attributes to state on each, rows to touch)
TREATMENTS = [
    ("A  nothing stated - the reader's own default", (), {}, None),
    ("B  weight 0, all four edges", EDGES, {"StrokeWeight": "0"}, None),
    ("C  colour None, all four edges", EDGES, {"StrokeColor": "Swatch/None"}, None),
    (
        "D  weight 0 and colour None, all four edges",
        EDGES,
        {"StrokeWeight": "0", "StrokeColor": "Swatch/None"},
        None,
    ),
    ("E  4pt magenta, all four edges", EDGES, LOUD, None),
    ("F  4pt magenta on row 1's top edge alone", ("Top",), LOUD, (1,)),
]

# Roomy enough that a 4pt rule and no rule cannot be confused, and the
# same grid §9's control file uses.
COLUMN_PT = 132.0
ROW_PT = 60.0
# Publisher's own 0.04in, stated rather than left out, so the text is not
# sitting on the rules and a default cannot creep in and confound the look.
INSET_PT = 0.04 * 72.0


def cell(row: int, column: int, text: str) -> model.TableCell:
    item = model.TableCell(
        row=row,
        column=column,
        insets=model.CellInsets(INSET_PT, INSET_PT, INSET_PT, INSET_PT),
    )
    paragraph = model.Paragraph()
    paragraph.spans.append(model.Span(text=text, size_pt=18.0, font="Helvetica"))
    item.story.paragraphs.append(paragraph)
    return item


def probe_table() -> model.Table:
    table = model.Table(
        x=72.0,
        y=72.0,
        width=COLUMN_PT * 3,
        height=ROW_PT * 3,
        column_widths=[COLUMN_PT] * 3,
        row_heights=[ROW_PT] * 3,
    )
    for row in range(3):
        for column in range(3):
            table.cells.append(cell(row, column, str(row * 3 + column + 1)))
    return table


def probe_document():
    """One page per treatment, plus what each page was told to state."""
    document = model.Document(title="Table cell rule probe")
    legend = []

    for label, edges, attributes, rows in TREATMENTS:
        page = model.Page(width=612.0, height=792.0)
        # A fresh table per page: the writer reads a table without holding
        # on to it, but each page's story is patched separately afterwards,
        # so nothing is shared that a patch could reach twice.
        page.items.append(probe_table())
        page.items.append(_caption(label, page.width, page.height))
        # The magenta the eye is looking for, and the only thing in the
        # document that puts that colour in the package's swatch list.
        page.items.append(
            model.Rectangle(
                x=72.0, y=page.height - 40.0, width=12.0, height=12.0,
                style=model.GraphicStyle(fill=MAGENTA),
            )
        )
        document.pages.append(page)
        legend.append((label, edges, attributes, rows))

    return document, legend


def _caption(text: str, page_width: float, page_height: float) -> model.TextFrame:
    frame = model.TextFrame(
        x=90.0, y=max(4.0, page_height - 40.0), width=page_width - 108.0, height=18.0
    )
    paragraph = model.Paragraph()
    paragraph.spans.append(model.Span(text=text, size_pt=9.0, font="Helvetica"))
    frame.story.paragraphs.append(paragraph)
    return frame


def build(destination: Path) -> None:
    document, legend = probe_document()
    writer = idml.IdmlWriter(document, image_dir_name="probe_images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)
    _apply_rules(destination, legend)
    _report(destination, legend)


def _story_order(name: str) -> int:
    return int("".join(character for character in name if character.isdigit()))


def _table_stories(parts) -> list:
    return sorted(
        (
            name for name in parts
            if name.startswith("Stories/") and b"<Table " in parts[name]
        ),
        key=_story_order,
    )


def _apply_rules(package: Path, legend) -> None:
    """State the cell edge strokes the writer has no way to state.

    `model.TableCell` carries insets and nothing else, deliberately: no
    table in the corpus records a rule, so there is nothing for a field to
    read. The attributes therefore go in here, on the package the real
    writer produced, which keeps the speculation in the probe.

    Story parts are numbered in the order they were written, which is the
    order the pages were built, so the nth table story takes the nth
    treatment.
    """
    with zipfile.ZipFile(package) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}

    tables = _table_stories(parts)
    if len(tables) != len(legend):
        raise SystemExit(f"expected {len(legend)} table stories, found {len(tables)}")

    for name, (_label, edges, attributes, rows) in zip(tables, legend):
        root = ET.fromstring(parts[name])
        for node in root.iter("Cell"):
            # A cell is named column first, then row.
            column, row = (int(part) for part in (node.get("Name") or "0:0").split(":"))
            if rows is not None and row not in rows:
                continue
            for edge in edges:
                for key, value in attributes.items():
                    node.set(f"{edge}Edge{key}", value)
        parts[name] = ET.tostring(root, encoding="UTF-8", xml_declaration=True)

    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as archive:
        # mimetype must stay first and stored, as it was written.
        if "mimetype" in parts:
            archive.writestr("mimetype", parts.pop("mimetype"), zipfile.ZIP_STORED)
        for name, payload in parts.items():
            archive.writestr(name, payload)


def _report(package: Path, legend) -> None:
    with zipfile.ZipFile(package) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}

    # Every part still has to parse: the patch rewrote the story XML, and a
    # package Affinity will not open answers nothing.
    for name, payload in parts.items():
        if name.endswith(".xml"):
            ET.fromstring(payload)

    graphic = parts["Resources/Graphic.xml"].decode("utf-8")
    if f'Self="{MAGENTA_REF}"' not in graphic:
        raise SystemExit(
            f"{MAGENTA_REF} is not defined in the package -- the loud treatment "
            "would show as no override at all"
        )

    print(f"wrote {package}  ({len(legend)} page(s))")
    for index, (name, (label, _edges, _attributes, rows)) in enumerate(
        zip(_table_stories(parts), legend)
    ):
        row = 0 if rows is None else rows[0]
        node = next(
            candidate for candidate in ET.fromstring(parts[name]).iter("Cell")
            if candidate.get("Name") == f"0:{row}"
        )
        stated = " ".join(
            f"{key}={value}" for key, value in sorted(node.attrib.items())
            if "Edge" in key
        )
        print(f"  page {index + 1}  {label:44} cell 0:{row}  {stated or '(nothing)'}")
    print("  open it and report which pages draw rules, and where")


if __name__ == "__main__":
    repo = Path(__file__).resolve().parent.parent
    build(
        Path(sys.argv[1]) if len(sys.argv) > 1
        else repo / "converted" / "probe" / "probe-cell-rules.idml"
    )
