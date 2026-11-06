"""Background work.

Importing 20,000 references or running deduplication over them takes long
enough that doing it on the GUI thread would freeze the window. Every
long-running call goes through :class:`Task`, which reports back with signals.
"""

from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QThread, Signal

__all__ = ["Task"]


class Task(QThread):
    """Run one callable off the GUI thread and report the outcome."""

    succeeded = Signal(object)
    failed = Signal(str)
    progressed = Signal(int, int, str)

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            # The traceback goes to the log; the reviewer sees the message.
            traceback.print_exc()
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
