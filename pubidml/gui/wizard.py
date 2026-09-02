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


class MixedRootsError(Exception):
    """The chosen files share no folder to mirror the tree from.

    Only reachable from a multi-path selection: two Windows drives, or a
    UNC path beside a drive letter. commonpath raises on Windows and
    returns the filesystem root on POSIX -- and the second is the worse
    outcome, because it silently mirrors the whole absolute path into the
    destination instead of failing.
    """


@dataclass
class Selection:
    """The .pub files to convert, and the root their tree hangs from."""
    paths: List[Path]
    root: Path
    is_single_file: bool = False

    @property
    def folder_count(self) -> int:
        """How many distinct parent folders selection.paths spans."""
        return len({p.parent for p in self.paths})


def _shares_one_root(paths: List[Path]) -> bool:
    """Whether every path hangs from the same filesystem anchor.

    On POSIX every absolute path's anchor is "/", so this only ever
    refuses on Windows -- two drive letters, or a UNC share beside a
    drive letter -- which is exactly the case os.path.commonpath cannot
    turn into one destination tree. The comparison is case-folded because
    'C:\\' and 'c:\\' name the same drive.

    This is an anchor check, not an absoluteness check: relative paths
    all carry the empty anchor and so trivially "share one root" no
    matter where they actually point, and os.path.commonpath on unrelated
    relative paths returns "" rather than raising. Neither case is
    reachable through a file-choosing dialog, which only ever hands back
    absolute paths -- but scan() does not itself force that, so a caller
    that skips the dialog can see it.
    """
    return len({p.anchor.lower() for p in paths}) <= 1


def scan(paths: List[Path]) -> Optional[Selection]:
    """Work out what was chosen.

    Two different things can go wrong, and they mean different things to
    a caller: this returns None when the selection holds no .pub files at
    all, and it raises MixedRootsError when it holds files that cannot be
    mirrored under one destination tree (they live on different drives).
    Task 6 must tell these apart rather than treating both as "nothing to
    convert".

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

    # A folder and a file inside it can both be dropped onto the icon at
    # once -- an ordinary mistake, not a malformed one -- and without this
    # the same file is found twice: once via find_sources(folder), once
    # via the file arm above. Left alone that inflates the count a user
    # reads and converts the file twice to the same destination.
    files = list(dict.fromkeys(files))

    if not _shares_one_root(files):
        raise MixedRootsError()

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
