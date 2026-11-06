"""Stage 5 of the build: turning extracted labels into graded evidence.

The page is organised as the seven steps in the order they must happen,
because the order is the substance here rather than a convenience: a
codebook approved after mapping, or a sign convention fixed after counting,
would quietly change what the review concludes.

Each tab therefore says what it is waiting for, and the buttons that cannot
yet be pressed say why rather than simply greying out.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
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
from ...exports import write_graded_inventory
from ...synthesis import SynthesisEngine, SynthesisError, SynthesisSettings
from ..dialogs import confirm_cost
from ..widgets import SimpleTable, section_label
from ..workers import Task
from .base import StagePage

__all__ = ["SynthesisePage"]

_GRADE_ORDER = ("strong", "moderate", "emerging", "contested", "tentative", "asserted")


class SynthesisePage(StagePage):
    title = "Synthesise"
    subtitle = (
        "One vocabulary, one sign convention, one vote per dataset — and the "
        "graded inventory everything after this may cite."
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._task: Task | None = None
        self._constructs: list[dict] = []
        self._contestable: list[dict] = []
        self._links: list[dict] = []
        self._graded: list[dict] = []

        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self._layout.addWidget(self.status_label)

        self.blocker_label = QLabel()
        self.blocker_label.setWordWrap(True)
        self.blocker_label.setObjectName("blockerLabel")
        self.blocker_label.hide()
        self._layout.addWidget(self.blocker_label)

        self.progress = QProgressBar()
        self.progress.hide()
        self._layout.addWidget(self.progress)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_codebook_tab(), "1. Codebook")
        self.tabs.addTab(self._build_mapping_tab(), "2. Mapping")
        self.tabs.addTab(self._build_orientation_tab(), "3. Orientation")
        self.tabs.addTab(self._build_datasets_tab(), "4. Datasets")
        self.tabs.addTab(self._build_inventory_tab(), "5. Graded inventory")
        self.tabs.addTab(self._build_sensitivity_tab(), "6. Sensitivity")
        self.tabs.addTab(self._build_contradictions_tab(), "7. Contradictions")
        self._layout.addWidget(self.tabs, 1)

    # -- tabs --------------------------------------------------------------

    def _build_codebook_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "A construct is a thing the studies measured, not a word they "
                "used. Nothing is mapped until you approve the codebook, "
                "because an unread codebook would make the model's vocabulary "
                "the review's."
            )
        )
        # What "Propose a codebook" does depends on the approach recorded in
        # the protocol, and the reviewer pressing the button is entitled to
        # know which of the two they are about to get.
        self.approach_label = _hint("")
        layout.addWidget(self.approach_label)
        row = QHBoxLayout()
        self.propose_button = QPushButton("Propose a codebook")
        self.propose_button.clicked.connect(self._propose)
        row.addWidget(self.propose_button)
        self.add_construct_button = QPushButton("Add a construct…")
        self.add_construct_button.clicked.connect(self._add_construct)
        row.addWidget(self.add_construct_button)
        self.edit_construct_button = QPushButton("Edit definition…")
        self.edit_construct_button.clicked.connect(self._edit_construct)
        row.addWidget(self.edit_construct_button)
        self.reject_construct_button = QPushButton("Reject")
        self.reject_construct_button.clicked.connect(self._reject_construct)
        row.addWidget(self.reject_construct_button)
        row.addStretch(1)
        self.approve_button = QPushButton("Approve the codebook")
        self.approve_button.clicked.connect(self._approve)
        row.addWidget(self.approve_button)
        layout.addLayout(row)

        self.construct_table = SimpleTable(
            ["Construct", "Status", "Definition", "Includes", "Excludes",
             "Orientation"],
            stretch_columns=[2, 3, 4],
        )
        layout.addWidget(self.construct_table, 1)
        return page

    def _build_mapping_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "Each author label is placed against a construct by what was "
                "measured, not by the words. Doubtful placements are flagged "
                "for you; confirming or moving one is logged as an override."
            )
        )
        row = QHBoxLayout()
        self.map_button = QPushButton("Map the labels")
        self.map_button.clicked.connect(self._map)
        row.addWidget(self.map_button)
        row.addStretch(1)
        layout.addLayout(row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.contestable_list = QListWidget()
        self.contestable_list.setMaximumWidth(300)
        self.contestable_list.setWordWrap(True)
        self.contestable_list.currentRowChanged.connect(self._show_mapping)
        splitter.addWidget(self.contestable_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(12, 0, 0, 0)
        self.mapping_view = QTextBrowser()
        right_layout.addWidget(self.mapping_view, 1)

        decide = QHBoxLayout()
        decide.addWidget(QLabel("Move to"))
        self.mapping_combo = QComboBox()
        decide.addWidget(self.mapping_combo, 1)
        self.move_button = QPushButton("Move it…")
        self.move_button.clicked.connect(self._move_mapping)
        decide.addWidget(self.move_button)
        self.keep_button = QPushButton("Keep as it is")
        self.keep_button.clicked.connect(self._keep_mapping)
        decide.addWidget(self.keep_button)
        right_layout.addLayout(decide)
        splitter.addWidget(right)
        splitter.setSizes([300, 760])
        layout.addWidget(splitter, 1)

        layout.addWidget(section_label("Every mapping"))
        self.mapping_table = SimpleTable(
            ["Label", "Role", "Construct", "Measured as", "Uses", "Status"],
            stretch_columns=[0, 3],
        )
        self.mapping_table.fit_to_rows(8)
        layout.addWidget(self.mapping_table)
        return page

    def _build_orientation_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "Which pole of each construct counts as \"more\"? This is "
                "always your decision: it fixes the sign of every finding, so "
                "a model choosing it would be choosing what the review says. "
                "Once every construct has one, harmonisation re-expresses the "
                "findings against them and records each sign it flips."
            )
        )
        row = QHBoxLayout()
        self.orientation_button = QPushButton("Set orientation…")
        self.orientation_button.clicked.connect(self._set_orientation)
        row.addWidget(self.orientation_button)
        row.addStretch(1)
        self.harmonise_button = QPushButton("Harmonise the directions")
        self.harmonise_button.clicked.connect(self._harmonise)
        row.addWidget(self.harmonise_button)
        layout.addLayout(row)

        self.orientation_table = SimpleTable(
            ["Construct", "Reference orientation", "Set by"], stretch_columns=[1]
        )
        self.orientation_table.fit_to_rows(8)
        layout.addWidget(self.orientation_table)

        layout.addWidget(section_label("Signs that were flipped"))
        self.flip_table = SimpleTable(
            ["Study", "Predictor", "Outcome", "As reported", "As counted", "Why"],
            stretch_columns=[0, 5],
        )
        layout.addWidget(self.flip_table, 1)

        layout.addWidget(section_label("Relationships that will not be counted"))
        self.uncounted_table = SimpleTable(
            ["Predictor", "Outcome", "Reason"], stretch_columns=[2]
        )
        self.uncounted_table.fit_to_rows(6)
        layout.addWidget(self.uncounted_table)
        return page

    def _build_datasets_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "A team that publishes one survey four times should not "
                "out-vote four independent studies. Possible shared samples "
                "are found from authors, site, sample size and dates, and put "
                "to you: reports are only collapsed into one vote once you "
                "confirm."
            )
        )
        row = QHBoxLayout()
        self.find_links_button = QPushButton("Look for shared datasets")
        self.find_links_button.clicked.connect(self._find_links)
        row.addWidget(self.find_links_button)
        self.confirm_link_button = QPushButton("Same dataset")
        self.confirm_link_button.clicked.connect(lambda: self._decide_link(True))
        row.addWidget(self.confirm_link_button)
        self.reject_link_button = QPushButton("Different datasets")
        self.reject_link_button.clicked.connect(lambda: self._decide_link(False))
        row.addWidget(self.reject_link_button)
        row.addStretch(1)
        self.build_datasets_button = QPushButton("Build the datasets")
        self.build_datasets_button.clicked.connect(self._build_datasets)
        row.addWidget(self.build_datasets_button)
        layout.addLayout(row)

        self.link_table = SimpleTable(
            ["Score", "Study A", "Study B", "Shared signals", "Why"],
            stretch_columns=[1, 2, 4],
        )
        layout.addWidget(self.link_table, 1)

        layout.addWidget(section_label("Datasets"))
        self.dataset_table = SimpleTable(
            ["Dataset", "Reports", "Country"], stretch_columns=[0]
        )
        self.dataset_table.fit_to_rows(6)
        layout.addWidget(self.dataset_table)
        return page

    def _build_inventory_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "One vote per dataset, counted and graded. This is the only "
                "thing the framework may cite: a proposition that rests on "
                "anything not in this table is rejected."
            )
        )
        row = QHBoxLayout()
        self.grade_button = QPushButton("Count and grade")
        self.grade_button.clicked.connect(self._grade)
        row.addWidget(self.grade_button)
        self.override_button = QPushButton("Change this grade…")
        self.override_button.clicked.connect(self._override_grade)
        row.addWidget(self.override_button)
        row.addStretch(1)
        self.export_button = QPushButton("Export the inventory")
        self.export_button.clicked.connect(self._export)
        row.addWidget(self.export_button)
        layout.addLayout(row)

        self.rules_label = QLabel()
        self.rules_label.setWordWrap(True)
        self.rules_label.setStyleSheet("color: #5b6677;")
        layout.addWidget(self.rules_label)

        self.inventory_table = SimpleTable(
            ["Link", "Direction", "Grade", "Datasets", "Agree", "Oppose", "Null",
             "Countries", "Agreement (95% CI)", "Sign test", "Why"],
            stretch_columns=[0, 10],
        )
        layout.addWidget(self.inventory_table, 1)
        return page

    def _build_sensitivity_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "The same counting under five different rules. A link that "
                "holds under all of them is a different kind of finding from "
                "one that disappears when the weakest studies are removed."
            )
        )
        row = QHBoxLayout()
        self.sensitivity_button = QPushButton("Run the sensitivity analyses")
        self.sensitivity_button.clicked.connect(self._sensitivity)
        row.addWidget(self.sensitivity_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.sensitivity_table = SimpleTable(
            ["Analysis", "Link", "Was", "Becomes", "Datasets", "What changed"],
            stretch_columns=[1, 5],
        )
        layout.addWidget(self.sensitivity_table, 1)
        return page

    def _build_contradictions_tab(self) -> QWidget:
        page, layout = _tab()
        layout.addWidget(
            _hint(
                "Where datasets disagree, the disagreement is the finding. "
                "Each contested link is explained from the study "
                "characteristics, with at most one suggested remedy for you "
                "to accept or reject. Nothing is applied until you do."
            )
        )
        row = QHBoxLayout()
        self.contradiction_button = QPushButton("Explain the contested links")
        self.contradiction_button.clicked.connect(self._explain)
        row.addWidget(self.contradiction_button)
        self.accept_contradiction_button = QPushButton("Accept the suggestion")
        self.accept_contradiction_button.clicked.connect(
            lambda: self._decide_contradiction(True)
        )
        row.addWidget(self.accept_contradiction_button)
        self.reject_contradiction_button = QPushButton("Reject it")
        self.reject_contradiction_button.clicked.connect(
            lambda: self._decide_contradiction(False)
        )
        row.addWidget(self.reject_contradiction_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.contradiction_table = SimpleTable(
            ["Link", "Explained by", "Explanation", "Suggests", "Status"],
            stretch_columns=[0, 2],
        )
        layout.addWidget(self.contradiction_table, 1)
        return page

    # -- engine ------------------------------------------------------------

    def _engine(self) -> SynthesisEngine:
        settings = load_settings(self.project)
        client = AIClient(
            self.project,
            provider_from_settings(settings),
            stage="synthesise",
            use_cache=settings.use_cache,
        )
        return SynthesisEngine(
            self.project, client, SynthesisSettings(budget_usd=settings.budget_usd)
        )

    def _busy(self, busy: bool) -> None:
        for button in (
            self.propose_button, self.approve_button, self.map_button,
            self.harmonise_button, self.find_links_button,
            self.build_datasets_button, self.grade_button,
            self.sensitivity_button, self.contradiction_button,
        ):
            button.setEnabled(not busy)
        self.progress.setVisible(busy)

    def _progress(self, done: int, total: int, message: str) -> None:
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)
        self.progress.setFormat(f"{message}  (%p%)")

    def _run_step(self, call, *, title: str, estimate_step: str | None = None,
                  **kwargs) -> None:
        """Run one step off the GUI thread, with a cost estimate if it calls out."""
        if self.project is None:
            return
        if estimate_step is not None:
            try:
                estimate = self._engine().estimate(estimate_step)
            except SynthesisError as exc:
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
        summary = ", ".join(f"{k.replace('n_', '')} {v}"
                            for k, v in (outcome.counts or {}).items())
        self.status_message.emit(f"{outcome.step}: {summary}" if summary
                                 else f"{outcome.step} finished")
        for message in outcome.messages:
            QMessageBox.information(self, "Synthesis", message)
        self.refresh()
        self.data_changed.emit()

    def _step_failed(self, message: str) -> None:
        self.progress.hide()
        QMessageBox.critical(self, "Synthesis stopped", message)

    # -- step 1 ------------------------------------------------------------

    def _propose(self) -> None:
        engine = self._engine()
        self._run_step(
            engine.propose_codebook, title="Propose a codebook",
            estimate_step="codebook",
        )

    def _selected_construct(self) -> dict | None:
        row = self.construct_table.currentRow()
        if row < 0 or row >= len(self._constructs):
            return None
        return self._constructs[row]

    def _add_construct(self) -> None:
        if self.project is None:
            return
        name, ok = QInputDialog.getText(self, "Add a construct", "Name")
        if not ok or not name.strip():
            return
        definition, ok = QInputDialog.getText(
            self, "Add a construct", f"What did studies measure as {name!r}?"
        )
        if not ok:
            return
        try:
            self._engine().add_construct(name, definition=definition)
        except SynthesisError as exc:
            QMessageBox.warning(self, "Cannot add it", str(exc))
            return
        self.refresh()

    def _edit_construct(self) -> None:
        construct = self._selected_construct()
        if construct is None:
            QMessageBox.information(
                self, "Nothing selected", "Choose a construct in the table first."
            )
            return
        definition, ok = QInputDialog.getMultiLineText(
            self, "Edit definition", construct["name"], construct["definition"]
        )
        if not ok:
            return
        self._engine().edit_construct(construct["id"], definition=definition)
        self.refresh()

    def _reject_construct(self) -> None:
        construct = self._selected_construct()
        if construct is None:
            return
        reason, ok = QInputDialog.getText(
            self, "Reject this construct", "Why? (recorded in the audit log)"
        )
        if not ok:
            return
        self._engine().reject_construct(construct["id"], reason=reason)
        self.refresh()

    def _approve(self) -> None:
        if self.project is None:
            return
        engine = self._engine()
        if engine.codebook_approved() and not engine.codebook_changed_since_approval():
            QMessageBox.information(
                self, "Already approved",
                "The codebook is approved and unchanged since then.",
            )
            return
        confirm = QMessageBox.question(
            self, "Approve the codebook",
            "Every label will be mapped against these constructs, and every "
            "finding counted through them. Approve?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            engine.approve_codebook()
        except SynthesisError as exc:
            QMessageBox.warning(self, "Cannot approve", str(exc))
            return
        self.refresh()
        self.data_changed.emit()

    # -- step 2 ------------------------------------------------------------

    def _map(self) -> None:
        engine = self._engine()
        try:
            engine._require_codebook()
        except SynthesisError as exc:
            QMessageBox.warning(self, "Not yet", str(exc))
            return
        self._run_step(
            engine.map_labels, title="Map the labels", estimate_step="mapping",
            on_progress=self._progress,
        )

    def _selected_mapping(self) -> dict | None:
        row = self.contestable_list.currentRow()
        if row < 0 or row >= len(self._contestable):
            return None
        return self._contestable[row]

    def _show_mapping(self, row: int) -> None:
        mapping = self._contestable[row] if 0 <= row < len(self._contestable) else None
        if mapping is None:
            self.mapping_view.setHtml("")
            return
        self.mapping_view.setHtml(
            f"<h3>{html.escape(mapping['label'])}</h3>"
            f"<p><b>Used as</b> {html.escape(mapping['role'])}, "
            f"{mapping['n_uses']} time(s)</p>"
            f"<p><b>Placed in</b> {html.escape(mapping['construct_name'] or '—')}</p>"
            f"<p><b>Read as</b> {html.escape(mapping['measured_as'] or '—')}</p>"
            f"<p><b>Reasoning</b> {html.escape(mapping['rationale'] or '—')}</p>"
            f"<p style='color:#9a4b06'><b>Flagged because</b> "
            f"{html.escape(mapping['contest_reason'] or '—')}</p>"
        )

    def _move_mapping(self) -> None:
        mapping = self._selected_mapping()
        if mapping is None:
            return
        construct_id = self.mapping_combo.currentData()
        if construct_id is None:
            return
        reason, ok = QInputDialog.getText(
            self, "Move this label", "Why does it belong there?"
        )
        if not ok:
            return
        try:
            self._engine().confirm_mapping(
                mapping["id"], construct_id=construct_id, reason=reason
            )
        except SynthesisError as exc:
            QMessageBox.warning(self, "Cannot move it", str(exc))
            return
        self.refresh()

    def _keep_mapping(self) -> None:
        mapping = self._selected_mapping()
        if mapping is None:
            return
        self._engine().confirm_mapping(mapping["id"])
        self.refresh()

    # -- step 3 ------------------------------------------------------------

    def _set_orientation(self) -> None:
        construct = self._selected_construct()
        if construct is None:
            row = self.orientation_table.currentRow()
            pending = [c for c in self._constructs if not c["is_residual"]]
            if 0 <= row < len(pending):
                construct = pending[row]
        if construct is None:
            QMessageBox.information(
                self, "Nothing selected",
                "Choose a construct in the table first.",
            )
            return
        text, ok = QInputDialog.getText(
            self, "Reference orientation",
            f"Which direction of {construct['name']!r} counts as more?\n"
            f'For example "higher trust in local government".',
            text=construct["orientation"] or f"higher {construct['name']}",
        )
        if not ok:
            return
        try:
            self._engine().set_orientation(construct["id"], text)
        except SynthesisError as exc:
            QMessageBox.warning(self, "Cannot set it", str(exc))
            return
        self.refresh()

    def _harmonise(self) -> None:
        engine = self._engine()
        try:
            self._run_step(engine.harmonise, title="Harmonise the directions")
        except SynthesisError as exc:  # pragma: no cover - raised inside the task
            QMessageBox.warning(self, "Not yet", str(exc))

    # -- step 4 ------------------------------------------------------------

    def _find_links(self) -> None:
        engine = self._engine()
        self._run_step(
            engine.find_dataset_links, title="Look for shared datasets",
            estimate_step="linkage",
        )

    def _decide_link(self, confirmed: bool) -> None:
        row = self.link_table.currentRow()
        if row < 0 or row >= len(self._links):
            QMessageBox.information(
                self, "Nothing selected", "Choose a pair in the table first."
            )
            return
        reason, ok = QInputDialog.getText(
            self, "Same dataset?" if confirmed else "Different datasets?",
            "Why? (recorded in the audit log)",
        )
        if not ok:
            return
        self._engine().decide_link(
            self._links[row]["id"], confirmed=confirmed, reason=reason
        )
        self.refresh()

    def _build_datasets(self) -> None:
        self._run_step(self._engine().build_datasets, title="Build the datasets")

    # -- steps 5 to 7 ------------------------------------------------------

    def _grade(self) -> None:
        self._run_step(self._engine().grade, title="Count and grade")

    def _override_grade(self) -> None:
        row = self.inventory_table.currentRow()
        if row < 0 or row >= len(self._graded):
            QMessageBox.information(
                self, "Nothing selected", "Choose a link in the table first."
            )
            return
        link = self._graded[row]
        grade, ok = QInputDialog.getItem(
            self, "Change this grade", f"{link['link_key']} is {link['grade']}.",
            list(_GRADE_ORDER), 0, False,
        )
        if not ok:
            return
        reason, ok = QInputDialog.getMultiLineText(
            self, "Change this grade",
            "Why? This appears beside the grade and in the Methods.",
        )
        if not ok:
            return
        try:
            self._engine().override_grade(link["id"], grade, reason=reason)
        except SynthesisError as exc:
            QMessageBox.warning(self, "Cannot change it", str(exc))
            return
        self.refresh()
        self.data_changed.emit()

    def _sensitivity(self) -> None:
        self._run_step(
            self._engine().run_sensitivity, title="Run the sensitivity analyses"
        )

    def _explain(self) -> None:
        engine = self._engine()
        self._run_step(
            engine.explain_contradictions, title="Explain the contested links",
            estimate_step="contradiction",
        )

    def _decide_contradiction(self, accepted: bool) -> None:
        row = self.contradiction_table.currentRow()
        rows = self._engine().contradictions()
        if row < 0 or row >= len(rows):
            return
        reason, ok = QInputDialog.getText(self, "Your decision", "Why?")
        if not ok:
            return
        self._engine().decide_contradiction(
            rows[row]["id"], accepted=accepted, reason=reason
        )
        self.refresh()

    def _export(self) -> None:
        if self.project is None:
            return
        path = self.project.root / "exports" / "graded-inventory.xlsx"
        try:
            write_graded_inventory(self.project, path)
        except Exception as exc:
            QMessageBox.critical(self, "Could not write it", str(exc))
            return
        self.status_message.emit(f"Graded inventory written to {path}")

    # -- refresh -----------------------------------------------------------

    def _show_approach(self) -> None:
        """Say which of the two codebook steps this button will run.

        The protocol decides it, not this page, so the wording points back
        there rather than offering to change it here: a reviewer who alters
        their framework development approach is amending their protocol,
        and that belongs on the page where amendments are recorded.
        """
        from ...core.protocol import parse_a_priori_constructs

        info = self.project.info()
        if (info.get("framework_approach") or "bottom_up") == "top_down":
            declared = parse_a_priori_constructs(info.get("a_priori_constructs", ""))
            if declared:
                names = ", ".join(name for name, _ in declared[:6])
                more = f", and {len(declared) - 6} more" if len(declared) > 6 else ""
                self.approach_label.setText(
                    f"This review is top-down: its protocol declares "
                    f"{len(declared)} construct(s) — {names}{more}. Proposing "
                    "a codebook writes their definitions against the "
                    "extracted labels. The model cannot add a construct or "
                    "take one away; labels that fit none of them go to the "
                    "residual code and are reported."
                )
            else:
                self.approach_label.setText(
                    "This review is top-down, but its protocol declares no "
                    "constructs. Enter them on the Project page and lock the "
                    "protocol; until then there is nothing to code into."
                )
        else:
            self.approach_label.setText(
                "This review is bottom-up: the model proposes constructs from "
                "the extracted labels, and you edit them. To start from "
                "constructs of your own instead, change the framework "
                "development approach on the Project page."
            )

    def refresh(self) -> None:
        if self.project is None:
            return
        engine = self._engine()
        counts = engine.counts()
        self._constructs = engine.constructs(status=None)
        self._contestable = engine.contestable_mappings()
        self._links = engine.proposed_links()
        self._graded = engine.graded_inventory()

        self.status_label.setText(
            f"{counts.get('n_labels', 0)} label(s) from the extraction · "
            f"{counts.get('n_constructs', 0)} approved construct(s) · "
            f"{counts.get('n_mapped', 0)} mapped · "
            f"{counts.get('n_datasets', 0)} dataset(s) · "
            f"{counts.get('n_graded_links', 0)} graded link(s), of which "
            f"{counts.get('n_strong', 0)} strong and "
            f"{counts.get('n_contested', 0)} contested."
        )
        self._show_approach()
        self._show_blocker(engine, counts)
        self._fill_codebook()
        self._fill_mapping(engine)
        self._fill_orientation(engine)
        self._fill_datasets()
        self._fill_inventory(engine)
        self._fill_sensitivity(engine)
        self._fill_contradictions(engine)

    def _show_blocker(self, engine: SynthesisEngine, counts: dict) -> None:
        message = ""
        if not counts.get("n_labels"):
            message = (
                "There is nothing to synthesise yet: no relationships have "
                "been extracted. Finish the Extract stage first."
            )
        elif not engine.codebook_approved():
            message = (
                "The codebook has not been approved. Propose one, read it, "
                "and approve it before anything is mapped."
            )
        elif engine.codebook_changed_since_approval():
            message = (
                "The codebook has been edited since it was approved. The "
                "existing mappings were made against the older definitions; "
                "approve it again, and re-check the mappings."
            )
        elif counts.get("n_mapped") and engine.constructs_without_orientation():
            missing = engine.constructs_without_orientation()
            message = (
                f"{len(missing)} construct(s) have no reference orientation: "
                + ", ".join(c["name"] for c in missing[:5])
                + ". Until each one has one, a positive finding and a "
                "negative one cannot be compared."
            )
        self.blocker_label.setText(message)
        self.blocker_label.setVisible(bool(message))

    def _fill_codebook(self) -> None:
        import json

        self.construct_table.fill(
            [
                [
                    row["name"],
                    row["status"] + (" (residual)" if row["is_residual"] else ""),
                    row["definition"],
                    "; ".join(json.loads(row["includes"] or "[]")),
                    "; ".join(json.loads(row["excludes"] or "[]")),
                    row["orientation"] or "—",
                ]
                for row in self._constructs
            ]
        )

    def _fill_mapping(self, engine: SynthesisEngine) -> None:
        self.contestable_list.clear()
        for row in self._contestable:
            self.contestable_list.addItem(f"{row['label']}  ({row['role']})")
        self.mapping_combo.clear()
        for construct in engine.constructs(status="approved"):
            self.mapping_combo.addItem(construct["name"], construct["id"])
        self.mapping_table.fill(
            [
                [
                    row["label"], row["role"], row["construct_name"] or "—",
                    row["measured_as"], row["n_uses"], row["status"],
                ]
                for row in self.project.db.query(
                    "SELECT m.*, c.name AS construct_name FROM label_mapping m "
                    "LEFT JOIN construct c ON c.id = m.construct_id "
                    "ORDER BY m.n_uses DESC, m.label"
                )
            ]
        )

    def _fill_orientation(self, engine: SynthesisEngine) -> None:
        self.orientation_table.fill(
            [
                [row["name"], row["orientation"] or "— not set —",
                 row["orientation_set_by"] or ""]
                for row in self._constructs
                if row["status"] == "approved" and not row["is_residual"]
            ]
        )
        self.flip_table.fill(
            [
                [
                    row["title"], row["predictor_label"], row["outcome_label"],
                    row["original_direction"], row["direction"], row["flip_reason"],
                ]
                for row in engine.sign_flips()
            ]
        )
        self.uncounted_table.fill(
            [
                [row["predictor_label"], row["outcome_label"], row["excluded_reason"]]
                for row in engine.uncounted_relationships()
            ]
        )

    def _fill_datasets(self) -> None:
        import json

        self.link_table.fill(
            [
                [
                    f"{row['score']:.2f}", row["title_a"], row["title_b"],
                    ", ".join(str(k) for k in json.loads(row["signals"] or "{}")),
                    row["reason"],
                ]
                for row in self._links
            ]
        )
        self.dataset_table.fill(
            [
                [row["label"], row["n_reports"], row["country"]]
                for row in self.project.db.query(
                    "SELECT d.label, d.country, "
                    "(SELECT COUNT(*) FROM dataset_member m "
                    "  WHERE m.dataset_id = d.id) AS n_reports "
                    "FROM dataset d ORDER BY n_reports DESC, d.id"
                )
            ]
        )

    def _fill_inventory(self, engine: SynthesisEngine) -> None:
        rules = engine.settings.grading
        note = rules.describe()
        unreachable = rules.unreachable_note()
        self.rules_label.setText(note + (f"  {unreachable}" if unreachable else ""))
        self.inventory_table.fill(
            [
                [
                    row["link_key"], row["direction"],
                    row["grade"] + (" (yours)" if row["grade_overridden"] else ""),
                    row["n_datasets"], row["n_agree"], row["n_oppose"],
                    row["n_null"], row["n_countries"],
                    _interval(row),
                    "—" if row["sign_p"] is None else f"p = {row['sign_p']:.4f}",
                    row["grade_reason"],
                ]
                for row in self._graded
            ]
        )

    def _fill_sensitivity(self, engine: SynthesisEngine) -> None:
        self.sensitivity_table.fill(
            [
                [
                    row["analysis"].replace("_", " "), row["link_key"],
                    row["base_grade"], row["new_grade"], row["n_datasets"],
                    "sign and grade" if row["changed_sign"] and row["changed_grade"]
                    else "sign" if row["changed_sign"] else "grade",
                ]
                for row in engine.sensitivity_results()
            ]
        )

    def _fill_contradictions(self, engine: SynthesisEngine) -> None:
        self.contradiction_table.fill(
            [
                [
                    row["link_key"], row["explanation_type"], row["explanation"],
                    row["suggested_moderator"] or row["suggestion_type"],
                    row["status"],
                ]
                for row in engine.contradictions()
            ]
        )


def _tab() -> tuple[QWidget, QVBoxLayout]:
    page = QWidget()
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 8, 0, 0)
    return page, layout


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #5b6677;")
    return label


def _interval(row) -> str:
    if row["proportion"] is None:
        return "—"
    return (
        f"{row['proportion']:.0%} ({row['ci_low']:.0%} to {row['ci_high']:.0%})"
    )
