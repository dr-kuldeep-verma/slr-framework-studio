"""Stage 8 of the build: handing the review over.

The page lists everything Section 12 names, says for each whether this review
can produce it yet, and writes the lot in one action. What it will not do is
write an empty file for a stage that has not been reached: the row says why
instead, because an empty graded inventory in a folder reads as a finding.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
)

from ...exports import SECTION_12, export_everything
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["ExportPage"]


class ExportPage(StagePage):
    title = "Export"
    subtitle = (
        "Everything the review produced, with every number read from the "
        "database."
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self._layout.addWidget(self.summary)

        row = QHBoxLayout()
        self.export_button = QPushButton("Export everything")
        self.export_button.clicked.connect(self._export)
        self.open_button = QPushButton("Open the exports folder")
        self.open_button.clicked.connect(self._open_folder)
        row.addWidget(self.export_button)
        row.addWidget(self.open_button)
        row.addStretch(1)
        self._layout.addLayout(row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = SimpleTable(
            ["Output", "File", "Ready"], stretch_columns=[0]
        )
        splitter.addWidget(self.table)
        self.detail = QTextBrowser()
        splitter.addWidget(self.detail)
        splitter.setSizes([360, 260])
        self._layout.addWidget(splitter, 1)

        self._layout.addWidget(section_label("The audit chain"))
        self.audit = QTextBrowser()
        self.audit.setMaximumHeight(110)
        self._layout.addWidget(self.audit)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

    def requires_project(self) -> bool:
        return True

    # -- actions ------------------------------------------------------------

    def _export(self) -> None:
        if self.project is None:
            return
        if self._task is not None and self._task.isRunning():
            return
        self.export_button.setEnabled(False)
        self.progress.setVisible(True)
        task = Task(export_everything, self.project)
        task.succeeded.connect(self._done)
        task.failed.connect(self._failed)
        task.finished.connect(
            lambda: (
                self.export_button.setEnabled(True),
                self.progress.hide(),
            )
        )
        self._task = task
        task.start()

    def _done(self, outcome) -> None:
        self.progress.hide()
        self.status_message.emit(
            f"{len(outcome.written)} file(s) written to {outcome.folder}"
        )
        parts = [
            f"<p><b>{len(outcome.written)} file(s) written</b> to "
            f"{html.escape(str(outcome.folder))}.</p><ul>"
        ]
        for path in sorted(outcome.written, key=lambda p: p.name):
            parts.append(f"<li>{html.escape(path.name)}</li>")
        parts.append("</ul>")
        if outcome.skipped:
            parts.append("<p><b>Not written</b></p><ul>")
            for reason in outcome.skipped.values():
                parts.append(f"<li>{html.escape(reason)}</li>")
            parts.append("</ul>")
        if outcome.failed:
            parts.append("<p style='color:#8a1f1f'><b>Failed</b></p><ul>")
            for kind, reason in outcome.failed.items():
                parts.append(
                    f"<li>{html.escape(kind)}: {html.escape(reason)}</li>"
                )
            parts.append("</ul>")
        self.detail.setHtml("".join(parts))
        self.refresh()
        self.data_changed.emit()

    def _failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Export stopped", message)

    def _open_folder(self) -> None:
        if self.project is None:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        folder = self.project.exports_dir
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # -- refresh ---------------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            return
        rows = []
        n_ready = 0
        for item in SECTION_12:
            try:
                available = self.project.db.scalar(item.needs) or 0
            except Exception:
                available = 0
            n_ready += bool(available)
            rows.append(
                [
                    item.description,
                    item.filename,
                    "yes" if available else "not yet",
                ]
            )
        self.table.fill(rows)
        self.summary.setText(
            f"{n_ready} of {len(SECTION_12)} output(s) can be written from "
            "this review as it stands. Anything not ready is left out rather "
            "than written empty, because an empty file in the folder reads "
            "as a finding."
        )

        problems = self.project.audit.verify()
        n_entries = self.project.db.scalar("SELECT COUNT(*) FROM audit_log")
        if problems:
            self.audit.setHtml(
                f"<p style='color:#8a1f1f'><b>The audit chain is broken at "
                f"{len(problems)} point(s).</b> Every export carries this "
                "result; explain it before relying on the review.</p>"
            )
        else:
            self.audit.setHtml(
                f"<p>{n_entries} entry(ies). The chain is intact: every "
                "entry's hash matches its content and the entry before it. "
                "The exported audit log carries this result.</p>"
            )
