"""Stage 6: finding and attaching the full texts."""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...fulltext.retrieval import RetrievalService
from ...fulltext.unpaywall import UnpaywallClient, UnpaywallError
from ..widgets import DropArea, SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["RetrievePage"]

_STATUS_LABELS = {
    "pending": "not yet",
    "retrieved": "retrieved",
    "not_retrieved": "not retrieved",
    "abstract_only": "abstract only",
}


class RetrievePage(StagePage):
    title = "Retrieve"
    subtitle = "Find the full texts, and bring them into the project."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._stop = False
        self._rows: list[dict] = []
        self._unmatched: list[dict] = []

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self._layout.addWidget(self.status_label)

        notice = QLabel(
            "This app finds links; it never downloads from a paywalled site, "
            "uses your institutional login, solves a CAPTCHA or scrapes a "
            "publisher. Follow a link, decide whether you may read it, and "
            "save the file yourself."
        )
        notice.setWordWrap(True)
        notice.setStyleSheet("color: #5b6677;")
        self._layout.addWidget(notice)

        controls = QHBoxLayout()
        self.lookup_button = QPushButton("Find open-access copies")
        self.lookup_button.setToolTip(
            "Asks Unpaywall, using the contact email on the Project page, "
            "where a free copy of each report is published."
        )
        self.lookup_button.clicked.connect(self._lookup)
        controls.addWidget(self.lookup_button)

        self.open_button = QPushButton("Open the selected link")
        self.open_button.clicked.connect(self._open_link)
        controls.addWidget(self.open_button)

        self.not_retrieved_button = QPushButton("Mark as not retrieved…")
        self.not_retrieved_button.clicked.connect(
            lambda: self._set_status("not_retrieved")
        )
        controls.addWidget(self.not_retrieved_button)

        self.abstract_button = QPushButton("Abstract only…")
        self.abstract_button.clicked.connect(lambda: self._set_status("abstract_only"))
        controls.addWidget(self.abstract_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        self._layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

        self.drop = DropArea(
            "Drop the PDFs you have saved here",
            suffixes=(".pdf",),
            file_filter="PDF files (*.pdf);;All files (*)",
        )
        self.drop.files_dropped.connect(self._add_pdfs)
        self._layout.addWidget(self.drop)

        tabs = QTabWidget()
        tabs.addTab(self._build_list_tab(), "Reports sought")
        tabs.addTab(self._build_unmatched_tab(), "Files to match by hand")
        self._tabs = tabs
        self._layout.addWidget(tabs, 1)

    # -- tabs --------------------------------------------------------------

    def _build_list_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.record_list = QListWidget()
        self.record_list.setMaximumWidth(330)
        self.record_list.setWordWrap(True)
        self.record_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.record_list.currentRowChanged.connect(self._show_record)
        splitter.addWidget(self.record_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)
        self.record_view = QTextBrowser()
        self.record_view.setOpenExternalLinks(True)
        right_layout.addWidget(self.record_view, 1)

        right_layout.addWidget(section_label("Where a free copy is published"))
        self.links_table = SimpleTable(
            ["Link", "Host", "Version", "Licence", "Best"], stretch_columns=[0]
        )
        self.links_table.fit_to_rows(6)
        right_layout.addWidget(self.links_table)

        splitter.addWidget(right)
        splitter.setSizes([330, 730])
        layout.addWidget(splitter, 1)
        return page

    def _build_unmatched_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        hint = QLabel(
            "These files could not be tied to a record with enough confidence. "
            "A PDF filed against the wrong study is worse than one filed "
            "against none, so nothing here is attached until you say so."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5b6677;")
        layout.addWidget(hint)

        self.unmatched_list = QListWidget()
        self.unmatched_list.setWordWrap(True)
        self.unmatched_list.currentRowChanged.connect(self._show_unmatched)
        layout.addWidget(self.unmatched_list, 1)

        self.unmatched_detail = QLabel()
        self.unmatched_detail.setWordWrap(True)
        self.unmatched_detail.setStyleSheet("color: #5b6677;")
        layout.addWidget(self.unmatched_detail)

        row = QHBoxLayout()
        row.addWidget(QLabel("Belongs to"))
        self.match_combo = QComboBox()
        self.match_combo.setMinimumWidth(420)
        row.addWidget(self.match_combo, 1)
        self.match_button = QPushButton("Attach to this record")
        self.match_button.clicked.connect(self._match_selected)
        row.addWidget(self.match_button)
        self.discard_button = QPushButton("Not part of this review")
        self.discard_button.clicked.connect(self._discard_selected)
        row.addWidget(self.discard_button)
        layout.addLayout(row)
        return page

    # -- service -----------------------------------------------------------

    def _service(self) -> RetrievalService:
        return RetrievalService(self.project)

    def _busy(self, busy: bool) -> None:
        for button in (self.lookup_button, self.not_retrieved_button,
                       self.abstract_button):
            button.setEnabled(not busy)
        self.drop.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.progress.setVisible(busy)
        if not busy:
            self._stop = False

    def _request_stop(self) -> None:
        self._stop = True

    def _progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%p%)")

    def _start(self, fn, *args, **kwargs) -> None:
        if self._task is not None and self._task.isRunning():
            return
        self._busy(True)
        task = Task(fn, *args, **kwargs)
        task.succeeded.connect(self._done)
        task.failed.connect(self._failed)
        task.finished.connect(lambda: self._busy(False))
        self._task = task
        task.start()

    def _done(self, result) -> None:
        self.progress.hide()
        if isinstance(result, dict):
            self.status_message.emit(
                f"Looked up {result.get('n_looked_up', 0)} DOI(s); "
                f"{result.get('n_open_access', 0)} have a free copy; "
                f"{result.get('n_errors', 0)} could not be checked."
            )
        elif isinstance(result, list):
            matched = sum(1 for outcome in result if outcome.matched)
            self.status_message.emit(
                f"Added {len(result)} file(s); {matched} matched automatically."
            )
            unmatched = [o for o in result if not o.matched]
            if unmatched:
                QMessageBox.information(
                    self, "Some files need matching by hand",
                    "\n\n".join(o.message for o in unmatched[:8]),
                )
                self._tabs.setCurrentIndex(1)
        self.refresh()
        self.data_changed.emit()

    def _failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Retrieval stopped", message)

    # -- actions -----------------------------------------------------------

    def _lookup(self) -> None:
        if self.project is None:
            return
        email = (self.project.info().get("contact_email") or "").strip()
        if not email:
            # Asked for here, where it is needed, rather than leaving the
            # button greyed out with the reason in small print elsewhere.
            email, ok = QInputDialog.getText(
                self, "Email for Unpaywall",
                "Unpaywall, the free service that finds legal open-access "
                "copies, asks everyone who uses it for a contact email.\n\n"
                "Only the records' DOIs and this email are sent to Unpaywall; "
                "no titles, abstracts or decisions. The email is saved with "
                "this review.\n\nYour email:",
            )
            email = (email or "").strip()
            if not ok or not email:
                return
            if "@" not in email or "." not in email.split("@")[-1]:
                QMessageBox.warning(self, "Not an email address",
                                    f"{email!r} does not look like an email address.")
                return
            self.project.update_info(contact_email=email)
        try:
            client = UnpaywallClient(email)
        except UnpaywallError as exc:
            QMessageBox.warning(self, "An email address is needed", str(exc))
            return
        service = self._service()
        self._start(
            service.find_open_access,
            client=client,
            on_progress=self._progress,
            should_stop=lambda: self._stop,
        )

    def _add_pdfs(self, paths: list[str]) -> None:
        if self.project is None or not paths:
            return
        service = self._service()
        self._start(service.attach_many, list(paths), on_progress=self._progress)

    def _selected_record(self) -> dict | None:
        row = self.record_list.currentRow()
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def _open_link(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        links = [loc["url"] for loc in record["oa_locations"]] or [record["doi_link"]]
        links = [link for link in links if link]
        if not links:
            QMessageBox.information(
                self, "No link", "This record has no DOI and no known free copy."
            )
            return
        QDesktopServices.openUrl(QUrl(links[0]))

    def _set_status(self, status: str) -> None:
        record = self._selected_record()
        if record is None or self.project is None:
            return
        prompt = (
            "Why could the full text not be retrieved?"
            if status == "not_retrieved"
            else "Why is only the abstract available?"
        )
        reason, ok = QInputDialog.getText(
            self, "Reason required",
            f"{prompt}\n\nPRISMA asks for the reports not retrieved and why.",
        )
        if not ok or not reason.strip():
            return
        self._service().set_status(record["id"], status, reason=reason.strip())
        self.status_message.emit(
            f"Record {record['id']} marked as {_STATUS_LABELS[status]}."
        )
        self.refresh()
        self.data_changed.emit()

    def _match_selected(self) -> None:
        row = self.unmatched_list.currentRow()
        if self.project is None or row < 0 or row >= len(self._unmatched):
            return
        record_id = self.match_combo.currentData()
        if record_id is None:
            QMessageBox.information(
                self, "Choose a record", "Pick the record this file belongs to."
            )
            return
        outcome = self._service().resolve_unmatched(
            self._unmatched[row]["id"], int(record_id)
        )
        self.status_message.emit(outcome.message)
        self.refresh()
        self.data_changed.emit()

    def _discard_selected(self) -> None:
        row = self.unmatched_list.currentRow()
        if self.project is None or row < 0 or row >= len(self._unmatched):
            return
        reason, ok = QInputDialog.getText(
            self, "Set this file aside",
            "Why is this file not part of the review?",
        )
        if not ok:
            return
        self._service().discard_unmatched(self._unmatched[row]["id"], reason.strip())
        self.refresh()
        self.data_changed.emit()

    # -- display -----------------------------------------------------------

    def _show_record(self, row: int) -> None:
        if row < 0 or row >= len(self._rows):
            self.record_view.clear()
            self.links_table.fill([])
            return
        record = self._rows[row]

        def esc(value: object) -> str:
            return html.escape("" if value is None else str(value))

        status = _STATUS_LABELS.get(record.get("status") or "pending", "not yet")
        meta = [
            ("Year", record.get("year")),
            ("Publication", record.get("journal")),
            ("Authors", "; ".join(str(a) for a in record.get("authors") or [])),
            ("Screened as", record.get("screening_decision")),
            ("Full text", status + (f" — {record['reason']}" if record.get("reason") else "")),
        ]
        if record.get("filename"):
            meta.append(("File", f"{record['filename']} ({record['n_pages']} pages)"))
        rows = "".join(
            f"<tr><td style='color:#5b6677;padding-right:10px;white-space:nowrap'>"
            f"{esc(k)}</td><td>{esc(v)}</td></tr>"
            for k, v in meta
        )
        link = record.get("doi_link")
        link_html = (
            f"<p><a href='{esc(link)}'>{esc(link)}</a></p>" if link else
            "<p style='color:#5b6677'>This record has no DOI.</p>"
        )
        self.record_view.setHtml(
            f"<h3 style='margin:0 0 8px 0'>{esc(record.get('title'))}</h3>"
            f"<table style='font-size:12px'>{rows}</table>{link_html}"
        )
        self.links_table.fill(
            [
                [
                    loc["url"], loc["host_type"] or "—", loc["version"] or "—",
                    loc["license"] or "—", "yes" if loc["is_best"] else "",
                ]
                for loc in record["oa_locations"]
            ]
        )

    def _show_unmatched(self, row: int) -> None:
        if row < 0 or row >= len(self._unmatched):
            self.unmatched_detail.clear()
            return
        item = self._unmatched[row]
        parts = [f"{item['filename']}"]
        if item["detected_doi"]:
            parts.append(f"DOI read from the file: {item['detected_doi']}")
        if item["detected_title"]:
            parts.append(f"Title read from the file: {item['detected_title']}")
        if item["best_guess_record_id"]:
            parts.append(
                f"Closest record: {item['best_guess_record_id']} "
                f"({item['best_guess_score']:.0%} title similarity) — "
                f"{item['guess_title'] or ''}"
            )
        else:
            parts.append("No record came close enough to suggest.")
        self.unmatched_detail.setText("\n".join(parts))
        if item["best_guess_record_id"]:
            index = self.match_combo.findData(item["best_guess_record_id"])
            if index >= 0:
                self.match_combo.setCurrentIndex(index)

    def refresh(self) -> None:
        if self.project is None:
            self._rows = []
            self.record_list.clear()
            self.status_label.setText("")
            return
        service = self._service()
        summary = service.summary()
        email = summary.get("contact_email") or ""
        # Always available: without an email, pressing it asks for one.
        self.lookup_button.setEnabled(True)
        self.status_label.setText(
            f"{summary.get('n_sought', 0)} report(s) sought · "
            f"{summary.get('n_retrieved', 0)} retrieved · "
            f"{summary.get('n_not_retrieved', 0)} not retrieved · "
            f"{summary.get('n_abstract_only', 0)} abstract only · "
            f"{summary.get('n_pending', 0)} still to find · "
            f"{summary.get('n_unmatched_pdfs', 0)} file(s) to match"
            + ("" if email else "  —  Find open-access copies will ask for "
                               "the contact email Unpaywall requires")
        )

        self._rows = service.retrieval_list()
        self.record_list.clear()
        for record in self._rows:
            status = _STATUS_LABELS.get(record.get("status") or "pending", "")
            self.record_list.addItem(
                QListWidgetItem(
                    f"#{record['id']}  [{status}]\n{(record.get('title') or '')[:70]}"
                )
            )
        if self._rows:
            self.record_list.setCurrentRow(0)
        else:
            self._show_record(-1)

        self.match_combo.clear()
        for record in self._rows:
            self.match_combo.addItem(
                f"#{record['id']}  {(record.get('title') or '')[:70]}", record["id"]
            )

        self._unmatched = service.unmatched()
        self.unmatched_list.clear()
        for item in self._unmatched:
            self.unmatched_list.addItem(
                QListWidgetItem(f"{item['filename']}\n{item['detected_title'][:70]}")
            )
        has_unmatched = bool(self._unmatched)
        self.match_button.setEnabled(has_unmatched)
        self.discard_button.setEnabled(has_unmatched)
        if has_unmatched:
            self.unmatched_list.setCurrentRow(0)
        else:
            self._show_unmatched(-1)
