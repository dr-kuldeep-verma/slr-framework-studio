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
        # A progress callback usually updates a progress bar. Called from
        # this worker thread it would touch a widget off the GUI thread,
        # which Qt does not allow: the full screening run crashed inside
        # Qt6Gui.dll that way. The work is given the signal's emit instead,
        # and the signal delivers each update on the GUI thread, where the
        # task object was created.
        callback = kwargs.get("on_progress")
        if callable(callback):
            self.progressed.connect(callback)
            kwargs["on_progress"] = self._report
        self._kwargs = kwargs

    def _report(self, done: int, total: int, message: str = "") -> None:
        self.progressed.emit(int(done), int(total), str(message))

    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            # The traceback goes to the log; the reviewer sees the message.
            traceback.print_exc()
            from ..errorlog import log_exception

            log_exception(exc, where=f"background task {getattr(self._fn, '__qualname__', '')}")
            self.failed.emit(str(exc))
        else:
            self.succeeded.emit(result)
