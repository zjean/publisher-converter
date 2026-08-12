"""Length parsing and conversion.

librevenge emits lengths as strings with an explicit unit ("8.2677in",
"12pt", "3.5cm"). IDML works exclusively in points, so everything is
normalised to points on the way in.
"""

from __future__ import annotations

import re

PT_PER_INCH = 72.0

_UNIT_TO_PT = {
    "in": PT_PER_INCH,
    "pt": 1.0,
    "cm": PT_PER_INCH / 2.54,
    "mm": PT_PER_INCH / 25.4,
    "pc": 12.0,
    "px": 0.75,  # librevenge assumes 96 dpi for px
    "twip": 1.0 / 20.0,
    "%": 1.0,  # caller must interpret; kept so parsing does not fail
    "": 1.0,  # unitless values from librevenge are already inches
}

_LENGTH_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*([a-z%]*)\s*$")


def to_points(value, default=None):
    """Convert a librevenge length string to points.

    Unitless numbers are treated as inches, matching librevenge's
    RVNG_INCH default for double-valued properties.
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value) * PT_PER_INCH

    match = _LENGTH_RE.match(str(value))
    if not match:
        return default

    number, unit = match.groups()
    try:
        magnitude = float(number)
    except ValueError:
        return default

    if unit == "":
        return magnitude * PT_PER_INCH
    factor = _UNIT_TO_PT.get(unit)
    if factor is None:
        return default
    return magnitude * factor


def to_float(value, default=None):
    """Parse a bare number, tolerating a trailing unit or percent sign."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    match = _LENGTH_RE.match(str(value))
    if not match:
        return default
    try:
        return float(match.group(1))
    except ValueError:
        return default


def percent(value, default=None):
    """Parse a librevenge percentage ("0.5" or "50%") into a 0..1 fraction."""
    if value is None:
        return default
    text = str(value).strip()
    if text.endswith("%"):
        number = to_float(text[:-1])
        return default if number is None else number / 100.0
    return to_float(text, default)


def fmt(value):
    """Format a point value for IDML output.

    IDML tolerates plenty of precision; six decimals keeps files small
    without visibly shifting geometry.
    """
    return f"{float(value):.6f}".rstrip("0").rstrip(".") or "0"
