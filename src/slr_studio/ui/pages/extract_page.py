"""Stage 8: extracting the data the synthesis will count."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
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

from ...ai.client import AIClient
from ...ai.settings import load_settings, provider_from_settings
from ...exports import write_extraction_matrix
from ...extraction import ExtractionEngine, ExtractionSettings, SecondCoder
from ...extraction.engine import ExtractionError
from ...extraction.schemas import DIRECTIONS
from ..dialogs import confirm_cost
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["ExtractPage"]

_REASON_LABELS = {
    "direction_mismatch": "Direction disagreed",
    "quote_failed": "Quotation problem",
    "design_rule": "Design or appraisal",
    "nothing_extracted": "Nothing extracted",
}


class ExtractPage(StagePage):
    title = "Extract"
    subtitle = "The study, the relationships it states, and the words each one rests on."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._stop = False
        self._queue: list[dict] = []
        self._relationships: list[dict] = []

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self._layout.addWidget(self.status_label)

        self.blocker_label = QLabel()
        self.blocker_label.setWordWrap(True)
        self.blocker_label.setObjectName("blockerLabel")
        self.blocker_label.hide()
        self._layout.addWidget(self.blocker_label)

        controls = QHBoxLayout()
        self.run_button = QPushButton("Extract from included reports")
        self.run_button.clicked.connect(self._run)
        controls.addWidget(self.run_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._request_stop)
        controls.addWidget(self.stop_button)
        self.sample_button = QPushButton("Choose second-coding sample")
        self.sample_button.setToolTip(
            "Picks a reproducible share of the reports for a second person to "
            "code independently, so inter-coder agreement can be reported."
        )
        self.sample_button.clicked.connect(self._choose_sample)
        controls.addWidget(self.sample_button)
        self.compare_button = QPushButton("Compare coders")
        self.compare_button.clicked.connect(self._compare)
        controls.addWidget(self.compare_button)
        controls.addStretch(1)
        self.export_button = QPushButton("Export extraction matrix")
        self.export_button.clicked.connect(self._export)
        controls.addWidget(self.export_button)
        self._layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

        tabs = QTabWidget()
        tabs.addTab(self._build_queue_tab(), "Needs your decision")
        tabs.addTab(self._build_inventory_tab(), "All relationships")
        tabs.addTab(self._build_agreement_tab(), "Second coder")
        self._layout.addWidget(tabs, 1)

    # -- tabs --------------------------------------------------------------

    def _build_queue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            "A direction the blind check read differently, a quotation that is "
            "not where it says, a study called an experiment without a control. "
            "The extracted value stands until you change it."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5b6677;")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.queue_list = QListWidget()
        self.queue_list.setMaximumWidth(290)
        self.queue_list.setWordWrap(True)
        self.queue_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.queue_list.currentRowChanged.connect(self._show_item)
        splitter.addWidget(self.queue_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)

        self.study_view = QTextBrowser()
        self.study_view.setMaximumHeight(185)
        right_layout.addWidget(self.study_view)

        right_layout.addWidget(section_label("Relationships from this report"))
        self.relationship_table = SimpleTable(
            ["Predictor", "Outcome", "Direction", "Check", "Basis", "Quotation",
             "Page", "Found"],
            stretch_columns=[0, 1, 5],
        )
        self.relationship_table.currentCellChanged.connect(
            lambda row, *_: self._select_relationship(row)
        )
        right_layout.addWidget(self.relationship_table, 1)

        fix_row = QHBoxLayout()
        fix_row.addWidget(QLabel("Set direction to"))
        self.direction_combo = QComboBox()
        for direction in DIRECTIONS:
            self.direction_combo.addItem(direction, direction)
        fix_row.addWidget(self.direction_combo)
        self.apply_button = QPushButton("Change this relationship")
        self.apply_button.clicked.connect(self._apply_direction)
        fix_row.addWidget(self.apply_button)
        self.withdraw_button = QPushButton("Withdraw it…")
        self.withdraw_button.setToolTip(
            "Stops this relationship being counted. It stays in the record and "
            "in the audit log."
        )
        self.withdraw_button.clicked.connect(self._withdraw)
        fix_row.addWidget(self.withdraw_button)
        fix_row.addStretch(1)
        self.accept_button = QPushButton("Checked — accept this report")
        self.accept_button.clicked.connect(self._accept)
        fix_row.addWidget(self.accept_button)
        right_layout.addLayout(fix_row)

        splitter.addWidget(right)
        splitter.setSizes([290, 770])
        layout.addWidget(splitter, 1)
        return page

    def _build_inventory_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        note = QLabel(
            "Every relationship the synthesis will read, with the authors' own "
            "labels. Mapping these onto shared constructs is the next stage."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #5b6677;")
        layout.addWidget(note)
        self.inventory_table = SimpleTable(
            ["Record", "Study", "Predictor", "Outcome", "Type", "Direction",
             "Basis", "Statistic", "Page", "Found", "Check"],
            stretch_columns=[1, 2, 3],
        )
        layout.addWidget(self.inventory_table, 1)
        return page

    def _build_agreement_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        note = QLabel(
            "Agreement is reported field by field. Two coders almost always "
            "agree about the country and often differ about the design family; "
            "one pooled number would hide exactly that."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #5b6677;")
        layout.addWidget(note)

        layout.addWidget(section_label("Reports chosen for second coding"))
        self.sample_table = SimpleTable(["Record", "Study", "Chosen", "Coded"],
                                        stretch_columns=[1])
        self.sample_table.fit_to_rows(8)
        layout.addWidget(self.sample_table)

        layout.addWidget(section_label("Inter-coder agreement"))
        self.agreement_table = SimpleTable(
            ["Field", "Compared", "Agreed", "Agreement", "κ", "PABAK", "When"]
        )
        self.agreement_table.fit_to_rows(12)
        layout.addWidget(self.agreement_table)

        layout.addWidget(section_label("Runs"))
        self.runs_table = SimpleTable(
            ["Status", "Reports", "Relationships", "Model", "Calls", "Cost (USD)",
             "Started"]
        )
        self.runs_table.fit_to_rows(5)
        layout.addWidget(self.runs_table)
        layout.addStretch(1)
        return page

    # -- engine ------------------------------------------------------------

    def _engine(self) -> ExtractionEngine:
        settings = load_settings(self.project)
        client = AIClient(
            self.project,
            provider_from_settings(settings),
            stage="extract",
            use_cache=settings.use_cache,
        )
        return ExtractionEngine(
            self.project, client, ExtractionSettings(budget_usd=settings.budget_usd)
        )

    def _busy(self, busy: bool) -> None:
        for button in (self.run_button, self.sample_button, self.compare_button,
                       self.export_button):
            button.setEnabled(not busy)
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
        except ExtractionError as exc:
            QMessageBox.warning(self, "Cannot extract yet", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Cannot extract yet", str(exc))
            return
        if estimate.n_calls == 0 and estimate.n_cached == 0:
            QMessageBox.information(
                self, "Nothing to extract",
                "Every included report has already been extracted. Include "
                "more reports on the Full-text page first.",
            )
            return
        if not confirm_cost(estimate, "Extract from included reports", self):
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
            f"Extracted {outcome.n_done} report(s) and "
            f"{outcome.n_relationships} relationship(s); "
            f"{outcome.n_queued} waiting for you."
        )
        for message in outcome.messages:
            QMessageBox.warning(self, "Extraction", message)
        self.refresh()
        self.data_changed.emit()

    def _failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Extraction stopped", message)

    # -- the queue ---------------------------------------------------------

    def _selected_relationship(self) -> dict | None:
        row = self.relationship_table.currentRow()
        if row < 0 or row >= len(self._relationships):
            return None
        return self._relationships[row]

    def _select_relationship(self, row: int) -> None:
        relationship = (
            self._relationships[row]
            if 0 <= row < len(self._relationships)
            else None
        )
        enabled = relationship is not None
        self.apply_button.setEnabled(enabled)
        self.withdraw_button.setEnabled(enabled)
        if relationship is not None:
            index = self.direction_combo.findData(relationship["direction"])
            self.direction_combo.setCurrentIndex(max(index, 0))

    def _apply_direction(self) -> None:
        relationship = self._selected_relationship()
        if relationship is None or self.project is None:
            return
        direction = str(self.direction_combo.currentData())
        if direction == relationship["direction"]:
            QMessageBox.information(
                self, "No change", "That is already the recorded direction."
            )
            return
        reason, ok = QInputDialog.getText(
            self, "Why the change?",
            "This overrides what was extracted and is written to the audit "
            "log.\n\nReason:",
        )
        if not ok:
            return
        self._engine().correct_relationship(
            relationship["id"], direction=direction, reason=reason.strip()
        )
        self.status_message.emit(
            f"Relationship {relationship['id']} set to {direction}."
        )
        self.refresh()
        self.data_changed.emit()

    def _withdraw(self) -> None:
        relationship = self._selected_relationship()
        if relationship is None or self.project is None:
            return
        reason, ok = QInputDialog.getText(
            self, "Withdraw this relationship",
            "It stops being counted but stays in the record.\n\nWhy?",
        )
        if not ok:
            return
        try:
            self._engine().delete_relationship(relationship["id"], reason.strip())
        except ExtractionError as exc:
            QMessageBox.warning(self, "Cannot withdraw it", str(exc))
            return
        self.refresh()
        self.data_changed.emit()

    def _accept(self) -> None:
        row = self.queue_list.currentRow()
        if self.project is None or row < 0 or row >= len(self._queue):
            return
        item = self._queue[row]
        self._engine().resolve(item["record_id"], "checked by the reviewer")
        self.status_message.emit(f"Report {item['record_id']} accepted.")
        self.refresh()
        self.data_changed.emit()

    # -- the second coder --------------------------------------------------

    def _choose_sample(self) -> None:
        if self.project is None:
            return
        settings = ExtractionSettings()
        chosen = SecondCoder(self.project).choose_sample(
            settings.second_coder_fraction, settings.second_coder_minimum
        )
        if not chosen:
            QMessageBox.information(
                self, "Nothing to sample",
                "Extract from some reports first; the sample is drawn from "
                "what has been coded.",
            )
            return
        QMessageBox.information(
            self, "Second-coding sample",
            f"{len(chosen)} report(s) chosen: "
            + ", ".join(str(r) for r in chosen)
            + ".\n\nThe same reports are chosen on any machine, so the sample "
            "is reproducible.",
        )
        self.refresh()
        self.data_changed.emit()

    def _compare(self) -> None:
        if self.project is None:
            return
        comparison = SecondCoder(self.project).compare()
        QMessageBox.information(self, "Inter-coder agreement", comparison.describe())
        self.refresh()

    def _export(self) -> None:
        if self.project is None:
            return
        try:
            path = write_extraction_matrix(self.project)
        except Exception as exc:
            QMessageBox.critical(self, "Could not write the matrix", str(exc))
            return
        QMessageBox.information(
            self, "Extraction matrix written",
            f"Saved to the project's exports folder as {path.name}.",
        )
        self.status_message.emit(f"Extraction matrix written to {path.name}.")

    # -- display -----------------------------------------------------------

    @staticmethod
    def _found_label(relationship: dict) -> str:
        # Kept short on purpose: this column competes for width with the
        # quotation, which is the one a reviewer actually needs to read.
        if not relationship["quote"]:
            return "—"
        if relationship["quote_verified"]:
            return "cited page"
        if relationship["quote_found_on"]:
            return f"page {relationship['quote_found_on']}"
        return "NOT FOUND"

    @staticmethod
    def _check_label(relationship: dict) -> str:
        if relationship["verify_agrees"] is None:
            return relationship["verified_direction"] or "—"
        if relationship["verify_agrees"]:
            return "agrees"
        return relationship["verified_direction"] or "differs"

    def _show_item(self, row: int) -> None:
        if row < 0 or row >= len(self._queue):
            self.study_view.clear()
            self.relationship_table.fill([])
            self._relationships = []
            self._select_relationship(-1)
            return
        item = self._queue[row]

        def esc(value: object) -> str:
            return html.escape("" if value is None else str(value))

        extraction = item.get("extraction") or {}
        meta = [
            ("Why flagged", item.get("detail")),
            ("Country", extraction.get("country")),
            ("Design", f"{extraction.get('design') or '—'} "
                       f"({extraction.get('design_family') or '—'})"),
            ("Sample", extraction.get("sample_size")
             or extraction.get("sample_size_text") or "—"),
            ("Appraisal", f"{extraction.get('appraisal_instrument') or '—'}, "
                          f"concern {extraction.get('appraisal_concern') or '—'}"),
        ]
        if extraction.get("design_downgraded"):
            meta.append(("Design note", extraction.get("design_note")))
        rows = "".join(
            f"<tr><td style='color:#5b6677;padding-right:10px;white-space:nowrap;"
            f"vertical-align:top'>{esc(k)}</td><td>{esc(v)}</td></tr>"
            for k, v in meta
        )
        self.study_view.setHtml(
            f"<h3 style='margin:0 0 6px 0'>{esc(item.get('title'))}</h3>"
            f"<table style='font-size:12px'>{rows}</table>"
        )

        self._relationships = item.get("relationships", [])
        self.relationship_table.fill(
            [
                [
                    rel["predictor_label"], rel["outcome_label"], rel["direction"],
                    self._check_label(rel), rel["evidence_basis"],
                    rel["quote"] or "—",
                    rel["quote_page"] if rel["quote_page"] else "—",
                    self._found_label(rel),
                ]
                for rel in self._relationships
            ]
        )
        if self._relationships:
            self.relationship_table.selectRow(0)
        self._select_relationship(0 if self._relationships else -1)

    def refresh(self) -> None:
        if self.project is None:
            self._queue = []
            self.queue_list.clear()
            self.status_label.setText("")
            return
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
                "The protocol has not been locked. Everything extracted here "
                "is counted in the synthesis, so it has to be traceable to a "
                "fixed protocol."
            )
        self.run_button.setEnabled(locked)

        self.status_label.setText(
            f"{summary.get('n_included', 0)} included report(s) · "
            f"{summary.get('n_extracted', 0)} extracted · "
            f"{summary.get('n_relationships', 0)} relationship(s) · "
            f"{summary.get('n_pending', 0)} still to extract · "
            f"{summary.get('n_awaiting_review', 0)} waiting for you"
            + (
                f" · {summary['n_direction_mismatches']} direction(s) the check "
                "disagreed with"
                if summary.get("n_direction_mismatches") else ""
            )
            + (
                f" · {summary['n_designs_downgraded']} design(s) downgraded"
                if summary.get("n_designs_downgraded") else ""
            )
        )

        self._queue = engine.review_queue()
        self.queue_list.clear()
        for item in self._queue:
            self.queue_list.addItem(
                QListWidgetItem(
                    f"#{item['record_id']}  "
                    f"{_REASON_LABELS.get(item['reason'], item['reason'])}\n"
                    f"{(item.get('title') or '')[:62]}"
                )
            )
        has_items = bool(self._queue)
        self.accept_button.setEnabled(has_items)
        if has_items:
            self.queue_list.setCurrentRow(0)
        else:
            self._show_item(-1)

        self.inventory_table.fill(
            [
                [
                    f"#{row['record_id']}", row["title"], row["predictor_label"],
                    row["outcome_label"], row["relationship_type"], row["direction"],
                    row["evidence_basis"], row["statistic"] or "—",
                    row["quote_page"] if row["quote_page"] else "—",
                    "yes" if row["quote_verified"] else "no",
                    "" if row["verify_agrees"] is None
                    else ("agrees" if row["verify_agrees"] else "DISAGREES"),
                ]
                for row in engine.inventory()
            ]
        )

        second = SecondCoder(self.project)
        self.sample_table.fill(
            [
                [
                    f"#{row['record_id']}", row["title"],
                    (row["chosen_at"] or "").replace("T", " ").replace("Z", ""),
                    "yes" if row["coded"] else "not yet",
                ]
                for row in second.sample()
            ]
        )
        self.agreement_table.fill(
            [
                [
                    row["scope"], row["n"], row["n_agree"],
                    f"{row['percent_agreement']:.1%}",
                    "—" if row["kappa"] is None else f"{row['kappa']:.3f}",
                    "—" if row["pabak"] is None else f"{row['pabak']:.3f}",
                    (row["created_at"] or "").replace("T", " ").replace("Z", ""),
                ]
                for row in second.latest()
            ]
        )
        self.runs_table.fill(
            [
                [
                    run["status"], run["n_records"], run["n_relationships"],
                    run["model"], run["n_calls"], f"{run['cost_usd']:.4f}",
                    (run["started_at"] or "").replace("T", " ").replace("Z", ""),
                ]
                for run in engine.runs()
            ]
        )
