"""Stage 6 of the build: the figures and the propositions.

Three tabs, in the order the work happens: group the constructs into themes,
draft the propositions, draw the figures. The page's job beyond that is to
make two things impossible to miss — which propositions the model put forward
and the application rejected because they cited evidence that is not there,
and which graded links appear in no figure because their constructs ended up
in different themes.
"""

from __future__ import annotations

import html
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
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
from ...framework import FrameworkEngine, FrameworkError, FrameworkSettings
from ..dialogs import confirm_cost
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["FrameworkPage"]


class FrameworkPage(StagePage):
    title = "Framework"
    subtitle = (
        "Figures drawn from graded links only, and propositions that may cite "
        "nothing else."
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._themes: list[dict] = []
        self._propositions: list[dict] = []
        self._figures: list[dict] = []

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self._layout.addWidget(self.summary)

        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setObjectName("blockerLabel")
        self.notice.hide()
        self._layout.addWidget(self.notice)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._themes_tab(), "1. Themes")
        self.tabs.addTab(self._propositions_tab(), "2. Propositions")
        self.tabs.addTab(self._figures_tab(), "3. Figures")
        self._layout.addWidget(self.tabs, 1)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

    def requires_project(self) -> bool:
        return True

    # -- tabs --------------------------------------------------------------

    def _themes_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        blurb = QLabel(
            "One evidence map is drawn per theme. A theme you reject gets no "
            "figure; a link whose two constructs sit in different themes "
            "appears in none of them, and is listed on the Figures tab."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.propose_themes_button = QPushButton("Propose themes")
        self.propose_themes_button.clicked.connect(self._propose_themes)
        self.approve_theme_button = QPushButton("Approve")
        self.approve_theme_button.clicked.connect(
            lambda: self._decide_theme(True)
        )
        self.reject_theme_button = QPushButton("Reject...")
        self.reject_theme_button.clicked.connect(
            lambda: self._decide_theme(False)
        )
        self.rename_theme_button = QPushButton("Rename...")
        self.rename_theme_button.clicked.connect(self._rename_theme)
        self.move_button = QPushButton("Move construct...")
        self.move_button.clicked.connect(self._move_construct)
        for button in (
            self.propose_themes_button, self.approve_theme_button,
            self.reject_theme_button, self.rename_theme_button, self.move_button,
        ):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.theme_table = SimpleTable(
            ["Theme", "Status", "Constructs", "Description"],
            stretch_columns=[3],
        )
        self.theme_table.itemSelectionChanged.connect(self._show_theme)
        splitter.addWidget(self.theme_table)
        self.theme_detail = QTextBrowser()
        splitter.addWidget(self.theme_detail)
        splitter.setSizes([520, 330])
        layout.addWidget(splitter, 1)
        return page

    def _propositions_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        blurb = QLabel(
            "Every proposition must cite links from the graded inventory by "
            "id. One that cites anything else is rejected whole, not trimmed, "
            "and is shown here with the reason."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.draft_button = QPushButton("Draft propositions")
        self.draft_button.clicked.connect(self._draft)
        self.accept_button = QPushButton("Accept")
        self.accept_button.clicked.connect(lambda: self._decide_proposition(True))
        self.reject_button = QPushButton("Reject...")
        self.reject_button.clicked.connect(lambda: self._decide_proposition(False))
        self.edit_button = QPushButton("Edit statement...")
        self.edit_button.clicked.connect(self._edit_proposition)
        for button in (
            self.draft_button, self.accept_button, self.reject_button,
            self.edit_button,
        ):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        splitter = QSplitter(Qt.Orientation.Vertical)
        self.proposition_table = SimpleTable(
            ["#", "Statement", "Kind", "Cites", "Status"], stretch_columns=[1]
        )
        self.proposition_table.itemSelectionChanged.connect(
            self._show_proposition
        )
        splitter.addWidget(self.proposition_table)
        self.proposition_detail = QTextBrowser()
        splitter.addWidget(self.proposition_detail)
        splitter.setSizes([320, 280])
        layout.addWidget(splitter, 1)
        return page

    def _figures_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        blurb = QLabel(
            "Thick for strong, solid for moderate, dashed for emerging, a "
            "head at each end where the association has no direction. Every "
            "arrow carries its link id. Each figure is written three ways: "
            "SVG to look at, PNG to paste, and PowerPoint you can edit."
        )
        blurb.setWordWrap(True)
        layout.addWidget(blurb)

        row = QHBoxLayout()
        self.maps_button = QPushButton("Draw the evidence maps")
        self.maps_button.clicked.connect(self._draw_maps)
        self.integrated_button = QPushButton("Draw the integrated diagram")
        self.integrated_button.clicked.connect(self._draw_integrated)
        self.open_button = QPushButton("Open the figures folder")
        self.open_button.clicked.connect(self._open_folder)
        for button in (
            self.maps_button, self.integrated_button, self.open_button
        ):
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        self.figure_table = SimpleTable(
            ["Figure", "Kind", "Format", "Constructs", "Links", "Crossings"],
            stretch_columns=[0],
        )
        layout.addWidget(self.figure_table, 1)

        self.not_drawn = QTextBrowser()
        self.not_drawn.setMaximumHeight(120)
        layout.addWidget(section_label("Links no evidence map shows"))
        layout.addWidget(self.not_drawn)
        return page

    # -- plumbing ----------------------------------------------------------

    def _engine(self) -> FrameworkEngine:
        settings = load_settings(self.project)
        client = AIClient(
            self.project,
            provider_from_settings(settings),
            stage="framework",
            use_cache=settings.use_cache,
        )
        return FrameworkEngine(
            self.project, client, FrameworkSettings(budget_usd=settings.budget_usd)
        )

    def _busy(self, busy: bool) -> None:
        for button in (
            self.propose_themes_button, self.draft_button, self.maps_button,
            self.integrated_button,
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
            except FrameworkError as exc:
                QMessageBox.warning(self, title, str(exc))
                return
            except Exception as exc:  # pragma: no cover - provider trouble
                QMessageBox.critical(self, title, str(exc))
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
        summary = ", ".join(
            f"{k.replace('n_', '')} {v}" for k, v in (outcome.counts or {}).items()
        )
        self.status_message.emit(
            f"{outcome.step}: {summary}" if summary else f"{outcome.step} finished"
        )
        for message in outcome.messages:
            QMessageBox.information(self, "Framework", message)
        self.refresh()
        self.data_changed.emit()

    def _step_failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Framework stopped", message)

    # -- themes ------------------------------------------------------------

    def _propose_themes(self) -> None:
        self._run_step(
            self._engine().propose_themes, title="Propose themes",
            estimate_step="themes",
        )

    def _selected_theme(self) -> dict | None:
        row = self.theme_table.currentRow()
        if row < 0 or row >= len(self._themes):
            return None
        return self._themes[row]

    def _decide_theme(self, approved: bool) -> None:
        theme = self._selected_theme()
        if theme is None or self.project is None:
            return
        reason = ""
        if not approved:
            reason, ok = QInputDialog.getText(
                self, "Reject this theme", "Why is it not a theme?"
            )
            if not ok:
                return
        self._engine().decide_theme(
            theme["id"], approved=approved, reason=reason, actor="human:ui"
        )
        self.refresh()
        self.data_changed.emit()

    def _rename_theme(self) -> None:
        theme = self._selected_theme()
        if theme is None or self.project is None:
            return
        name, ok = QInputDialog.getText(
            self, "Rename the theme", "Name:", text=theme["name"]
        )
        if ok and name.strip():
            self._engine().rename_theme(
                theme["id"], name.strip(), actor="human:ui"
            )
            self.refresh()

    def _move_construct(self) -> None:
        theme = self._selected_theme()
        if theme is None or self.project is None:
            return
        engine = self._engine()
        others = [t for t in engine.themes() if t["id"] != theme["id"]]
        if not others:
            QMessageBox.information(
                self, "Move a construct", "There is only one theme."
            )
            return
        constructs = engine.theme_constructs(theme["id"])
        if not constructs:
            return
        which, ok = QInputDialog.getItem(
            self, "Move a construct", "Construct:",
            [c["name"] for c in constructs], 0, False,
        )
        if not ok:
            return
        target, ok = QInputDialog.getItem(
            self, "Move a construct", "Into which theme?",
            [t["name"] for t in others], 0, False,
        )
        if not ok:
            return
        construct = next(c for c in constructs if c["name"] == which)
        destination = next(t for t in others if t["name"] == target)
        engine.move_construct(
            construct["id"], destination["id"], actor="human:ui"
        )
        self.refresh()
        self.data_changed.emit()

    def _show_theme(self) -> None:
        theme = self._selected_theme()
        if theme is None or self.project is None:
            self.theme_detail.clear()
            return
        constructs = self._engine().theme_constructs(theme["id"])
        parts = [f"<h3>{html.escape(theme['name'])}</h3>"]
        if theme["description"]:
            parts.append(f"<p>{html.escape(theme['description'])}</p>")
        parts.append("<p><b>Constructs</b></p><ul>")
        for construct in constructs:
            parts.append(f"<li>{html.escape(construct['name'])}</li>")
        parts.append("</ul>")
        if theme["status_reason"]:
            parts.append(
                f"<p><i>{html.escape(theme['status'])}: "
                f"{html.escape(theme['status_reason'])}</i></p>"
            )
        self.theme_detail.setHtml("".join(parts))

    # -- propositions --------------------------------------------------------

    def _draft(self) -> None:
        self._run_step(
            self._engine().draft_propositions, title="Draft propositions",
            estimate_step="propositions",
        )

    def _selected_proposition(self) -> dict | None:
        row = self.proposition_table.currentRow()
        if row < 0 or row >= len(self._propositions):
            return None
        return self._propositions[row]

    def _decide_proposition(self, accepted: bool) -> None:
        proposition = self._selected_proposition()
        if proposition is None or self.project is None:
            return
        reason = ""
        if not accepted:
            reason, ok = QInputDialog.getText(
                self, "Reject this proposition", "Why?"
            )
            if not ok:
                return
        self._engine().decide_proposition(
            proposition["id"], accepted=accepted, reason=reason, actor="human:ui"
        )
        self.refresh()
        self.data_changed.emit()

    def _edit_proposition(self) -> None:
        proposition = self._selected_proposition()
        if proposition is None or self.project is None:
            return
        statement, ok = QInputDialog.getMultiLineText(
            self, "Edit the statement", "Statement:", proposition["statement"]
        )
        if ok and statement.strip():
            self._engine().edit_proposition(
                proposition["id"], statement=statement.strip(), actor="human:ui"
            )
            self.refresh()
            self.data_changed.emit()

    def _show_proposition(self) -> None:
        proposition = self._selected_proposition()
        if proposition is None:
            self.proposition_detail.clear()
            return
        try:
            theories = json.loads(proposition["theories"] or "[]")
        except (TypeError, ValueError):
            theories = []
        rows = [
            ("Mechanism", proposition["mechanism"]),
            ("Theories", "; ".join(str(t) for t in theories)),
            ("Moderators", proposition["moderators"]),
            ("Boundary conditions", proposition["boundary_conditions"]),
            ("Falsifying test", proposition["falsifying_test"]),
            ("Cites", proposition["cited_links"] or "nothing"),
            ("Kind", proposition["element_kind"].replace("_", " ")),
        ]
        parts = [f"<p><b>{html.escape(proposition['statement'])}</b></p>"]
        if proposition["status"] == "rejected" and proposition["status_reason"]:
            parts.append(
                "<p style='color:#8a1f1f'><b>"
                f"{html.escape(proposition['status_reason'])}</b></p>"
            )
        parts.append("<table cellpadding='3'>")
        for label, value in rows:
            if not value:
                continue
            parts.append(
                f"<tr><td valign='top'><b>{label}</b></td>"
                f"<td>{html.escape(str(value))}</td></tr>"
            )
        parts.append("</table>")
        if proposition["edited_from"]:
            parts.append(
                "<p><i>Edited by a reviewer. What the model wrote:</i></p>"
                f"<pre>{html.escape(proposition['edited_from'])}</pre>"
            )
        self.proposition_detail.setHtml("".join(parts))

    # -- figures -------------------------------------------------------------

    def _draw_maps(self) -> None:
        self._run_step(
            self._engine().draw_evidence_maps, title="Draw the evidence maps"
        )

    def _draw_integrated(self) -> None:
        self._run_step(
            self._engine().draw_integrated_diagram,
            title="Draw the integrated diagram",
        )

    def _open_folder(self) -> None:
        if self.project is None:
            return
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        folder = self.project.exports_dir / "figures"
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # -- refresh ---------------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            return
        engine = self._engine()
        counts = engine.counts()
        self.summary.setText(
            f"{counts.get('n_themes', 0)} theme(s), "
            f"{counts.get('n_themes_approved', 0)} approved · "
            f"{counts.get('n_propositions', 0)} proposition(s), "
            f"{counts.get('n_accepted', 0)} accepted, "
            f"{counts.get('n_rejected', 0)} rejected · "
            f"{counts.get('n_evidenced', 0)} evidenced link(s), "
            f"{counts.get('n_mechanisms', 0)} proposed mechanism(s), "
            f"{counts.get('n_untested', 0)} untested pathway(s) · "
            f"{counts.get('n_figures', 0)} figure file(s)."
        )

        notices = []
        if not engine.inventory():
            notices.append(
                "There is no graded inventory yet. The framework may only "
                "cite graded links, so finish the Synthesise stage first."
            )
        rejected = counts.get("n_rejected", 0)
        if rejected:
            notices.append(
                f"{rejected} proposition(s) were rejected because they cited "
                "links that are not in the graded inventory, or claimed "
                "evidence without citing any. They are listed on the "
                "Propositions tab with the reason."
            )
        if notices:
            self.notice.setText(" ".join(notices))
            self.notice.show()
        else:
            self.notice.hide()

        self._themes = engine.themes()
        self.theme_table.fill(
            [
                [
                    theme["name"],
                    theme["status"],
                    str(theme["n_constructs"]),
                    theme["description"],
                ]
                for theme in self._themes
            ]
        )

        self._propositions = engine.propositions()
        self.proposition_table.fill(
            [
                [
                    str(proposition["number"] or "-"),
                    proposition["statement"],
                    proposition["element_kind"].replace("_", " "),
                    proposition["cited_links"] or "-",
                    proposition["status"],
                ]
                for proposition in self._propositions
            ]
        )

        self._figures = engine.figures()
        self.figure_table.fill(
            [
                [
                    figure["filename"],
                    figure["kind"].replace("_", " "),
                    figure["format"],
                    str(figure["n_constructs"]),
                    str(figure["n_links"]),
                    str(figure["n_crossings"]),
                ]
                for figure in self._figures
            ]
        )

        missing = engine.links_not_drawn() if self._themes else []
        if missing:
            rows = "".join(
                f"<li>L{link['id']}: {html.escape(link['predictor_name'])} → "
                f"{html.escape(link['outcome_name'])} ({link['grade']})</li>"
                for link in missing
            )
            self.not_drawn.setHtml(
                "<p>These graded links are in no evidence map, because their "
                "two constructs are in different themes. The integrated "
                "diagram shows them regardless.</p>"
                f"<ul>{rows}</ul>"
            )
        else:
            self.not_drawn.setHtml(
                "<p>Every graded link appears in an evidence map.</p>"
                if self._themes
                else "<p>Propose the themes first.</p>"
            )
