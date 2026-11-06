"""Stage 4: deduplication and its side-by-side review screen."""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...dedup import DedupEngine, DedupSettings
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["DedupPage"]


class DedupPage(StagePage):
    title = "Deduplicate"
    subtitle = "DOI, then title and year, then fuzzy title."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._pairs: list[dict] = []

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Fuzzy title threshold"))
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.50, 1.00)
        self.threshold.setSingleStep(0.01)
        self.threshold.setDecimals(2)
        self.threshold.setValue(0.92)
        self.threshold.setToolTip(
            "Below this similarity a pair is not proposed at all. Lower it to "
            "catch more duplicates and review more pairs by hand."
        )
        controls.addWidget(self.threshold)

        controls.addSpacing(16)
        controls.addWidget(QLabel("Merge without asking at"))
        self.auto_threshold = QDoubleSpinBox()
        self.auto_threshold.setRange(0.50, 1.00)
        self.auto_threshold.setSingleStep(0.01)
        self.auto_threshold.setDecimals(2)
        self.auto_threshold.setValue(0.97)
        self.auto_threshold.setToolTip(
            "A fuzzy pair this similar, with the same first author, merges "
            "automatically. Everything between the two thresholds is reviewed."
        )
        controls.addWidget(self.auto_threshold)

        controls.addStretch(1)
        self.run_button = QPushButton("Run deduplication")
        self.run_button.clicked.connect(self._run)
        controls.addWidget(self.run_button)
        self._layout.addLayout(controls)

        self.summary_label = QLabel("Not run yet.")
        self.summary_label.setStyleSheet("color: #5b6677;")
        self._layout.addWidget(self.summary_label)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #a8450c;")
        self.warning_label.hide()
        self._layout.addWidget(self.warning_label)

        tabs = QTabWidget()
        tabs.addTab(self._build_review_tab(), "Needs review")
        tabs.addTab(self._build_merged_tab(), "Merges applied")
        self._layout.addWidget(tabs, 1)

    # -- review tab --------------------------------------------------------

    def _build_review_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)

        hint = QLabel(
            "Pairs the rules could not settle. Your decision overrides the "
            "automatic one and is written to the audit log."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #5b6677;")
        layout.addWidget(hint)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.pair_list = QListWidget()
        self.pair_list.setMaximumWidth(280)
        self.pair_list.setWordWrap(True)
        self.pair_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.pair_list.currentRowChanged.connect(self._show_pair)
        splitter.addWidget(self.pair_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)

        compare = QSplitter(Qt.Orientation.Horizontal)
        self.left_view = QTextBrowser()
        self.right_view = QTextBrowser()
        compare.addWidget(self.left_view)
        compare.addWidget(self.right_view)
        compare.setSizes([400, 400])
        right_layout.addWidget(compare, 1)

        self.evidence_label = QLabel("")
        self.evidence_label.setWordWrap(True)
        self.evidence_label.setStyleSheet("color: #5b6677;")
        right_layout.addWidget(self.evidence_label)

        decision = QHBoxLayout()
        self.reason_edit = QLineEdit()
        self.reason_edit.setPlaceholderText("Reason (recorded with your decision)")
        decision.addWidget(self.reason_edit, 1)
        self.same_button = QPushButton("Same study — merge")
        self.same_button.clicked.connect(lambda: self._decide(True))
        decision.addWidget(self.same_button)
        self.different_button = QPushButton("Different studies — keep both")
        self.different_button.clicked.connect(lambda: self._decide(False))
        decision.addWidget(self.different_button)
        right_layout.addLayout(decision)

        splitter.addWidget(right)
        splitter.setSizes([280, 720])
        layout.addWidget(splitter, 1)
        return page

    def _build_merged_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.addWidget(
            section_label("Every merge, with the rule that produced it")
        )
        self.merged_table = SimpleTable(
            ["Kept record", "Kept from", "Merged record", "Merged from", "Rule", "Score"]
        )
        layout.addWidget(self.merged_table)
        return page

    # -- running -----------------------------------------------------------

    def _settings(self) -> DedupSettings:
        return DedupSettings(
            fuzzy_threshold=self.threshold.value(),
            auto_threshold=max(self.auto_threshold.value(), self.threshold.value()),
        )

    def _run(self) -> None:
        if self.project is None:
            return
        if self._task is not None and self._task.isRunning():
            return
        engine = DedupEngine(self.project, self._settings())
        self.run_button.setEnabled(False)
        self.summary_label.setText("Comparing records…")
        task = Task(engine.run)
        task.succeeded.connect(self._run_done)
        task.failed.connect(self._run_failed)
        task.finished.connect(lambda: self.run_button.setEnabled(True))
        self._task = task
        task.start()

    def _run_done(self, result) -> None:
        self.status_message.emit(
            f"Deduplication merged {result.n_merged} record(s); "
            f"{result.n_uncertain} pair(s) need review."
        )
        self.refresh()
        self.data_changed.emit()

    def _run_failed(self, message: str) -> None:
        self.summary_label.setText("Deduplication failed.")
        QMessageBox.critical(self, "Deduplication failed", message)

    # -- review ------------------------------------------------------------

    def _decide(self, is_duplicate: bool) -> None:
        if self.project is None:
            return
        row = self.pair_list.currentRow()
        if row < 0 or row >= len(self._pairs):
            return
        pair = self._pairs[row]
        engine = DedupEngine(self.project, self._settings())
        engine.decide_pair(
            pair["id"],
            is_duplicate,
            reason=self.reason_edit.text().strip(),
        )
        self.reason_edit.clear()
        self.status_message.emit(
            f"Pair {pair['id']} recorded as "
            + ("the same study." if is_duplicate else "two different studies.")
        )
        self.refresh()
        self.data_changed.emit()

    def _render(self, record: dict) -> str:
        def esc(value: object) -> str:
            return html.escape("" if value is None else str(value))

        authors = "; ".join(record.get("authors", []))
        volume, issue = record.get("volume", ""), record.get("issue", "")
        volume_issue = f"{volume} ({issue})" if volume and issue else (volume or issue)
        rows = [
            ("Record", f"#{record.get('id')}"),
            ("Database", record.get("source_database")),
            ("Authors", authors),
            ("Year", record.get("year")),
            ("Journal", record.get("journal")),
            ("Volume/issue", volume_issue or "—"),
            ("Pages", record.get("pages") or "—"),
            ("DOI", record.get("doi") or "—"),
            ("Type", record.get("doc_type")),
            ("Accession", record.get("accession") or "—"),
        ]
        body = "".join(
            f"<tr><td style='color:#5b6677;padding-right:10px;white-space:nowrap'>{esc(k)}</td>"
            f"<td>{esc(v)}</td></tr>"
            for k, v in rows
        )
        abstract = esc(record.get("abstract") or "No abstract in this record.")
        return (
            f"<h3 style='margin:0 0 8px 0'>{esc(record.get('title'))}</h3>"
            f"<table style='font-size:12px'>{body}</table>"
            f"<p style='margin-top:10px;font-size:12px'>{abstract}</p>"
        )

    def _show_pair(self, row: int) -> None:
        if row < 0 or row >= len(self._pairs):
            self.left_view.clear()
            self.right_view.clear()
            self.evidence_label.clear()
            return
        pair = self._pairs[row]
        self.left_view.setHtml(self._render(pair["a"]))
        self.right_view.setHtml(self._render(pair["b"]))
        evidence = pair.get("evidence", {})
        reason = evidence.get("reason", "")
        detail = ", ".join(
            f"{k}={v}" for k, v in evidence.items() if k != "reason"
        )
        self.evidence_label.setText(
            f"Rule: {pair['rule']}, score {pair['score']:.3f}"
            + (f" — {reason}" if reason else "")
            + (f"\n{detail}" if detail else "")
        )

    # -- display -----------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            self._pairs = []
            self.pair_list.clear()
            self.merged_table.fill([])
            self.summary_label.setText("Not run yet.")
            return

        engine = DedupEngine(self.project, self._settings())
        summary = engine.summary()
        run = summary["last_run"]
        if run:
            self.threshold.blockSignals(True)
            self.threshold.setValue(run["fuzzy_threshold"])
            self.auto_threshold.setValue(run["auto_threshold"])
            self.threshold.blockSignals(False)
            self.summary_label.setText(
                f"{summary['n_identified']} identified · "
                f"{summary['n_duplicates_removed']} duplicates removed · "
                f"{summary['n_screened']} to screen · "
                f"{summary['n_uncertain']} awaiting review"
            )
        else:
            self.summary_label.setText(
                f"{summary['n_identified']} record(s) imported. Not deduplicated yet."
            )

        self._pairs = engine.pending_pairs()
        self.pair_list.clear()
        for pair in self._pairs:
            item = QListWidgetItem(
                f"#{pair['id']}  {pair['rule']}  {pair['score']:.2f}\n"
                f"{pair['a'].get('title', '')[:60]}"
            )
            self.pair_list.addItem(item)
        has_pairs = bool(self._pairs)
        for button in (self.same_button, self.different_button):
            button.setEnabled(has_pairs)
        if has_pairs:
            self.pair_list.setCurrentRow(0)
        else:
            self._show_pair(-1)

        self.merged_table.fill(
            [
                [
                    f"#{m['kept_record_id']} {m['kept_title'][:60]}",
                    m["kept_db"],
                    f"#{m['merged_record_id']} {m['merged_title'][:60]}",
                    m["merged_db"],
                    m["rule"],
                    f"{m['score']:.3f}",
                ]
                for m in engine.merged_groups()
            ]
        )
