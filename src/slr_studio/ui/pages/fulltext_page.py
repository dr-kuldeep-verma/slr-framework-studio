"""Stage 7: assessing retrieved reports for eligibility."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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

from ...ai.client import AIClient
from ...ai.settings import load_settings, provider_from_settings
from ...fulltext import EligibilityEngine, EligibilitySettings
from ...fulltext.engine import EligibilityError
from ..dialogs import confirm_cost
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["FullTextPage"]

_REASON_LABELS = {
    "quote_failed": "Quotation not found",
    "unclear": "Assessor unsure",
    "conflict": "Needs a second look",
    "confirm_inclusion": "Confirm inclusion",
}


class FullTextPage(StagePage):
    title = "Full-text"
    subtitle = "Each report judged against the ordered criteria, with the page it rests on."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._stop = False
        self._queue: list[dict] = []

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self._layout.addWidget(self.status_label)

        self.blocker_label = QLabel()
        self.blocker_label.setWordWrap(True)
        self.blocker_label.setObjectName("blockerLabel")
        self.blocker_label.hide()
        self._layout.addWidget(self.blocker_label)

        controls = QHBoxLayout()
        self.run_button = QPushButton("Assess retrieved reports")
        self.run_button.clicked.connect(self._run)
        controls.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        self._layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

        tabs = QTabWidget()
        tabs.addTab(self._build_queue_tab(), "Needs your decision")
        tabs.addTab(self._build_included_tab(), "Included studies")
        self._layout.addWidget(tabs, 1)

    def _build_queue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            "An exclusion here is final and is reported in the PRISMA diagram, "
            "so anything borderline comes to you. The page the assessment "
            "quoted is shown underneath, as the app read it."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5b6677;")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.queue_list = QListWidget()
        self.queue_list.setMaximumWidth(300)
        self.queue_list.setWordWrap(True)
        self.queue_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.queue_list.currentRowChanged.connect(self._show_item)
        splitter.addWidget(self.queue_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)

        self.record_view = QTextBrowser()
        self.record_view.setMaximumHeight(170)
        right_layout.addWidget(self.record_view)

        right_layout.addWidget(section_label("The assessment"))
        self.assessment_table = SimpleTable(
            ["Assessor", "Decision", "Code", "Reason", "Quotation", "Page", "Checked"],
            stretch_columns=[3, 4],
        )
        self.assessment_table.fit_to_rows(4)
        right_layout.addWidget(self.assessment_table)

        self.page_label = section_label("The cited page")
        right_layout.addWidget(self.page_label)
        self.page_view = QTextBrowser()
        right_layout.addWidget(self.page_view, 1)

        decision_row = QHBoxLayout()
        decision_row.addWidget(QLabel("Exclusion code"))
        self.code_combo = QComboBox()
        self.code_combo.setMinimumWidth(190)
        decision_row.addWidget(self.code_combo)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Reason (recorded with your decision)")
        decision_row.addWidget(self.note_edit, 1)
        right_layout.addLayout(decision_row)

        buttons = QHBoxLayout()
        self.include_button = QPushButton("Include in the review")
        self.include_button.clicked.connect(lambda: self._resolve("include"))
        buttons.addWidget(self.include_button)
        self.exclude_button = QPushButton("Exclude")
        self.exclude_button.clicked.connect(lambda: self._resolve("exclude"))
        buttons.addWidget(self.exclude_button)
        self.unclear_button = QPushButton("Still unclear")
        self.unclear_button.clicked.connect(lambda: self._resolve("unclear"))
        buttons.addWidget(self.unclear_button)
        right_layout.addLayout(buttons)

        splitter.addWidget(right)
        splitter.setSizes([300, 760])
        layout.addWidget(splitter, 1)
        return page

    def _build_included_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(section_label("The studies the synthesis will be built from"))
        self.included_table = SimpleTable(["Record", "Year", "Title", "Journal", "DOI"],
                                          stretch_columns=[2])
        layout.addWidget(self.included_table, 1)

        layout.addWidget(section_label("Excluded at full text, with reasons"))
        self.exclusion_table = SimpleTable(["Code", "Criterion", "Reports"])
        self.exclusion_table.fit_to_rows(8)
        layout.addWidget(self.exclusion_table)

        layout.addWidget(section_label("Runs"))
        self.runs_table = SimpleTable(
            ["Status", "Reports", "Model", "Calls", "Cost (USD)", "Started"]
        )
        self.runs_table.fit_to_rows(5)
        layout.addWidget(self.runs_table)
        return page

    # -- engine ------------------------------------------------------------

    def _engine(self) -> EligibilityEngine:
        settings = load_settings(self.project)
        client = AIClient(
            self.project,
            provider_from_settings(settings),
            stage="fulltext",
            use_cache=settings.use_cache,
        )
        return EligibilityEngine(
            self.project, client, EligibilitySettings(budget_usd=settings.budget_usd)
        )

    def _busy(self, busy: bool) -> None:
        self.run_button.setEnabled(not busy)
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

    def _run(self) -> None:
        if self.project is None:
            return
        try:
            engine = self._engine()
            estimate = engine.estimate()
        except EligibilityError as exc:
            QMessageBox.warning(self, "Cannot assess yet", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Cannot assess yet", str(exc))
            return
        if estimate.n_calls == 0 and estimate.n_cached == 0:
            QMessageBox.information(
                self, "Nothing to assess",
                "Every retrieved report already has an eligibility decision. "
                "Add more PDFs on the Retrieve page first.",
            )
            return
        if not confirm_cost(estimate, "Assess retrieved reports", self):
            return
        if self._task is not None and self._task.isRunning():
            return
        self._busy(True)
        task = Task(
            engine.run, on_progress=self._progress, should_stop=lambda: self._stop
        )
        task.succeeded.connect(self._done)
        task.failed.connect(self._failed)
        task.finished.connect(lambda: self._busy(False))
        self._task = task
        task.start()

    def _done(self, outcome) -> None:
        self.progress.hide()
        self.status_message.emit(
            f"Assessed {outcome.n_done} report(s); "
            f"{outcome.n_quotes_on_cited_page} quotation(s) verified on the "
            f"page cited; {outcome.n_queued} waiting for you."
        )
        for message in outcome.messages:
            QMessageBox.warning(self, "Full-text assessment", message)
        self.refresh()
        self.data_changed.emit()

    def _failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Assessment stopped", message)

    # -- the queue ---------------------------------------------------------

    def _resolve(self, decision: str) -> None:
        row = self.queue_list.currentRow()
        if self.project is None or row < 0 or row >= len(self._queue):
            return
        item = self._queue[row]
        try:
            self._engine().resolve(
                item["record_id"], decision,
                exclusion_code=str(self.code_combo.currentData() or ""),
                reason=self.note_edit.text().strip(),
            )
        except EligibilityError as exc:
            QMessageBox.warning(self, "Cannot record that", str(exc))
            return
        self.note_edit.clear()
        self.status_message.emit(f"Report {item['record_id']} recorded as {decision}.")
        self.refresh()
        self.data_changed.emit()

    def _show_item(self, row: int) -> None:
        if row < 0 or row >= len(self._queue):
            self.record_view.clear()
            self.assessment_table.fill([])
            self.page_view.clear()
            return
        item = self._queue[row]

        def esc(value: object) -> str:
            return html.escape("" if value is None else str(value))

        meta = [
            ("Year", item.get("year")),
            ("Publication", item.get("journal")),
            ("Authors", "; ".join(str(a) for a in item.get("authors") or [])),
            ("File", f"{item.get('filename') or '—'} ({item.get('n_pages') or 0} pages)"),
            ("Flagged because", item.get("review_reason") or item.get("detail")),
        ]
        rows = "".join(
            f"<tr><td style='color:#5b6677;padding-right:10px;white-space:nowrap'>"
            f"{esc(k)}</td><td>{esc(v)}</td></tr>"
            for k, v in meta
        )
        self.record_view.setHtml(
            f"<h3 style='margin:0 0 6px 0'>{esc(item.get('title'))}</h3>"
            f"<table style='font-size:12px'>{rows}</table>"
        )

        assessments = item.get("assessments", [])
        self.assessment_table.fill(
            [
                [
                    "AI" if a["reviewer_role"] == "assessor" else "You",
                    a["decision"],
                    a["exclusion_code"] or "—",
                    a["reason"],
                    a["quote"] or "—",
                    a["quote_page"] if a["quote_page"] else "—",
                    _quote_state(a),
                ]
                for a in assessments
            ]
        )

        cited = next(
            (a for a in assessments if a["reviewer_role"] == "assessor" and a["quote_page"]),
            None,
        )
        if cited is None:
            self.page_label.setText("The cited page")
            self.page_view.setPlainText(
                "The assessment quoted nothing, so there is no page to show."
            )
            return
        page_no = cited["quote_found_on"] or cited["quote_page"]
        text = self._engine().page_text(item["record_id"], page_no)
        self.page_label.setText(
            f"PDF page {page_no}"
            + (
                " (where the quotation actually is)"
                if cited["quote_found_on"] and cited["quote_found_on"] != cited["quote_page"]
                else ""
            )
        )
        self.page_view.setHtml(_highlight(text, cited["quote"]))

    # -- display -----------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            self._queue = []
            self.queue_list.clear()
            self.status_label.setText("")
            return

        self.code_combo.clear()
        self.code_combo.addItem("— none —", "")
        for code in self.project.exclusion_codes():
            self.code_combo.addItem(f"{code['code']} {code['label']}", code["code"])

        try:
            engine = self._engine()
        except Exception as exc:
            self.blocker_label.setText(str(exc))
            self.blocker_label.show()
            return

        summary = engine.summary()
        locked = summary["protocol_locked"]
        self.blocker_label.setVisible(not locked)
        if not locked:
            self.blocker_label.setText(
                "The protocol has not been locked. An exclusion at full text "
                "is final and is reported in the PRISMA diagram, so it must "
                "be traceable to a fixed protocol."
            )
        self.run_button.setEnabled(locked)

        self.status_label.setText(
            f"{summary.get('n_retrieved', 0)} report(s) retrieved · "
            f"{summary.get('n_assessed', 0)} assessed · "
            f"{summary.get('n_included', 0)} included · "
            f"{summary.get('n_excluded', 0)} excluded · "
            f"{summary.get('n_unclear', 0)} unclear · "
            f"{summary.get('n_pending', 0)} still to assess · "
            f"{summary.get('n_awaiting_review', 0)} waiting for you"
            + (
                f" · {summary['quotes_not_found']} quotation(s) not found in their report"
                if summary.get("quotes_not_found")
                else ""
            )
        )

        self._queue = engine.review_queue()
        self.queue_list.clear()
        for item in self._queue:
            self.queue_list.addItem(
                QListWidgetItem(
                    f"#{item['record_id']}  "
                    f"{_REASON_LABELS.get(item['reason'], item['reason'])}\n"
                    f"{(item.get('title') or '')[:65]}"
                )
            )
        has_items = bool(self._queue)
        for button in (self.include_button, self.exclude_button, self.unclear_button):
            button.setEnabled(has_items)
        if has_items:
            self.queue_list.setCurrentRow(0)
        else:
            self._show_item(-1)

        self.included_table.fill(
            [
                [
                    f"#{r['id']}", r["year"], r["title"], r["journal"], r["doi"] or "—",
                ]
                for r in engine.included_records()
            ]
        )
        self.exclusion_table.fill(
            [[r["exclusion_code"], r["label"], r["n"]] for r in engine.exclusion_reasons()]
        )
        self.runs_table.fill(
            [
                [
                    run["status"], run["n_records"], run["model"], run["n_calls"],
                    f"{run['cost_usd']:.4f}",
                    (run["started_at"] or "").replace("T", " ").replace("Z", ""),
                ]
                for run in engine.runs()
            ]
        )


def _quote_state(assessment: dict) -> str:
    if not assessment["quote"]:
        return "no quotation"
    if assessment["quote_verified"]:
        return "on the cited page"
    if assessment["quote_found_on"]:
        return f"on page {assessment['quote_found_on']} instead"
    return "NOT IN THE REPORT"


def _highlight(text: str, quote: str) -> str:
    """Show the page with the quoted words marked.

    The span is located in the normalised form and mapped back, because a
    quotation almost always runs across a line break and a literal search
    would find nothing on exactly the pages it matters most.
    """
    from ...fulltext.pdf import locate_quote_span

    text = text or ""
    span = locate_quote_span(text, quote) if quote else None
    if span is None:
        return (
            "<pre style='white-space:pre-wrap;font-family:inherit'>"
            f"{html.escape(text)}</pre>"
        )
    start, end = span
    marked = (
        html.escape(text[:start])
        + "<mark style='background:#fff2a8'>"
        + html.escape(text[start:end])
        + "</mark>"
        + html.escape(text[end:])
    )
    return f"<pre style='white-space:pre-wrap;font-family:inherit'>{marked}</pre>"
