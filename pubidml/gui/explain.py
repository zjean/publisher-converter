"""A ConversionError, said in Dutch to someone who cannot act on the
original.

Matched by prefix rather than equality. Most of convert.py's failures are
f-strings -- f"timed out after {PARSE_TIMEOUT_S}s", f"IDML write failed:
{exc}" -- so an exact lookup would miss the majority of them and fall
through to English inside a Dutch window.

An unmapped error is passed through verbatim rather than replaced. The
English is ugly there, and it is also the only clue a support
conversation has; hiding it to keep the surface tidy would cost more than
it buys. English appearing in the window is a missing row in this table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from .. import batch, convert
from . import strings

# The prefixes below are mutually non-shadowing: no entry's prefix is a
# prefix of another entry's. That, not the order they are written in, is
# what keeps the lookup below correct -- the first (and only) match for
# any real message is always the right one. Anyone adding a row must
# preserve that property, and if two prefixes ever do collide, put the
# more specific one first.
_TRANSLATIONS = [
    (
        "not a supported Publisher file",
        "dit is geen Publisher-bestand, of het bestand is beschadigd",
    ),
    (
        "libmspub could not parse the document",
        "Publisher-bestand kon niet gelezen worden",
    ),
    (
        "document contains no pages",
        "dit bestand bevat geen pagina's",
    ),
    (
        "timed out after",
        "dit bestand duurde te lang om te openen",
    ),
    (
        "pubdump binary missing",
        "het programma is niet compleet; download het opnieuw",
    ),
    (
        "could not launch parser",
        "het omzetprogramma kon niet gestart worden",
    ),
    (
        "parser could not write its event stream",
        "het omzetprogramma kon niet schrijven; is de schijf vol?",
    ),
    (
        "parser output was truncated",
        "het bestand werd maar half gelezen",
    ),
    (
        "malformed event stream",
        "het bestand werd niet goed gelezen",
    ),
    (
        "IDML write failed",
        "het omzetten lukte, maar het bestand kon niet opgeslagen worden",
    ),
]


@dataclass
class Counts:
    ok: int = 0
    review: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0


def error(message: str) -> str:
    """One Dutch sentence for a conversion failure."""
    if not message:
        return f"onbekende fout — {strings.ERROR_SEE_REPORT}"
    lowered = message.lower()
    for prefix, dutch in _TRANSLATIONS:
        if lowered.startswith(prefix.lower()):
            return dutch
    return f"{message} — {strings.ERROR_SEE_REPORT}"


def failure_line(result: convert.Result) -> str:
    """The filename and why it did not convert, on one line."""
    return f"{result.source.name} — {error(result.error or '')}"


def summary(results: List[convert.Result]) -> Counts:
    """How many results landed in each of batch.status_of's four buckets.

    total counts the results in hand, which is not the size of the batch:
    a cancelled run hands over what arrived. A caller writing a "x of y"
    line wants the number of files asked for, not this.

    The invariant this loop rests on: every string batch.status_of can
    return is the name of a field on Counts. A fifth status would raise
    AttributeError right here rather than being quietly dropped, which is
    the failure worth having -- and test_gui_explain asserts the
    correspondence so it is not discovered in front of a user.
    """
    counts = Counts(total=len(results))
    for result in results:
        status = batch.status_of(result)
        setattr(counts, status, getattr(counts, status) + 1)
    return counts
