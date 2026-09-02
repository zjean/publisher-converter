"""What the wizard decides, separated from what it draws.

Keeping these as functions over plain values rather than methods on a
frame is what makes them testable on a machine with no display -- which
includes this project's own development machine, where Python has no
_tkinter at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .. import batch
from . import strings

CHOOSE, DESTINATION, CONVERTING, DONE = 0, 1, 2, 3


@dataclass
class Selection:
    """The .pub files to convert, and the root their tree hangs from."""
    paths: List[Path]
    root: Path
    is_single_file: bool = False

    @property
    def folder_count(self) -> int:
        return len({p.parent for p in self.paths})


def scan(paths: List[Path]) -> Optional[Selection]:
    """Work out what was chosen. None when it holds no .pub files.

    Delegates the folder search to batch.find_sources rather than walking
    globs here, so the ~$-lock-file rule has one owner instead of two that
    can drift apart. find_sources answers a *directory* question --
    everything under a chosen folder that looks like a real .pub -- so it
    is only called where a directory is actually in hand; a path the user
    named as a single file is trusted as-is, exactly as cli's own caller
    trusts it.
    """
    paths = [Path(p) for p in paths if Path(p).exists()]
    if not paths:
        return None

    if len(paths) == 1 and paths[0].is_dir():
        found = batch.find_sources(paths[0])
        return Selection(found, paths[0]) if found else None

    if len(paths) == 1 and paths[0].is_file():
        return Selection([paths[0]], paths[0], is_single_file=True)

    files: List[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(batch.find_sources(path))
        elif path.suffix.lower() == ".pub" and not path.name.startswith("~$"):
            files.append(path)
    if not files:
        return None

    root = Path(os.path.commonpath([str(p.parent) for p in files]))
    return Selection(sorted(files), root)


def default_destination(selection: Selection) -> Path:
    """A sibling of what was chosen, named after it.

    Beside rather than inside, because inside is the one arrangement that
    makes a later run convert its own output.
    """
    if selection.is_single_file:
        return selection.root.parent / f"{selection.root.stem} omgezet"
    return selection.root.parent / f"{selection.root.name} omgezet"


def destination_problem(destination: Path, selection: Selection) -> Optional[str]:
    """A Dutch explanation of why this destination will not do, or None.

    Only a folder selection is checked against its own tree. A single
    file's default destination -- <parent>/<stem> omgezet -- necessarily
    sits inside that file's parent, so refusing "inside" there would
    refuse the default itself; there is no source tree to walk into a
    second time when there was only ever one file to begin with.
    """
    if selection.is_single_file:
        return None
    try:
        destination.resolve().relative_to(selection.root.resolve())
    except ValueError:
        return None
    return strings.STEP2_INSIDE_SOURCE
