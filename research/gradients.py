"""Print every gradient a .pub states, beside what libmspub reports of it.

The evidence behind backlog §13. libmspub reads a gradient's fill colour,
its fill-back colour and its waypoint list, and then builds the ramp from
the waypoint list alone -- so both ends are dropped from every gradient
that has waypoints, and one with a single waypoint collapses to a stop
that cannot ramp at all.

    python3 research/gradients.py files

Per shape it prints the file's reading and libmspub's side by side:

  file:     type, focus, angle, the two end colours, and the waypoints in
            the order the file holds them
  libmspub: the ramp that reached the event stream

Three things to read off it. Whether the two agree on the waypoints --
they must, since `pubfile` applies the same focus rule, and where they
stop agreeing something in that rule is wrong for this file. Whether a
fill type other than 7 turns up, since only the shaded types are handled
and only 7 appears in the corpus. And whether a focus other than 0 or 100
appears *with* a waypoint list, which is the one case deliberately left
unreconstructed for want of anything to check it against.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pubidml import convert, model, pubfile  # noqa: E402

FILL_TYPE, FILL_COLOR, FILL_BACK, SHADE = 0x0180, 0x0181, 0x0183, 0x0197


def shade_shapes(path: Path):
    """Every shape stating a shaded fill, with its raw fields."""
    data = path.read_bytes()
    contents = pubfile._read_stream(data, *pubfile._CONTENTS_STREAM)
    if not contents:
        return []
    palette = pubfile._read_palette(contents, pubfile._chunk_references(contents))
    escher = pubfile._read_stream(data, *pubfile._ESCHER_STREAM)
    if not escher:
        return []

    out = []
    for body, end in pubfile._escher_shapes(escher, 0, len(escher)):
        props, box = {}, None
        for _v, instance, rec_type, sub_body, sub_end in pubfile._escher_records(
            escher, body, end
        ):
            if rec_type in pubfile._PROPERTY_RECORDS:
                props.update(
                    pubfile._escher_properties(escher, sub_body, sub_end, instance)
                )
            elif rec_type == pubfile._CLIENT_ANCHOR:
                anchor = pubfile._escher_values(escher, sub_body, sub_end)
                if all(side in anchor for side in pubfile._ANCHOR_SIDES):
                    box = [
                        pubfile._signed(anchor[side]) / pubfile._EMU_PER_POINT
                        for side in pubfile._ANCHOR_SIDES
                    ]
        if props.get(FILL_TYPE) is None or box is None:
            continue
        fill = props.get(FILL_COLOR, 0)
        out.append({
            "type": props[FILL_TYPE],
            "focus": (
                pubfile._signed(props[pubfile._PROP_FILL_FOCUS])
                if pubfile._PROP_FILL_FOCUS in props else None
            ),
            "angle": pubfile._gradient_angle(props),
            "fill": pubfile._resolve_color(fill, fill, palette),
            "back": pubfile._resolve_color(props.get(FILL_BACK, 0), fill, palette),
            "waypoints": pubfile._shade_stops(props.get(SHADE), palette, fill),
            "centre": ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0),
            "size": (box[2] - box[0], box[3] - box[1]),
        })
    return out


def swatch(colour) -> str:
    return "#%02x%02x%02x" % colour if colour else "-"


def ramp(stops) -> str:
    return " ".join(f"{position * 100:.0f}%{swatch(colour)}" for position, colour in stops)


def dump(path: Path) -> None:
    shapes = shade_shapes(path)
    if not shapes:
        print(f"\n=== {path.name} ===  no shaded fills")
        return

    kinds = Counter(shape["type"] for shape in shapes)
    focuses = Counter(
        shape["focus"] for shape in shapes if shape["waypoints"]
    )
    print(f"\n=== {path.name} ===  {len(shapes)} shaded fills; "
          f"types {dict(kinds)}; focus where waypoints are stated {dict(focuses)}")

    document = convert.parse_document(path)
    structure = pubfile.read_structure(path)
    for index, page in enumerate(document.pages, 1):
        for item in model._walk(page.items):
            if item.style.gradient is None and not item.style.approximated_fill:
                continue
            centre = (
                item.x + item.width / 2.0 - page.width / 2.0,
                item.y + item.height / 2.0 - page.height / 2.0,
            )
            near = [
                shape for shape in shapes
                if abs(shape["centre"][0] - centre[0]) <= 0.5
                and abs(shape["centre"][1] - centre[1]) <= 0.5
            ]
            near.sort(key=lambda s: abs(s["size"][0] - item.width)
                      + abs(s["size"][1] - item.height))
            print(f"\n  page {index}  {item.width:6.1f}x{item.height:5.1f}")
            if near:
                shape = near[0]
                print(f"    file:     type={shape['type']} focus={shape['focus']} "
                      f"angle={shape['angle']:.0f} "
                      f"fill={swatch(shape['fill'])} back={swatch(shape['back'])}")
                print(f"              waypoints {ramp(shape['waypoints']) or '-'}")
            else:
                print("    file:     nothing stated at that centre")
            if item.style.gradient is not None:
                stops = [
                    (stop.location / 100.0, stop.color)
                    for stop in item.style.gradient.stops
                ]
                print(f"    libmspub: angle={item.style.gradient.angle:.0f} {ramp(stops)}")
            else:
                print(f"    libmspub: FLAT {swatch(item.style.fill)}")
            found = (
                structure.gradient_for(*centre, item.width, item.height)
                if structure is not None else None
            )
            if found is not None:
                print(f"    ours:     angle={model._fold_angle(found.angle):.0f} "
                      f"{ramp(found.stops)}")
            else:
                print("    ours:     nothing to replace it with -- "
                      "no ramp stated here, or two shapes equally close")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    for argument in sys.argv[1:]:
        target = Path(argument)
        for source in sorted(target.rglob("*.pub")) if target.is_dir() else [target]:
            dump(source)
