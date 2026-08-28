"""What a face measures, as a line for fontmetrics.BAKED.

A machine without a font cannot measure it, and the two faces 46 of the
corpus's 48 headlines are set in -- Monotype Corsiva and Pristina -- ship
with Office rather than with either operating system. Run this where the
font is installed and paste the line it prints into `fontmetrics.BAKED`,
so a machine without the font gets that font's own proportions rather
than an average of every headline face.

    python3 -m research.font_metrics "Monotype Corsiva"
    python3 -m research.font_metrics "Pristina" --bold
"""
import argparse

from pubidml import fontmetrics

# A spread of headline words rather than one, so the average is not
# decided by whichever letters happen to be in a single example. These are
# the corpus's own headlines.
_SAMPLES = (
    "Kerkdiensten", "Meditatie", "Verjaardagen", "Uit de gemeente",
    "Schoonmaakrooster", "Activiteitenagenda", "Kerkelijke stand",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("family")
    parser.add_argument("--bold", action="store_true")
    parser.add_argument("--italic", action="store_true")
    args = parser.parse_args()

    face = fontmetrics.find_face(args.family, args.bold, args.italic)
    if face is None:
        print(f"{args.family!r} is not installed on this machine")
        return 1

    inks, advances, per_glyph = [], [], []
    for sample in _SAMPLES:
        found = face.measure(sample)
        if not found:
            continue
        ink, width, advance = found
        if ink:
            inks.append(ink)
        advances.append(advance)
        per_glyph.append(width / len(sample))
    if not advances:
        print(f"{args.family!r} covers none of the sample words")
        return 1

    print(f"# {face.family} {face.subfamily}, measured over "
          f"{len(advances)} headline(s)")
    print(f'    "{args.family.casefold()}": '
          f"({sum(inks) / len(inks):.3f}, "
          f"{sum(advances) / len(advances):.3f}, "
          f"{sum(per_glyph) / len(per_glyph):.3f}),")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
