"""Stage 5: title and abstract screening."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
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
from ...exports import write_screening_log
from ...screening import ScreeningEngine, ScreeningSettings
from ...screening.engine import ScreeningError
from ..dialogs import confirm_cost
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["ScreenPage"]

_ROLE_LABELS = {
    "pilot_human": "Pilot (you)",
    "screener_a": "Screener A",
    "screener_b": "Screener B",
    "adjudicator": "Adjudicator",
    "qc": "Quality control",
    "human": "You",
}

_REASON_LABELS = {
    "conflict": "Screeners disagreed",
    "unclear": "Unclear",
    "qc_rescue": "Quality control rescued",
    "random_audit": "Random audit",
    "quote_failed": "Quotation not found",
    "revisit": "You left it unclear",
}


class ScreenPage(StagePage):
    title = "Screen"
    subtitle = "Two blinded AI screeners, adjudication, and your decision on what is left."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._stop_requested = False
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
        self.pilot_button = QPushButton("Run pilot")
        self.pilot_button.setToolTip(
            "Screen the records you labelled by hand and report how closely "
            "the AI agrees with you."
        )
        self.pilot_button.clicked.connect(self._run_pilot)
        controls.addWidget(self.pilot_button)

        self.waive_button = QPushButton("Skip pilot…")
        self.waive_button.clicked.connect(self._waive_pilot)
        controls.addWidget(self.waive_button)

        self.run_button = QPushButton("Screen all records")
        self.run_button.clicked.connect(self._run_full)
        controls.addWidget(self.run_button)

        self.qc_button = QPushButton("Quality-control pass")
        self.qc_button.setToolTip(
            "Re-read a reproducible sample of the records both screeners "
            "excluded, plus any exclusion that mentions the review's own "
            "concepts."
        )
        self.qc_button.clicked.connect(self._run_qc)
        controls.addWidget(self.qc_button)

        self.settle_button = QPushButton("Let the AI settle unclear records…")
        self.settle_button.setToolTip(
            "The AI reads each unclear record again and includes or excludes it "
            "where the abstract allows. The rest go to full text."
        )
        self.settle_button.clicked.connect(self._settle_unclear)
        controls.addWidget(self.settle_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self._request_stop)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.stop_button)

        controls.addStretch(1)
        self.export_button = QPushButton("Export screening log")
        self.export_button.clicked.connect(self._export)
        controls.addWidget(self.export_button)
        self._layout.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

        tabs = QTabWidget()
        self.tabs = tabs
        tabs.addTab(self._build_queue_tab(), "Needs your decision")
        tabs.addTab(self._build_pilot_tab(), "Pilot")
        tabs.addTab(self._build_agreement_tab(), "Agreement")
        self._layout.addWidget(tabs, 1)

    # -- tabs --------------------------------------------------------------

    def _build_queue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            "Conflicts, unclear records, rescued exclusions and a random audit "
            "of decisions the screeners agreed on. Your decision overrides the "
            "AI's and is written to the audit log."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5b6677;")
        layout.addWidget(hint)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Show"))
        self.queue_mode = QComboBox()
        self.queue_mode.addItem("Records waiting for your decision", "queue")
        self.queue_mode.addItem("My 'still unclear' decisions, to decide again", "revisit")
        self.queue_mode.currentIndexChanged.connect(self._mode_changed)
        mode_row.addWidget(self.queue_mode, 1)
        layout.addLayout(mode_row)
        self.revisit_hint = QLabel(
            "Read each abstract and the screeners' reasons, then decide again. "
            "Your earlier decision stays in the audit log. Keep "
            "a record unclear only when the abstract cannot settle it: each "
            "one kept is a PDF to fetch."
        )
        self.revisit_hint.setWordWrap(True)
        self.revisit_hint.setStyleSheet("color: #5b6677;")
        self.revisit_hint.setVisible(False)
        layout.addWidget(self.revisit_hint)
        self._kept_unclear: set[int] = set()

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
        right_layout.addWidget(self.record_view, 3)

        right_layout.addWidget(section_label("What each reviewer said"))
        self.decisions_table = SimpleTable(
            ["Reviewer", "Decision", "Code", "Reason", "Quotation"],
            stretch_columns=[3, 4],
            wrap=True,
        )
        right_layout.addWidget(self.decisions_table, 3)

        decision_row = QHBoxLayout()
        self.code_combo = QComboBox()
        self.code_combo.setMinimumWidth(190)
        decision_row.addWidget(QLabel("Exclusion code"))
        decision_row.addWidget(self.code_combo)
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Reason (recorded with your decision)")
        decision_row.addWidget(self.note_edit, 1)
        right_layout.addLayout(decision_row)

        buttons = QHBoxLayout()
        self.include_button = QPushButton("Include")
        self.include_button.clicked.connect(lambda: self._resolve("include"))
        buttons.addWidget(self.include_button)
        self.exclude_button = QPushButton("Exclude")
        self.exclude_button.clicked.connect(lambda: self._resolve("exclude"))
        buttons.addWidget(self.exclude_button)
        self.unclear_button = QPushButton("Still unclear — keep for full text")
        self.unclear_button.clicked.connect(lambda: self._resolve("unclear"))
        buttons.addWidget(self.unclear_button)
        right_layout.addLayout(buttons)
        # How many records the reviewer has kept unclear, and a note when
        # decisions come faster than an abstract can be read.
        self.unclear_note = QLabel()
        self.unclear_note.setWordWrap(True)
        self.unclear_note.setStyleSheet("color: #5b6677;")
        right_layout.addWidget(self.unclear_note)
        self.pace_note = QLabel(
            "These decisions are coming within a few seconds of each other. "
            "Each one is logged with its time; take the time to read the "
            "abstract and the screeners' reasons."
        )
        self.pace_note.setWordWrap(True)
        self.pace_note.setStyleSheet("color: #9a4b06;")
        self.pace_note.setVisible(False)
        right_layout.addWidget(self.pace_note)
        self._decision_times: list[float] = []

        splitter.addWidget(right)
        splitter.setSizes([300, 760])
        layout.addWidget(splitter, 1)
        return page

    def _build_pilot_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            "Label a sample yourself, then run the pilot. The sample is drawn "
            "reproducibly from the project, so the same records are chosen on "
            "any machine. The full run is blocked until the pilot has run or "
            "been skipped with a reason."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5b6677;")
        layout.addWidget(hint)

        self.pilot_progress_label = QLabel()
        layout.addWidget(self.pilot_progress_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.pilot_list = QListWidget()
        self.pilot_list.setMaximumWidth(300)
        self.pilot_list.setWordWrap(True)
        self.pilot_list.currentRowChanged.connect(self._show_pilot_record)
        splitter.addWidget(self.pilot_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)
        self.pilot_view = QTextBrowser()
        right_layout.addWidget(self.pilot_view, 1)

        label_row = QHBoxLayout()
        label_row.addWidget(QLabel("Your judgement:"))
        for decision, text in (
            ("include", "Include"),
            ("exclude", "Exclude"),
            ("unclear", "Unclear"),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, d=decision: self._label_pilot(d))
            label_row.addWidget(button)
        label_row.addStretch(1)
        right_layout.addLayout(label_row)

        splitter.addWidget(right)
        splitter.setSizes([300, 760])
        layout.addWidget(splitter, 1)
        self._pilot_records: list[dict] = []
        return page

    def _build_agreement_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        note = QLabel(
            "Percentage agreement flatters any screening process when most "
            "records are obvious excludes. κ corrects for chance; PABAK "
            "corrects κ's own weakness when one category dominates. All "
            "three are reported in the Methods draft."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #5b6677;")
        layout.addWidget(note)

        self.agreement_table = SimpleTable(
            ["Comparison", "Records", "Agreement", "κ", "PABAK", "Reading", "When"]
        )
        self.agreement_table.fit_to_rows(10)
        layout.addWidget(self.agreement_table)

        layout.addWidget(section_label("Exclusions by reason"))
        self.exclusion_table = SimpleTable(["Code", "Criterion", "Records"])
        self.exclusion_table.fit_to_rows(10)
        layout.addWidget(self.exclusion_table)

        layout.addWidget(section_label("Runs"))
        self.runs_table = SimpleTable(
            ["Kind", "Status", "Records", "Model", "Calls", "From cache", "Cost (USD)", "Started"]
        )
        self.runs_table.fit_to_rows(8)
        layout.addWidget(self.runs_table)
        layout.addStretch(1)
        return page

    # -- engine ------------------------------------------------------------

    def _engine(self) -> ScreeningEngine:
        settings = load_settings(self.project)
        provider = provider_from_settings(settings)
        client = AIClient(
            self.project,
            provider,
            stage="screen",
            use_cache=settings.use_cache,
        )
        return ScreeningEngine(
            self.project,
            client,
            ScreeningSettings(budget_usd=settings.budget_usd),
        )

    def _busy(self, busy: bool) -> None:
        for button in (
            self.pilot_button, self.run_button, self.qc_button,
            self.settle_button, self.waive_button, self.export_button,
        ):
            button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.progress.setVisible(busy)
        if not busy:
            self._stop_requested = False

    def _request_stop(self) -> None:
        self._stop_requested = True
        self.status_message.emit("Stopping after the current record…")

    def _should_stop(self) -> bool:
        return self._stop_requested

    def _start(self, fn, *args, **kwargs) -> None:
        if self._task is not None and self._task.isRunning():
            return
        self._busy(True)
        task = Task(fn, *args, **kwargs)
        task.succeeded.connect(self._run_done)
        task.failed.connect(self._run_failed)
        task.finished.connect(lambda: self._busy(False))
        self._task = task
        task.start()

    def _run_done(self, outcome) -> None:
        self.progress.hide()
        kind = "second look" if outcome.kind == "adjudication" else outcome.kind
        parts = [f"{kind} run finished: {outcome.n_done} record(s)"]
        if outcome.n_adjudicated:
            parts.append(f"{outcome.n_adjudicated} adjudicated")
        if outcome.n_qc_rescued:
            parts.append(f"{outcome.n_qc_rescued} exclusion(s) rescued")
        if outcome.n_queued:
            parts.append(f"{outcome.n_queued} waiting for you")
        self.status_message.emit("; ".join(parts) + ".")
        for message in outcome.messages:
            QMessageBox.information(self, "Screening", message)
        if outcome.agreement is not None and outcome.kind == "pilot":
            QMessageBox.information(
                self, "Pilot agreement",
                f"{outcome.agreement.describe()}\n\n"
                f"Reading of κ: {outcome.agreement.interpretation}.\n"
                f"{outcome.agreement.note}",
            )
        self.refresh()
        self.data_changed.emit()

    def _run_failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Screening stopped", message)
        self.refresh()

    # -- actions -----------------------------------------------------------

    def _run_pilot(self) -> None:
        if self.project is None:
            return
        try:
            engine = self._engine()
            if not engine.pilot_labels():
                QMessageBox.information(
                    self, "Label some records first",
                    "The pilot compares your own judgements with the AI's on "
                    "the same records. Label a few on the Pilot tab, then run "
                    "it.",
                )
                return
        except Exception as exc:
            QMessageBox.warning(self, "Cannot run the pilot", str(exc))
            return
        self._start(engine.run_pilot, on_progress=self._progress)

    def _waive_pilot(self) -> None:
        if self.project is None:
            return
        reason, ok = QInputDialog.getText(
            self, "Skip the pilot",
            "The pilot is how you find out whether the AI reads your protocol "
            "the way you do.\nSkipping it is recorded and reported in the "
            "Methods section.\n\nReason:",
        )
        if not ok:
            return
        try:
            self._engine().waive_pilot(reason)
        except ScreeningError as exc:
            QMessageBox.warning(self, "Cannot skip the pilot", str(exc))
            return
        self.status_message.emit("Pilot skipped; the reason has been logged.")
        self.refresh()
        self.data_changed.emit()

    def _run_full(self) -> None:
        if self.project is None:
            return
        try:
            engine = self._engine()
            estimate = engine.estimate_full_run()
        except ScreeningError as exc:
            QMessageBox.warning(self, "Cannot screen yet", str(exc))
            return
        except Exception as exc:
            QMessageBox.critical(self, "Cannot screen yet", str(exc))
            return
        if estimate.n_calls == 0 and estimate.n_cached == 0:
            QMessageBox.information(
                self, "Nothing to screen",
                "Every record already has a screening decision.",
            )
            return
        if not confirm_cost(estimate, "Screen all records", self):
            return
        self._start(
            engine.run_full, on_progress=self._progress, should_stop=self._should_stop
        )

    def _settle_unclear(self) -> None:
        if self.project is None:
            return
        try:
            engine = self._engine()
            n = len(engine.unclear_to_settle())
        except Exception as exc:
            QMessageBox.critical(self, "Cannot do that yet", str(exc))
            return
        if not n:
            QMessageBox.information(
                self, "Nothing to settle",
                "No record with an abstract is unclear. Records without an "
                "abstract need your own decision or the full text.",
            )
            return
        answer = QMessageBox.question(
            self, "Let the AI settle unclear records",
            f"{n} unclear record(s) have an abstract. The AI will read each "
            "one again against the protocol and include or exclude it where "
            "the abstract allows; the rest stay unclear and go to full text.\n\n"
            "Where it decides, its decision replaces the 'unclear', including "
            "any you made yourself. The audit log records that you handed "
            "these decisions to the AI, and the AI-use statement reports it. "
            "An exclusion of a record a screener wanted is still put in front "
            "of you.\n\nGo ahead?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            estimate = engine.estimate_second_look()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot do that yet", str(exc))
            return
        if not confirm_cost(estimate, "Second look at unclear records", self):
            return
        self._start(
            engine.settle_unclear, on_progress=self._progress,
            should_stop=self._should_stop,
        )

    def _run_qc(self) -> None:
        if self.project is None:
            return
        try:
            engine = self._engine()
        except Exception as exc:
            QMessageBox.critical(self, "Cannot run quality control", str(exc))
            return
        self._start(
            engine.run_qc, on_progress=self._progress, should_stop=self._should_stop
        )

    def _progress(self, done: int, total: int, message: str) -> None:
        # Runs on the GUI thread: Task delivers progress through a signal.
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%p%)")

    def _export(self) -> None:
        if self.project is None:
            return
        try:
            path = write_screening_log(self.project)
        except Exception as exc:
            QMessageBox.critical(self, "Could not write the log", str(exc))
            return
        QMessageBox.information(
            self, "Screening log written",
            f"Saved to the project's exports folder as {path.name}.",
        )
        self.status_message.emit(f"Screening log written to {path.name}.")

    # -- the pilot tab -----------------------------------------------------

    def _label_pilot(self, decision: str) -> None:
        row = self.pilot_list.currentRow()
        if self.project is None or row < 0 or row >= len(self._pilot_records):
            return
        record = self._pilot_records[row]
        code = ""
        if decision == "exclude":
            # The code with what it means: "E4" alone asked the reviewer to
            # remember eight definitions.
            codes = self.project.exclusion_codes()
            choices = [f"{c['code']} — {c['label']}" for c in codes]
            choice, ok = QInputDialog.getItem(
                self, "Which criterion does it fail?",
                "First exclusion code the record fails (checked in this order):",
                choices, 0, False,
            )
            if not ok:
                return
            code = codes[choices.index(choice)]["code"]
        self._engine().record_pilot_label(record["id"], decision, exclusion_code=code)
        self.refresh()
        if row + 1 < self.pilot_list.count():
            self.pilot_list.setCurrentRow(row + 1)

    def _show_pilot_record(self, row: int) -> None:
        if row < 0 or row >= len(self._pilot_records):
            self.pilot_view.clear()
            return
        self.pilot_view.setHtml(self._render_record(self._pilot_records[row]))

    # -- the queue ---------------------------------------------------------

    def _render_record(self, record: dict) -> str:
        def esc(value: object) -> str:
            return html.escape("" if value is None else str(value))

        authors = record.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        meta = [
            ("Year", record.get("year")),
            ("Publication", record.get("journal")),
            ("Type", record.get("doc_type")),
            ("Authors", "; ".join(str(a) for a in authors)),
            ("DOI", record.get("doi") or "—"),
        ]
        rows = "".join(
            f"<tr><td style='color:#5b6677;padding-right:10px;white-space:nowrap'>"
            f"{esc(k)}</td><td>{esc(v)}</td></tr>"
            for k, v in meta
        )
        abstract = esc(record.get("abstract") or "No abstract in this record.")
        return (
            f"<h3 style='margin:0 0 8px 0'>{esc(record.get('title'))}</h3>"
            f"<table style='font-size:12px'>{rows}</table>"
            f"<p style='margin-top:10px'>{abstract}</p>"
        )

    def focus_on(self, part: str) -> None:
        """Open the tab and list the next-step guide points to."""
        if part == "pilot":
            self.tabs.setCurrentIndex(1)
        elif part in ("queue", "revisit"):
            self.tabs.setCurrentIndex(0)
            self.queue_mode.setCurrentIndex(1 if part == "revisit" else 0)

    def _mode(self) -> str:
        return str(self.queue_mode.currentData() or "queue")

    def _mode_changed(self, *args) -> None:
        self.revisit_hint.setVisible(self._mode() == "revisit")
        self._kept_unclear.clear()
        self.refresh()

    def _show_item(self, row: int) -> None:
        if row < 0 or row >= len(self._queue):
            self.record_view.clear()
            self.decisions_table.fill([])
            return
        item = self._queue[row]
        self.record_view.setHtml(self._render_record(item))
        self.decisions_table.fill(
            [
                [
                    _ROLE_LABELS.get(d["reviewer_role"], d["reviewer_role"]),
                    d["decision"],
                    d["exclusion_code"] or "—",
                    d["reason"],
                    (d["quote"] or "—")
                    + ("" if d["quote_verified"] or not d["quote"]
                       else "  [not found in the record]"),
                ]
                for d in item.get("decisions", [])
            ]
        )
        current = item.get("current_code") or ""
        index = self.code_combo.findData(current)
        self.code_combo.setCurrentIndex(index if index >= 0 else 0)

    def _resolve(self, decision: str) -> None:
        row = self.queue_list.currentRow()
        if self.project is None or row < 0 or row >= len(self._queue):
            return
        item = self._queue[row]
        code = str(self.code_combo.currentData() or "")
        reason = self.note_edit.text().strip()
        if decision == "unclear":
            # Unclear has to be about something: which criterion can the
            # abstract not settle? A record that clearly fails one is an
            # exclusion, not an unclear.
            codes = self.project.exclusion_codes()
            choices = [f"{c['code']} — {c['label']}" for c in codes
                       if not c["label"].lower().startswith("full text")]
            choice, ok = QInputDialog.getItem(
                self, "What can the abstract not settle?",
                "Keep it unclear only if the abstract cannot tell you this. "
                "If it clearly fails a criterion, press Cancel and exclude it.",
                choices, 0, False,
            )
            if not ok:
                return
            reason = f"Abstract does not settle {choice}" + (f"; {reason}" if reason else "")
        try:
            self._engine().resolve(
                item["record_id"], decision,
                exclusion_code=code, reason=reason,
            )
        except ScreeningError as exc:
            QMessageBox.warning(self, "Cannot record that", str(exc))
            return
        self.note_edit.clear()
        import time

        self._decision_times = (self._decision_times + [time.monotonic()])[-4:]
        gaps = [b - a for a, b in zip(self._decision_times, self._decision_times[1:])]
        self.pace_note.setVisible(len(gaps) >= 3 and all(g < 6 for g in gaps))
        if decision == "unclear" and self._mode() == "revisit":
            # Kept unclear on purpose: drop it from this pass of the list.
            self._kept_unclear.add(item["record_id"])
        self.status_message.emit(
            f"Record {item['record_id']} recorded as {decision}."
        )
        self.refresh()
        self.data_changed.emit()
        if self.queue_list.count():
            self.queue_list.setCurrentRow(min(row, self.queue_list.count() - 1))

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

        locked = self.project.protocol.is_locked()
        self.blocker_label.setVisible(not locked)
        if not locked:
            self.blocker_label.setText(
                "The protocol has not been locked yet. Screening reads the "
                "locked protocol, so that every decision can be traced to the "
                "exact wording it was judged against. Lock it on the Project "
                "page first."
            )
        for button in (self.pilot_button, self.run_button, self.qc_button):
            button.setEnabled(locked)
        n_unclear = int(self.project.db.scalar(
            "SELECT COUNT(*) FROM screening_status WHERE decision = 'unclear'") or 0)
        self.settle_button.setVisible(bool(n_unclear))
        self.settle_button.setEnabled(locked)

        summary = engine.summary()
        pilot_state = (
            "pilot run" if summary["pilot_done"]
            else ("pilot skipped" if summary["pilot_waived"] else "pilot not run")
        )
        self.status_label.setText(
            f"{summary.get('n_to_screen', 0)} record(s) to screen · "
            f"{summary.get('n_included', 0)} included · "
            f"{summary.get('n_excluded', 0)} excluded · "
            f"{summary.get('n_unclear', 0)} unclear · "
            f"{summary.get('n_not_yet_screened', 0)} not yet screened · "
            f"{summary.get('n_awaiting_review', 0)} waiting for you · {pilot_state}"
        )

        if self._mode() == "revisit":
            self._queue = [
                item for item in engine.revisit_list()
                if item["record_id"] not in self._kept_unclear
            ]
        else:
            self._queue = engine.review_queue()
        n_revisit = len(engine.revisit_list())
        self.queue_mode.setItemText(
            1, f"My 'still unclear' decisions, to decide again ({n_revisit})"
        )
        # Before the pilot, the only work on this page is labelling it: open
        # on that tab rather than on an empty queue.
        if (
            locked and not summary["pilot_done"] and not summary["pilot_waived"]
            and not self._queue and hasattr(self, "tabs")
        ):
            self.tabs.setCurrentIndex(1)
        self.queue_list.clear()
        for item in self._queue:
            self.queue_list.addItem(
                QListWidgetItem(
                    f"#{item['record_id']}  {_REASON_LABELS.get(item['reason'], item['reason'])}\n"
                    f"{(item.get('title') or '')[:70]}"
                )
            )
        n_kept = int(self.project.db.scalar(
            "SELECT COUNT(*) FROM screening_status WHERE source = 'human' "
            "AND decision = 'unclear'"
        ) or 0)
        self.unclear_note.setText(
            f"You have kept {n_kept} record{'s' if n_kept != 1 else ''} unclear. "
            "Each one goes to full text: a PDF to fetch and read."
            if n_kept else ""
        )
        has_items = bool(self._queue)
        for button in (self.include_button, self.exclude_button, self.unclear_button):
            button.setEnabled(has_items)
        if has_items:
            self.queue_list.setCurrentRow(0)
        else:
            self._show_item(-1)

        labels = engine.pilot_labels()
        self._pilot_records = engine.pilot_sample()
        self.pilot_list.clear()
        for record in self._pilot_records:
            mark = labels.get(record["id"])
            prefix = f"[{mark}] " if mark else "[ ] "
            self.pilot_list.addItem(
                QListWidgetItem(f"{prefix}#{record['id']}\n{(record['title'] or '')[:70]}")
            )
        self.pilot_progress_label.setText(
            f"{len(labels)} of {len(self._pilot_records)} pilot records labelled."
        )

        self.agreement_table.fill(
            [
                [
                    row["scope"],
                    row["n"],
                    f"{row['percent_agreement']:.1%}",
                    "—" if row["kappa"] is None else f"{row['kappa']:.3f}",
                    "—" if row["pabak"] is None else f"{row['pabak']:.3f}",
                    _interpret(row["kappa"]),
                    (row["created_at"] or "").replace("T", " ").replace("Z", ""),
                ]
                for row in engine.agreement_reports()
            ]
        )
        self.exclusion_table.fill(
            [[r["exclusion_code"], r["label"], r["n"]] for r in engine.exclusion_reasons()]
        )
        self.runs_table.fill(
            [
                [
                    run["kind"], run["status"], run["n_records"], run["model"],
                    run["n_calls"], run["n_cached"], f"{run['cost_usd']:.4f}",
                    (run["started_at"] or "").replace("T", " ").replace("Z", ""),
                ]
                for run in engine.runs()
            ]
        )


def _interpret(kappa: float | None) -> str:
    from ...screening.stats import interpret_kappa

    return interpret_kappa(kappa)
