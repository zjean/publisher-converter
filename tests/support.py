"""Helpers for building event streams by hand.

Splitting the pipeline at a JSON boundary means the whole document model
can be exercised without a .pub file and without Publisher. No sample in
the corpus contains a shape group, for instance, so hand-authored streams
are the only way to reach that code at all.
"""

from __future__ import annotations

import io
import json
from typing import Optional

from pubidml import model


def event(name: str, props: Optional[dict] = None, text: Optional[str] = None) -> str:
    payload = {"e": name}
    if props is not None:
        payload["p"] = props
    if text is not None:
        payload["t"] = text
    return json.dumps(payload)


def build(*lines: str) -> model.Document:
    """Replay hand-written event lines into a Document."""
    return model.build(io.StringIO("\n".join(lines) + "\n"))


def page(width: str = "8.5in", height: str = "11in") -> list:
    return [event("startPage", {"svg:width": width, "svg:height": height})]


def text_frame(
    *runs: str,
    x: str = "1in",
    y: str = "1in",
    width: str = "3in",
    height: str = "2in",
) -> list:
    """A single text frame carrying one paragraph of one span per run."""
    lines = [
        event(
            "startTextObject",
            {"svg:x": x, "svg:y": y, "svg:width": width, "svg:height": height},
        ),
        event("openParagraph", {}),
        event("openSpan", {"style:font-name": "Arial", "fo:font-size": "12pt"}),
    ]
    for run in runs:
        lines.append(event("insertText", text=run))
    lines += [event("closeSpan"), event("closeParagraph"), event("endTextObject")]
    return lines


def document(*body: str) -> model.Document:
    """Wrap body events in a complete, well-terminated stream."""
    return build(*(page() + list(body) + [event("endPage"), event("endDocument")]))


def paged_document(*bodies: list) -> model.Document:
    """One page per body, so cross-page behaviour can be exercised."""
    lines: list = []
    for body in bodies:
        lines += page() + list(body) + [event("endPage")]
    return build(*(lines + [event("endDocument")]))


def only_span(doc: model.Document) -> model.Span:
    frame = next(
        item
        for p in doc.pages
        for item in model._walk(p.items)
        if isinstance(item, model.TextFrame)
    )
    return frame.story.paragraphs[0].spans[0]
