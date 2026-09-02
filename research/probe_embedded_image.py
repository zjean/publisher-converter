"""Build a probe .idml that asks whether Affinity reads an embedded image.

Every conversion currently ships a picture twice over: the `.idml` and a
`<name>_images/` folder beside it, linked by relative URI
(`idml.py:1771`, with `StoredState="Normal"`). That sidecar is the one way
a *successful* conversion still loses its pictures -- move the package
without the folder and the images are gone -- and it is why the wizard's
last screen has to spend three lines telling a non-technical reader to
keep the two together until they save in Affinity.

IDML does not require this. An image can carry its own bytes, and our
writer already advertises `CanEmbed="true"` (`idml.py:1779`) without ever
using it. Nothing in README, backlog or actions discusses embedding, so
linked-with-a-sidecar looks like the obvious first implementation rather
than a decision anyone weighed.

If Affinity honours embedded contents, the sidecar disappears entirely:
one self-contained file, no folder to keep beside it, and the warning
stops being load-bearing. The Save As step stays -- that is inherent to
IDML being an interchange format Affinity imports rather than edits --
but it stops being a data-loss trap and becomes ordinary housekeeping.

Affinity's importer is undocumented and cannot be unit-tested, so this
asks it directly.

## The design point

A picture that appears proves nothing if the link beside it also
resolves. So rows C and D have their `LinkResourceURI` pointed at a
filename that is deliberately absent from the sidecar: if their pictures
appear, the bytes can only have come from the embedded contents.

Row B is the same broken link with no embedded data, which is what makes
that inference safe -- it shows what Affinity does with a link it cannot
resolve, so C and D are read against it rather than against a guess.

Two encodings are tried in one round because an Affinity round-trip is
manual and slow, and the IDML spec does not settle which representation
its importer accepts.

    python3 research/probe_embedded_image.py

Writes converted/probe/probe-embedded-image.idml plus its
probe_images/ sidecar. Open it in Affinity and read off:

  - A shows a picture, B shows none          -> the probe is sound; read C and D.
  - A shows none                             -> the probe is broken, not Affinity.
                                                Stop; the sidecar or the placement
                                                is wrong and nothing below is valid.
  - C shows a picture                         -> base64 embedding works. Drop the
                                                sidecar: write Contents instead of
                                                a resolvable link.
  - D shows a picture                         -> hex embedding works; same
                                                conclusion, different encoder.
  - C and D both show pictures                -> either encoding is accepted; pick
                                                base64, it is a third smaller.
  - C and D show nothing, B shows nothing     -> Affinity ignores embedded contents.
                                                The sidecar is necessary and the
                                                warning has to stay. Record it in
                                                README's known limitations.
  - C or D shows a *placeholder* rather than
    a picture                                 -> the element is being parsed but the
                                                payload rejected; the encoding is
                                                wrong rather than the mechanism.
                                                Worth one more round with the other
                                                encoding fixed up.
"""

import base64
import binascii
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import idml, model  # noqa: E402

# A filename the sidecar will not contain, so a picture drawn from a row
# using it cannot have come through the link.
ABSENT = "probe_images/deliberately-absent.png"

ROWS = [
    ("A - linked, sidecar present (control: MUST show a picture)", "linked"),
    ("B - linked, file absent (control: must show nothing)", "broken"),
    ("C - embedded base64, link deliberately broken", "base64"),
    ("D - embedded hex, link deliberately broken", "hex"),
]


def checkerboard(size: int = 96, squares: int = 6) -> bytes:
    """A PNG no one could mistake for a rendering artefact.

    Written by hand rather than pulled from a library because this
    project has no third-party runtime dependencies and a probe should
    not be the first thing to add one. Magenta and yellow, because a
    picture that fails to load leaves grey or white and neither of those
    is in here.
    """
    step = size // squares
    rows = []
    for y in range(size):
        row = bytearray([0])  # PNG filter byte: none
        for x in range(size):
            dark = ((x // step) + (y // step)) % 2 == 0
            row += bytes((0xE8, 0x00, 0x8A) if dark else (0xFF, 0xD4, 0x00))
        rows.append(bytes(row))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", binascii.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">2I5B", size, size, 8, 2, 0, 0, 0)  # 8-bit truecolour
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def probe_document(picture: bytes) -> model.Document:
    document = model.Document(title="Embedded image probe")
    page = model.Page(width=612.0, height=792.0)

    for index, (label, _kind) in enumerate(ROWS):
        top = 72.0 + index * 168.0

        # A black rule under each slot, so an empty slot is visibly empty
        # rather than ambiguously white-on-white.
        page.items.append(
            model.Rectangle(
                x=144.0, y=top + 120.0, width=144.0, height=6.0,
                style=model.GraphicStyle(fill=(0x00, 0x00, 0x00)),
            )
        )
        page.items.append(
            model.Image(
                x=144.0, y=top, width=144.0, height=108.0,
                data=picture, mime_type="image/png",
            )
        )

        caption = model.TextFrame(x=306.0, y=top + 36.0, width=240.0, height=36.0)
        paragraph = model.Paragraph()
        paragraph.spans.append(model.Span(text=label, size_pt=10.0, font="Helvetica"))
        caption.story.paragraphs.append(paragraph)
        page.items.append(caption)

    document.pages.append(page)
    return document


def _embed(image: ET.Element, link: ET.Element, payload: bytes, encoding: str) -> str:
    """Turn one linked image into an embedded one, in place.

    `<Contents>` goes inside `<Properties>`, which is where IDML keeps an
    image's own data, and the link is left in place but marked embedded
    and pointed at nothing -- both because InDesign keeps the element and
    because a broken URI is what makes a drawn picture proof.
    """
    link.set("StoredState", "Embedded")
    link.set("LinkResourceURI", "file:" + ABSENT)

    if encoding == "base64":
        text = base64.b64encode(payload).decode("ascii")
    else:
        text = binascii.hexlify(payload).decode("ascii")

    properties = image.find("Properties")
    if properties is None:
        properties = ET.SubElement(image, "Properties")
    contents = ET.SubElement(properties, "Contents")
    contents.text = text
    return text


def build(destination: Path) -> None:
    picture = checkerboard()
    writer = idml.IdmlWriter(probe_document(picture), image_dir_name="probe_images")
    destination.parent.mkdir(parents=True, exist_ok=True)
    writer.write(destination)

    with zipfile.ZipFile(destination) as archive:
        names = archive.namelist()
        parts = {name: archive.read(name) for name in names}

    spread_name = next(n for n in names if n.startswith("Spreads/"))
    root = ET.fromstring(parts[spread_name])
    images = list(root.iter("Image"))
    if len(images) != len(ROWS):
        raise SystemExit(
            f"expected {len(ROWS)} images in the spread, found {len(images)} - "
            "the writer's layout changed and this probe needs rereading"
        )

    written = []
    for image, (label, kind) in zip(images, ROWS):
        link = image.find("Link")
        if kind == "linked":
            written.append((label, link.get("StoredState"), link.get("LinkResourceURI"), 0))
        elif kind == "broken":
            link.set("LinkResourceURI", "file:" + ABSENT)
            written.append((label, link.get("StoredState"), ABSENT, 0))
        else:
            text = _embed(image, link, picture, kind)
            written.append((label, "Embedded", ABSENT, len(text)))

    parts[spread_name] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    # Rewritten rather than patched in place: the mimetype entry must stay
    # first and uncompressed or the package is not an IDML at all.
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/vnd.adobe.indesign-idml-package",
            compress_type=zipfile.ZIP_STORED,
        )
        for name in names:
            if name != "mimetype":
                archive.writestr(name, parts[name])

    sidecar = destination.parent / "probe_images"
    present = sorted(p.name for p in sidecar.glob("*")) if sidecar.is_dir() else []

    print(f"wrote {destination} ({destination.stat().st_size} bytes)")
    print(f"  picture: {len(picture)} byte PNG, magenta/yellow checkerboard")
    print(f"  sidecar: {sidecar} -> {present}")
    print(f"  absent by design: {ABSENT.split('/')[-1]} "
          f"({'correct' if ABSENT.split('/')[-1] not in present else 'PRESENT - probe invalid'})")
    print()
    print("  row  stored state  link target                        contents chars")
    for label, state, uri, size in written:
        print(f"    {label[0]}  {state:<12}  {uri.split('/')[-1]:<32}  {size or '-'}")
    print()
    print("  open it in Affinity and report which rows show a picture")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1
               else "converted/probe/probe-embedded-image.idml"))
