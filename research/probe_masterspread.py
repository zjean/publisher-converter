"""Build a probe .idml that asks Affinity one question.

Does a MasterSpread we generate actually work? Everything about lifting
repeated master content out of the pages depends on the answer, and it
cannot be answered from this machine -- the only authority is Affinity
opening the file.

Deliberately not testing the auto-page-number marker. IDML is XML and
XML 1.0 forbids C0 control characters, so whatever InDesign uses for that
marker, it is not the raw character it is often described as. Guessing it
would make a failed probe ambiguous: no page number could mean the marker
is wrong *or* the master is wrong. This probe changes one thing.

    python3 research/probe_masterspread.py

Writes converted/probe/probe-masterspread.idml. Open it in Affinity and
read off:

  - Does "MASTER CONTENT" appear on all three pages?  -> master spreads
    work, and repeated content can be lifted onto one.
  - Does it appear on none, or only page one?          -> they do not,
    and master content must stay flattened per page as it is today.
  - Does the file refuse to open at all?               -> the MasterSpread
    part is malformed; that is a bug here, not an Affinity limitation.

Each page also carries its own line, so a page showing "Own content:
page 2" but no master line is distinguishable from a page that failed to
render at all.
"""

import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

IDPKG = idml.IDPKG
MASTER_PART = "MasterSpreads/MasterSpread_mA.xml"
MASTER_STORY = "Stories/Story_masterstory.xml"


def three_page_document() -> model.Document:
    document = model.Document(title="MasterSpread probe")
    for number in range(1, 4):
        page = model.Page(width=612.0, height=792.0)
        frame = model.TextFrame(x=72.0, y=200.0, width=400.0, height=40.0)
        paragraph = model.Paragraph()
        paragraph.spans.append(
            model.Span(text=f"Own content: page {number}", size_pt=18.0)
        )
        frame.story.paragraphs.append(paragraph)
        page.items.append(frame)
        document.pages.append(page)
    return document


def master_spread_part() -> bytes:
    """A MasterSpread holding one text frame, in the same shape as a Spread."""
    root = ET.Element(
        "idPkg:MasterSpread", {"xmlns:idPkg": IDPKG, "DOMVersion": idml.DOM_VERSION}
    )
    spread = ET.SubElement(
        root,
        "MasterSpread",
        {
            "Self": "mA",
            "Name": "A-Master",
            "NamePrefix": "A",
            "BaseName": "Master",
            "ShowMasterItems": "true",
            "PageCount": "1",
            "OverriddenPageItemProps": "",
            "ItemTransform": "1 0 0 1 0 0",
        },
    )
    ET.SubElement(
        spread,
        "Page",
        {
            "Self": "mApage",
            "Name": "A",
            "AppliedMaster": "n",
            "OverrideList": "",
            "GeometricBounds": "0 0 792 612",
            "ItemTransform": "1 0 0 1 -306 -396",
            "AppliedTrapPreset": "TrapPreset/$ID/kDefaultTrapStyleName",
            "GridStartingPoint": "TopOutside",
            "UseMasterGrid": "true",
        },
    )
    frame = ET.SubElement(
        spread,
        "TextFrame",
        {
            "Self": "masterframe",
            "ParentStory": "masterstory",
            "PreviousTextFrame": "n",
            "NextTextFrame": "n",
            "ContentType": "TextType",
            "ItemTransform": "1 0 0 1 0 -250",
            "AppliedObjectStyle": "ObjectStyle/$ID/[Normal Text Frame]",
            "FillColor": "Swatch/None",
            "StrokeColor": "Swatch/None",
            "StrokeWeight": "0",
            "Visible": "true",
        },
    )
    properties = ET.SubElement(frame, "Properties")
    idml._rect_path(properties, 400.0, 40.0)
    ET.SubElement(
        frame,
        "TextFramePreference",
        {"TextColumnCount": "1", "VerticalJustification": "TopAlign",
         "InsetSpacing": "0 0 0 0", "AutoSizingType": "Off"},
    )
    return idml._serialise(root)


def master_story_part() -> bytes:
    root = ET.Element("idPkg:Story", {"xmlns:idPkg": IDPKG, "DOMVersion": idml.DOM_VERSION})
    story = ET.SubElement(
        root,
        "Story",
        {"Self": "masterstory", "AppliedTOCStyle": "n", "TrackChanges": "false",
         "StoryTitle": "", "AppliedNamedGrid": "n"},
    )
    ET.SubElement(
        story,
        "StoryPreference",
        {"OpticalMarginAlignment": "false", "OpticalMarginSize": "12",
         "FrameType": "TextFrameType", "StoryOrientation": "Horizontal",
         "StoryDirection": "LeftToRightDirection"},
    )
    paragraph = ET.SubElement(
        story,
        "ParagraphStyleRange",
        {"AppliedParagraphStyle": idml.NO_PARAGRAPH_STYLE, "Justification": "LeftAlign"},
    )
    run = ET.SubElement(
        paragraph,
        "CharacterStyleRange",
        {"AppliedCharacterStyle": idml.NO_CHARACTER_STYLE,
         "PointSize": "24", "FillColor": "Color/Black"},
    )
    ET.SubElement(run, "Content").text = "MASTER CONTENT"
    return idml._serialise(root)


def build(destination: Path) -> None:
    writer = idml.IdmlWriter(three_page_document(), image_dir_name="probe_images")
    writer._build()

    parts = dict(writer._parts)
    parts[MASTER_PART] = master_spread_part()
    parts[MASTER_STORY] = master_story_part()

    # designmap must list the new parts, and every page must apply the master.
    designmap = ET.fromstring(parts["designmap.xml"])
    ET.SubElement(designmap, "idPkg:MasterSpread", {"src": MASTER_PART})
    ET.SubElement(designmap, "idPkg:Story", {"src": MASTER_STORY})
    designmap.set("StoryList", (designmap.get("StoryList", "") + " masterstory").strip())
    parts["designmap.xml"] = idml._serialise(designmap, processing_instruction=True)

    for name in list(parts):
        if not name.startswith("Spreads/"):
            continue
        spread = ET.fromstring(parts[name])
        for page in spread.iter("Page"):
            page.set("AppliedMaster", "mA")
        parts[name] = idml._serialise(spread)

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/vnd.adobe.indesign-idml-package",
            compress_type=zipfile.ZIP_STORED,
        )
        for name, payload in parts.items():
            archive.writestr(name, payload)

    with zipfile.ZipFile(destination) as archive:
        for name in archive.namelist():
            if name.endswith(".xml"):
                ET.fromstring(archive.read(name))
    print(f"wrote {destination}")
    print("  every part re-parses; 3 pages, each applying master 'mA'")
    print("  open it and report whether 'MASTER CONTENT' shows on all three")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-masterspread.idml"))
