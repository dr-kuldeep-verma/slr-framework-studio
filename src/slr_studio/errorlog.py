"""A plain-text log of unexpected errors, for reporting a problem.

The packaged Windows program has no console, so a traceback printed there
is lost, and "it said Cannot operate on a closed database" is all anyone has
to go on. Every unexpected error is appended here with its full traceback.
Nothing in it is sent anywhere; the reviewer can attach it to a bug report.

The log holds error messages and code locations, not records, prompts or
keys.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["log_path", "log_exception", "install_excepthook"]

#: Older entries are dropped once the file passes this size.
MAX_BYTES = 512_000


def log_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "SLR Framework Studio" / "error-log.txt"


def log_exception(exc: BaseException, *, where: str = "") -> Path | None:
    """Append one error with its traceback. Never raises."""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            tail = path.read_text(encoding="utf-8", errors="replace")[-MAX_BYTES // 2 :]
            path.write_text(tail, encoding="utf-8")
        from . import __version__

        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n=== {stamp}  version {__version__}  {where}\n{text}")
        return path
    except Exception:
        return None


def install_excepthook() -> None:
    """Log anything that would otherwise vanish, then behave as before."""
    previous = sys.excepthook

    def hook(kind, value, tb):
        if value is not None and value.__traceback__ is None:
            value = value.with_traceback(tb)
        log_exception(value, where="unhandled")
        previous(kind, value, tb)

    sys.excepthook = hook
