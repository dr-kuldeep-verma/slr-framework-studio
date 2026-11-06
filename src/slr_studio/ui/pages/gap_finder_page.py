"""Stage 7 of the build: finding, evidencing and ranking the gaps.

Two modes, kept on separate tabs because they are not the same kind of claim.
The quick scan is a scoping view over the imported records and says so on the
page; the full analysis is built from coded evidence and every statement it
produces is qualified to this corpus and its search date.

The page's first job is to make the search date impossible to overlook:
without it the whole stage refuses to run, and a reviewer who does not know
why would reasonably think the application was broken.
"""

from __future__ import annotations

import html
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
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
from ...exports import (
    write_future_research_draft,
    write_gap_register,
    write_research_agenda,
)
from ...gaps import GapEngine, GapError, GapSettings, Weights
from ..dialogs import confirm_cost
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["GapFinderPage"]

_DIMENSIONS = (
    "country", "region", "income_group", "setting", "population", "activity",
)


class GapFinderPage(StagePage):
    title = "Gap Finder"
    subtitle = (
        "Every absence is an absence within this corpus, as searched to a "
        "date — and every gap carries the counts behind it."
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._gaps: list[dict] = []
        self._topics: list[dict] = []

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self._layout.addWidget(self.summary)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("blockerLabel")
        self.notice.hide()
        self._layout.addWidget(self.notice)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scan_tab(), "1. Quick scan")
        self.tabs.addTab(self._register_tab(), "2. Gap register")
        self.tabs.addTab(self._map_tab(), "3. Evidence gap map")
        self._layout.addWidget(self.tabs, 1)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

    def requires_project(self) -> bool:
        return True

    # -- tabs --------------------------------------------------------------

    def _scan_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        blurb = QLabel(
            "A scoping view over the imported records, before screening. It "
            "clusters titles, abstracts and keywords into topics so you can "
            "see roughly what the corpus covers while the objectives are "
            "still being refined. It is not a finding, and nothing later in "
            "the review cites it."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.scan_button = QPushButton("Run the quick scan")
        self.scan_button.clicked.connect(self._quick_scan)
        row.addWidget(self.scan_button)
        row.addStretch(1)
        layout.addLayout(row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.topic_table = SimpleTable(
            ["Topic", "Records", "First", "Last"], stretch_columns=[0]
        )
        self.topic_table.itemSelectionChanged.connect(self._show_topic)
        splitter.addWidget(self.topic_table)
        self.topic_detail = QTextBrowser()
        splitter.addWidget(self.topic_detail)
        splitter.setSizes([460, 420])
        layout.addWidget(splitter, 1)
        return page

    def _register_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        blurb = QLabel(
            "Built from the coded evidence. Each gap carries the counts and "
            "the studies behind it; a candidate without them is not recorded "
            "at all."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.find_button = QPushButton("Find the gaps")
        self.find_button.clicked.connect(self._find_gaps)
        self.freshness_button = QPushButton("Check for recent work...")
        self.freshness_button.clicked.connect(self._check_freshness)
        self.studies_button = QPushButton("Draft studies")
        self.studies_button.clicked.connect(self._draft_studies)
        self.accept_button = QPushButton("Accept")
        self.accept_button.clicked.connect(lambda: self._decide("accepted"))
        self.reject_button = QPushButton("Reject...")
        self.reject_button.clicked.connect(lambda: self._decide("rejected"))
        self.rescore_button = QPushButton("Change scores...")
        self.rescore_button.clicked.connect(self._override_scores)
        self.export_button = QPushButton("Export register and agenda")
        self.export_button.clicked.connect(self._export)
        for button in (
            self.find_button, self.freshness_button, self.studies_button,
            self.accept_button, self.reject_button, self.rescore_button,
            self.export_button,
        ):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        weights = QHBoxLayout()
        weights.addWidget(QLabel("Weights:"))
        self.weight_boxes: dict[str, QDoubleSpinBox] = {}
        for name in ("centrality", "size", "relevance", "feasibility"):
            box = QDoubleSpinBox()
            box.setRange(0.0, 9.0)
            box.setSingleStep(0.5)
            box.setValue(1.0)
            box.setPrefix(f"{name[:4]} ")
            self.weight_boxes[name] = box
            weights.addWidget(box)
        apply_weights = QPushButton("Apply")
        apply_weights.clicked.connect(self._apply_weights)
        weights.addWidget(apply_weights)
        weights.addStretch(1)
        layout.addLayout(weights)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.gap_table = SimpleTable(
            ["Priority", "Type", "Statement", "Freshness", "Status"],
            stretch_columns=[2],
        )
        self.gap_table.itemSelectionChanged.connect(self._show_gap)
        splitter.addWidget(self.gap_table)
        self.gap_detail = QTextBrowser()
        splitter.addWidget(self.gap_detail)
        splitter.setSizes([340, 280])
        layout.addWidget(splitter, 1)
        return page

    def _map_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        blurb = QLabel(
            "Constructs against where and whom the studies covered, counted "
            "in datasets and coloured by the best grade in each cell. An "
            "empty cell is hatched rather than pale, so that none cannot be "
            "mistaken for a little. Clicking a cell lists its studies."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        row.addWidget(QLabel("Cross constructs against:"))
        self.dimension_box = QComboBox()
        self.dimension_box.addItems(_DIMENSIONS)
        self.dimension_box.currentTextChanged.connect(lambda _: self._draw_map())
        row.addWidget(self.dimension_box)
        self.write_map_button = QPushButton("Write the figure")
        self.write_map_button.clicked.connect(self._write_map)
        row.addWidget(self.write_map_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.map_table = SimpleTable(["Construct"], stretch_columns=[0])
        self.map_table.itemSelectionChanged.connect(self._show_cell)
        layout.addWidget(self.map_table, 1)
        layout.addWidget(section_label("The studies in the selected cell"))
        self.cell_detail = QTextBrowser()
        self.cell_detail.setMaximumHeight(130)
        layout.addWidget(self.cell_detail)
        return page

    # -- plumbing ------------------------------------------------------------

    def _engine(self) -> GapEngine:
        settings = load_settings(self.project)
        client = AIClient(
            self.project,
            provider_from_settings(settings),
            stage="gaps",
            use_cache=settings.use_cache,
        )
        return GapEngine(
            self.project,
            client,
            GapSettings(
                budget_usd=settings.budget_usd,
                weights=Weights(
                    **{
                        name: box.value()
                        for name, box in self.weight_boxes.items()
                    }
                ),
            ),
        )

    def _busy(self, busy: bool) -> None:
        for button in (
            self.scan_button, self.find_button, self.freshness_button,
            self.studies_button,
        ):
            button.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _run_step(self, call, *, title: str, estimate_step: str | None = None,
                  **kwargs) -> None:
        if self.project is None:
            return
        if estimate_step is not None:
            try:
                estimate = self._engine().estimate(estimate_step)
            except GapError as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            if estimate.n_calls and not confirm_cost(estimate, title, self):
                return
        if self._task is not None and self._task.isRunning():
            return
        self._busy(True)
        task = Task(call, **kwargs)
        task.succeeded.connect(self._step_done)
        task.failed.connect(self._step_failed)
        task.finished.connect(lambda: self._busy(False))
        self._task = task
        task.start()

    def _step_done(self, outcome) -> None:
        self.progress.hide()
        self.status_message.emit(outcome.describe().splitlines()[0])
        for message in outcome.messages[:3]:
            QMessageBox.information(self, "Gap Finder", message)
        self.refresh()
        self.data_changed.emit()

    def _step_failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Gap Finder stopped", message)

    # -- actions ---------------------------------------------------------------

    def _quick_scan(self) -> None:
        self._run_step(self._engine().quick_scan, title="Quick scan")

    def _find_gaps(self) -> None:
        self._run_step(self._engine().find_gaps, title="Find the gaps")

    def _draft_studies(self) -> None:
        self._run_step(
            self._engine().draft_studies, title="Draft studies",
            estimate_step="studies", top=5,
        )

    def _check_freshness(self) -> None:
        answer = QMessageBox.question(
            self,
            "Check for recent work",
            "This asks OpenAlex and Crossref whether anything published after "
            "your search date matches each gap. It is the only step in this "
            "stage that reaches the internet, and it sends the gap's "
            "construct names and your contact email. Continue?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._run_step(
            self._engine().check_freshness, title="Check for recent work",
            top=20,
        )

    def _selected_gap(self) -> dict | None:
        row = self.gap_table.currentRow()
        if row < 0 or row >= len(self._gaps):
            return None
        return self._gaps[row]

    def _decide(self, status: str) -> None:
        gap = self._selected_gap()
        if gap is None or self.project is None:
            return
        reason = ""
        if status == "rejected":
            reason, ok = QInputDialog.getText(
                self, "Reject this gap", "Why is it not a gap?"
            )
            if not ok:
                return
        self._engine().decide_gap(
            gap["id"], status=status, reason=reason, actor="human:ui"
        )
        self.refresh()
        self.data_changed.emit()

    def _override_scores(self) -> None:
        gap = self._selected_gap()
        if gap is None or self.project is None:
            return
        criterion, ok = QInputDialog.getItem(
            self, "Change a score", "Which criterion?",
            ["centrality", "size", "relevance", "feasibility"], 0, False,
        )
        if not ok:
            return
        value, ok = QInputDialog.getDouble(
            self, "Change a score", f"{criterion} (0 to 1):",
            gap[f"score_{criterion}"] or 0.0, 0.0, 1.0, 2,
        )
        if not ok:
            return
        reason, ok = QInputDialog.getText(
            self, "Change a score", "Why? (recorded in the audit log)"
        )
        if not ok:
            return
        self._engine().override_scores(
            gap["id"], {criterion: value}, reason=reason, actor="human:ui"
        )
        self.refresh()
        self.data_changed.emit()

    def _apply_weights(self) -> None:
        if self.project is None:
            return
        self._engine().set_weights(
            {name: box.value() for name, box in self.weight_boxes.items()},
            actor="human:ui",
        )
        self.refresh()

    def _export(self) -> None:
        if self.project is None:
            return
        folder = self.project.exports_dir
        written = [
            write_gap_register(self.project, folder / "gap-register.xlsx"),
            write_research_agenda(self.project, folder / "research-agenda.docx"),
            write_future_research_draft(
                self.project, folder / "future-research-draft.md"
            ),
        ]
        QMessageBox.information(
            self, "Exported",
            "Written:\n" + "\n".join(path.name for path in written),
        )

    def _write_map(self) -> None:
        if self.project is None:
            return
        written = self._engine().write_gap_map(
            actor="human:ui", dimension=self.dimension_box.currentText()
        )
        QMessageBox.information(
            self, "Evidence gap map",
            "Written:\n" + "\n".join(path.name for path in written),
        )

    # -- detail panels -----------------------------------------------------------

    def _show_topic(self) -> None:
        row = self.topic_table.currentRow()
        if row < 0 or row >= len(self._topics):
            self.topic_detail.clear()
            return
        topic = self._topics[row]
        trend = topic.get("trend") or {}
        rows = "".join(
            f"<tr><td>{year}</td><td>{count}</td></tr>"
            for year, count in sorted(trend.items())
        )
        self.topic_detail.setHtml(
            f"<h3>{html.escape(topic['label'])}</h3>"
            f"<p>{topic['n_records']} record(s). Terms: "
            f"{html.escape(', '.join(topic.get('terms') or []))}</p>"
            f"<p><b>Records per year</b></p><table>{rows}</table>"
        )

    def _show_gap(self) -> None:
        gap = self._selected_gap()
        if gap is None or self.project is None:
            self.gap_detail.clear()
            return
        try:
            counts = json.loads(gap["supporting_counts"] or "{}")
        except (TypeError, ValueError):
            counts = {}
        evidence = self._engine().gap_evidence(gap["id"])
        parts = [f"<p><b>{html.escape(gap['statement'])}</b></p>"]
        parts.append("<p><b>Supporting counts</b></p><table cellpadding='3'>")
        for key, value in sorted(counts.items()):
            parts.append(
                f"<tr><td><b>{html.escape(str(key))}</b></td>"
                f"<td>{html.escape(str(value))}</td></tr>"
            )
        parts.append("</table>")
        if evidence:
            parts.append("<p><b>Studies cited</b></p><ul>")
            for item in evidence[:20]:
                label = item["title"] or item["label"] or item["record_id"]
                parts.append(f"<li>{html.escape(str(label))}</li>")
            parts.append("</ul>")
        if gap["freshness_status"] == "possibly_addressed":
            parts.append(
                "<p style='color:#7a5c13'><b>Work published after the search "
                "date may already address this.</b> The gap still stands — "
                "this corpus has the hole — but read the papers before "
                "reporting it.</p>"
            )
        if gap["suggested_question"]:
            parts.append(
                "<p><b>Suggested study (drafted, unchecked)</b></p>"
                f"<p>{html.escape(gap['suggested_question'])}<br>"
                f"{html.escape(gap['suggested_hypothesis'])}<br>"
                f"{html.escape(gap['suggested_design'])}</p>"
            )
        if gap["override_reason"]:
            parts.append(
                f"<p><i>Scores changed by {html.escape(gap['overridden_by'])}: "
                f"{html.escape(gap['override_reason'])}</i></p>"
            )
        self.gap_detail.setHtml("".join(parts))

    def _show_cell(self) -> None:
        if self.project is None:
            return
        row = self.map_table.currentRow()
        column = self.map_table.currentColumn()
        if row < 0 or column < 1:
            return
        grid = self._engine().gap_map(dimension=self.dimension_box.currentText())
        if row >= len(grid.rows) or column - 1 >= len(grid.columns):
            return
        construct = grid.rows[row]
        value = grid.columns[column - 1]
        cell = grid.cell(construct, value)
        if not cell.record_ids:
            self.cell_detail.setHtml(
                f"<p>No dataset studies {html.escape(construct)} in "
                f"{html.escape(value)}. That is the gap.</p>"
            )
            return
        titles = [
            row["title"]
            for row in self.project.db.query(
                "SELECT title FROM record WHERE id IN "
                f"({','.join('?' * len(cell.record_ids))})",
                tuple(cell.record_ids),
            )
        ]
        listed = "".join(f"<li>{html.escape(t or '')}</li>" for t in titles)
        self.cell_detail.setHtml(
            f"<p>{cell.n_datasets} dataset(s), best grade "
            f"{html.escape(cell.best_grade or '-')}</p><ul>{listed}</ul>"
        )

    def _draw_map(self) -> None:
        if self.project is None:
            return
        try:
            grid = self._engine().gap_map(
                dimension=self.dimension_box.currentText()
            )
        except GapError:
            return
        self.map_table.setColumnCount(len(grid.columns) + 1)
        self.map_table.setHorizontalHeaderLabels(["Construct", *grid.columns])
        self.map_table.fill(
            [
                [row, *[str(grid.cell(row, c).n_datasets) for c in grid.columns]]
                for row in grid.rows
            ]
        )

    # -- refresh -------------------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            return
        engine = self._engine()
        counts = engine.counts()
        searched_to = (self.project.info().get("searched_to") or "").strip()
        self.summary.setText(
            f"{counts['n_gaps']} gap(s) in the register · "
            f"{counts['n_with_study']} with a drafted study · "
            f"{counts['n_possibly_addressed']} possibly being addressed · "
            f"{counts['n_topics']} quick-scan topic(s) · "
            + (
                f"corpus searched to {searched_to}."
                if searched_to
                else "no search date recorded."
            )
        )

        if not searched_to:
            self.notice.setText(
                "This project does not record the date the searches were run. "
                "Every gap is an absence within the included corpus as "
                "searched to a date, so the full analysis will not run until "
                "you set it on the Project page. The quick scan does not need "
                "it."
            )
            self.notice.show()
        else:
            self.notice.hide()

        scan = engine.scan_results()
        self._topics = scan["topics"]
        self.topic_table.fill(
            [
                [
                    topic["label"],
                    str(topic["n_records"]),
                    str(min(topic["trend"]) if topic["trend"] else "-"),
                    str(max(topic["trend"]) if topic["trend"] else "-"),
                ]
                for topic in self._topics
            ]
        )

        self._gaps = engine.ranked()
        self.gap_table.fill(
            [
                [
                    f"{gap['priority']:.2f}" if gap["priority"] else "-",
                    gap["gap_type"].replace("_", " "),
                    gap["statement"],
                    gap["freshness_status"].replace("_", " "),
                    gap["status"],
                ]
                for gap in self._gaps
            ]
        )
        self._draw_map()
