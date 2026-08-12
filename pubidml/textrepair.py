"""Recovery of text that libmspub decoded with the wrong code page.

libmspub maps each text byte straight to the Unicode code point of the
same value — effectively decoding everything as Latin-1 — instead of
honouring the document's code page. For Western European documents that
is harmless, because CP1252 and Latin-1 agree across the accented range.
For anything else the text arrives as mojibake: the Russian sample in the
libmspub corpus reads "Ðóññêèé òåêñò" where it should read
"Русский текст".

The original bytes survive intact, so the damage is reversible: encode
back to Latin-1 to recover them, then decode with the code page that was
actually meant.

Picking that code page is the hard part, and guessing wrong would destroy
correct text. The detector is therefore deliberately conservative and
leans on one strong signal: in genuine Western European text, accented
letters are rare and isolated, while mojibake from a non-Latin script
makes nearly every letter non-ASCII. Documents that look like ordinary
Latin text are left completely untouched.
"""

from __future__ import annotations

import codecs
from collections import Counter
from typing import Iterable, List, Optional

# Legacy code pages Publisher actually wrote, ordered by how commonly
# they appear in the wild.
CANDIDATE_CODE_PAGES = [
    "cp1251",  # Cyrillic
    "cp1253",  # Greek
    "cp1250",  # Central European
    "cp1254",  # Turkish
    "cp1257",  # Baltic
    "cp1255",  # Hebrew
    "cp1256",  # Arabic
    "cp932",   # Japanese
    "cp936",   # Simplified Chinese
    "cp949",   # Korean
    "cp950",   # Traditional Chinese
]

# Above this share of non-ASCII letters the text cannot plausibly be
# Western European prose, so re-decoding is worth attempting.
_SUSPICION_THRESHOLD = 0.30

# The sharper of the two guards. Accents in Western European text are
# isolated — "être", "Zoë", "Grüße" all give runs of exactly one — so the
# mean run of consecutive non-ASCII letters sits at ~1.0. Mojibake from a
# non-Latin script turns whole words into unbroken runs, averaging five or
# more. Requiring both signals keeps heavily accented French or German,
# which can drift past the ratio threshold on its own, out of reach.
_MIN_MEAN_RUN = 2.5

# A candidate must produce text this plausible before it is trusted, and
# must also beat the runner-up by this margin. The margin matters as much
# as the threshold: Greek scores 0.75 under CP1253 against 0.55 under the
# wrong CP1251, so an absolute cut-off alone would either reject the right
# answer or wave through a near-miss.
_ACCEPT_THRESHOLD = 0.60
_ACCEPT_MARGIN = 0.15

# Script coherence alone cannot choose between code pages: Greek bytes
# decoded as CP1251 yield perfectly coherent — and perfectly wrong —
# Cyrillic. Letter frequency separates them, because only the correct code
# page concentrates the text on that language's common letters. Measured
# on a Greek sample: CP1253 puts 75% of letters in this set, CP1251 55%.
_FREQUENT_LETTERS = {
    "Greek": set("αεοιτνσρυπκημλω"),
    "Cyrillic": set("оеаинтсрвлкмдпу"),
    "Hebrew": set("אהוילמרשתבנכספ"),
    "Arabic": set("اللمنويربتهعدسف"),
}

_SCRIPT_RANGES = [
    ("Latin", 0x0041, 0x024F),
    ("Greek", 0x0370, 0x03FF),
    ("Cyrillic", 0x0400, 0x04FF),
    ("Hebrew", 0x0590, 0x05FF),
    ("Arabic", 0x0600, 0x06FF),
    ("Thai", 0x0E00, 0x0E7F),
    ("Hangul", 0xAC00, 0xD7AF),
    ("Kana", 0x3040, 0x30FF),
    ("Han", 0x4E00, 0x9FFF),
]


def _script_of(char: str) -> str:
    code = ord(char)
    for name, low, high in _SCRIPT_RANGES:
        if low <= code <= high:
            return name
    return "Other"


def _non_ascii_letter_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if ord(c) > 127) / len(letters)


def _mean_non_ascii_run(text: str) -> float:
    """Average length of unbroken runs of non-ASCII letters."""
    runs: List[int] = []
    current = 0
    for char in text:
        if char.isalpha() and ord(char) > 127:
            current += 1
        else:
            if current:
                runs.append(current)
            current = 0
    if current:
        runs.append(current)
    if not runs:
        return 0.0
    return sum(runs) / len(runs)


def _coherence(text: str) -> float:
    """How consistently the letters belong to a single non-Latin script."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0

    scripts = Counter(_script_of(c) for c in letters)
    dominant, count = scripts.most_common(1)[0]
    if dominant in ("Latin", "Other"):
        # Re-decoding is only justified when it reveals another script;
        # Latin in, Latin out means we have learned nothing.
        return 0.0

    share = count / len(letters)

    # Undefined mappings and control characters mean the wrong code page.
    junk = sum(1 for c in text if c == "�" or (ord(c) < 32 and c not in "\t\n\r"))
    share = max(0.0, share - junk / max(1, len(text)))

    frequent = _FREQUENT_LETTERS.get(dominant)
    if frequent:
        in_script = [c.lower() for c in letters if _script_of(c) == dominant]
        hit_rate = sum(1 for c in in_script if c in frequent) / max(1, len(in_script))
        return share * hit_rate

    # Scripts without a frequency table (CJK) are judged on coherence
    # alone; their character sets are too large for this to help.
    return share * 0.75


def _sample(texts: Iterable[str], limit: int = 20000) -> str:
    collected: List[str] = []
    total = 0
    for text in texts:
        if not text:
            continue
        collected.append(text)
        total += len(text)
        if total >= limit:
            break
    return " ".join(collected)


def detect(texts: Iterable[str]) -> Optional[str]:
    """Return the code page to re-decode with, or None to leave text alone."""
    sample = _sample(texts)
    if not sample:
        return None

    if _non_ascii_letter_ratio(sample) < _SUSPICION_THRESHOLD:
        return None
    if _mean_non_ascii_run(sample) < _MIN_MEAN_RUN:
        return None

    try:
        raw = sample.encode("latin-1")
    except UnicodeEncodeError:
        # Not a clean byte-per-character mapping, so the premise does not
        # hold and re-decoding would be guesswork.
        return None

    scored = []
    for codec in CANDIDATE_CODE_PAGES:
        try:
            decoded = raw.decode(codec)
        except (UnicodeDecodeError, LookupError):
            continue
        scored.append((_coherence(decoded), codec))

    if not scored:
        return None

    scored.sort(reverse=True)
    best_score, best_codec = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0

    if best_score < _ACCEPT_THRESHOLD:
        return None
    if best_score - runner_up < _ACCEPT_MARGIN:
        # Two code pages explain the bytes about equally well; picking
        # either would be a coin flip, so leave the text untouched.
        return None
    return best_codec


def repair(text: str, codec: str) -> str:
    """Re-decode one string, leaving it untouched if that is not possible."""
    if not text:
        return text
    try:
        return text.encode("latin-1").decode(codec)
    except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
        return text


def repair_document(document, codec: Optional[str] = "auto") -> Optional[str]:
    """Repair every text run in a document in place.

    `codec` may be "auto" to detect, None to disable, or an explicit codec
    name to force. Returns the code page applied, or None if the text was
    left as libmspub produced it.
    """
    if codec is None:
        return None

    spans = [
        span
        for page in document.pages
        for item in _iter_items(document, page)
        for paragraph in getattr(item, "story", _EMPTY).paragraphs
        for span in paragraph.spans
    ]
    if not spans:
        return None

    chosen = detect(span.text for span in spans) if codec == "auto" else codec
    if not chosen:
        return None

    # `repair` swallows a bad codec per span and returns the text unchanged,
    # so without this a typo in --codepage would leave every run untouched
    # while the warning below still claimed the text had been re-decoded.
    try:
        codecs.lookup(chosen)
    except LookupError:
        document.warnings.append(
            f"text left as-is: {chosen} is not a code page Python knows"
        )
        return None

    changed = 0
    for span in spans:
        repaired = repair(span.text, chosen)
        if repaired != span.text:
            span.text = repaired
            changed += 1

    if not changed:
        document.warnings.append(
            f"text left as-is: {chosen} changed nothing (the text was "
            f"probably decoded correctly already)"
        )
        return None

    document.warnings.append(
        f"text re-decoded as {chosen}: libmspub had applied the wrong code page"
    )
    return chosen


class _Empty:
    paragraphs: list = []


_EMPTY = _Empty()


def _iter_items(document, page):
    from . import model

    return [item for item in model._walk(page.items) if isinstance(item, model.TextFrame)]
