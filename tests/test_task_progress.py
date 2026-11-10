"""Progress from background work reaches the window on the GUI thread."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThread  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from slr_studio.ui.workers import Task  # noqa: E402


def test_progress_callbacks_run_on_the_gui_thread(monkeypatch):
    # The full screening run crashed inside Qt6Gui.dll because its progress
    # bar was updated from the worker thread.
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    seen = []

    def on_progress(done, total, message):
        seen.append((done, total, message, QThread.currentThread() is app.thread()))

    def work(*, on_progress):
        for index in range(1, 51):
            on_progress(index, 50, f"step {index}")
        return "done"

    task = Task(work, on_progress=on_progress)
    results = []
    task.succeeded.connect(results.append)
    task.start()
    deadline = time.time() + 10
    while (task.isRunning() or not results) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    assert results == ["done"]
    assert len(seen) == 50
    assert all(on_gui for *_, on_gui in seen)
    assert seen[-1][:3] == (50, 50, "step 50")
