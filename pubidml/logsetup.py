"""Disk logging, so a failed batch can be diagnosed after the fact.

Console output stays a short human summary; the log file gets everything
needed to work out what went wrong on a machine you cannot inspect:
environment, the resolved parser path, per-file timings, the parser's own
stderr, and full tracebacks for anything unexpected.

Logs go to a per-user location rather than the working directory, so
running the tool against a read-only or network folder still produces a
log:

    Windows   %LOCALAPPDATA%\\pub2idml\\logs
    macOS     ~/Library/Logs/pub2idml
    other     ${XDG_STATE_HOME:-~/.local/state}/pub2idml/logs
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

LOGGER_NAME = "pubidml"

# Keep the log directory from growing without bound on a machine that
# converts collections regularly.
MAX_KEPT_LOGS = 30


def default_log_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(base) / "pub2idml" / "logs"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "pub2idml"
    state = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(state) / "pub2idml" / "logs"


def _prune(directory: Path) -> None:
    try:
        logs = sorted(directory.glob("pub2idml-*.log"), key=lambda p: p.stat().st_mtime)
        for old in logs[:-MAX_KEPT_LOGS]:
            old.unlink(missing_ok=True)
    except OSError:
        # Pruning is housekeeping; never let it break a conversion run.
        pass


def configure(log_file: Optional[Path] = None, verbose: bool = False) -> Optional[Path]:
    """Attach a file handler and return the path actually written to.

    Returns None when no log could be opened, which is not fatal — the
    tool still runs and still prints to the console.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    # Warnings and errors also reach the terminal; routine progress does
    # not, because the CLI already prints a readable per-file summary.
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)

    if log_file is None:
        directory = default_log_dir()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        log_file = directory / f"pub2idml-{stamp}.log"
    log_file = Path(log_file)

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_file, encoding="utf-8")
    except OSError as exc:
        logger.warning("could not open log file %s: %s", log_file, exc)
        return None

    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
        )
    )
    logger.addHandler(handler)
    _prune(log_file.parent)
    return log_file


def log_environment(pubdump: Path) -> None:
    """Record everything needed to reproduce a run on another machine."""
    from . import __version__

    logger = logging.getLogger(LOGGER_NAME)
    logger.info("pub2idml %s starting", __version__)
    logger.info("command line: %s", " ".join(sys.argv))
    logger.info(
        "python %s on %s (%s)",
        platform.python_version(),
        platform.platform(),
        platform.machine(),
    )
    logger.info("frozen bundle: %s", bool(getattr(sys, "frozen", False)))
    if getattr(sys, "frozen", False):
        logger.info("bundle dir: %s", getattr(sys, "_MEIPASS", "?"))
    logger.info("parser: %s (exists=%s)", pubdump, pubdump.exists())
    if pubdump.exists():
        try:
            logger.info("parser size: %d bytes", pubdump.stat().st_size)
        except OSError:
            pass
    logger.info("working directory: %s", Path.cwd())


def install_excepthook() -> None:
    """Send otherwise-silent crashes to the log with a full traceback."""
    logger = logging.getLogger(LOGGER_NAME)
    previous = sys.excepthook

    def hook(exc_type, exc, tb):
        logger.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        previous(exc_type, exc, tb)

    sys.excepthook = hook


def get_logger(suffix: str = "") -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{suffix}" if suffix else LOGGER_NAME)
