"""Stage 1: the protocol form.

Stage 1 of the build collects the fields the later stages need and keeps the
exclusion-code ladder visible, because screening is only as good as that
ladder. Drafting, locking and hashing the protocol document is stage 2 of the
build; the button says so rather than pretending.
"""

from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)

from ...core.protocol import ProtocolError, parse_a_priori_constructs
from ..dialogs import FrameworkDialog, ProtocolDialog
from ..widgets import SimpleTable, hint_label, section_label, text_area
from .base import StagePage

__all__ = ["ProjectPage"]


class ProjectPage(StagePage):
    title = "Project"
    subtitle = "The protocol everything else is judged against."

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._layout.addWidget(section_label("Review protocol"))

        form_box = QGroupBox()
        form = QFormLayout(form_box)
        # Labels sit at the top of their row. Centred against a four-line
        # box, a one-word label floats in the middle of nothing.
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        self.title_edit = QLineEdit()
        self.objectives_edit = text_area(
            "What this review is for, in the reviewer's own words.", lines=4
        )
        self.questions_edit = text_area("One research question per line.", lines=4)
        self.framework_combo = QComboBox()
        self.framework_combo.setToolTip(
            "The question framework the protocol states: what the review is "
            "about. Separate from how the framework is built, below."
        )
        self.framework_combo.activated.connect(self._framework_chosen)
        self.framework_stages_label = hint_label("")
        self.inclusion_edit = text_area("Inclusion criteria, one per line.", lines=4)

        self.date_from = QSpinBox()
        self.date_from.setRange(0, 2200)
        self.date_from.setSpecialValueText("no limit")
        self.date_to = QSpinBox()
        self.date_to.setRange(0, 2200)
        self.date_to.setSpecialValueText("no limit")
        self.date_from.setMinimumWidth(110)
        self.date_to.setMinimumWidth(110)
        dates = QHBoxLayout()
        dates.setSpacing(8)
        dates.addWidget(QLabel("from"))
        dates.addWidget(self.date_from)
        dates.addSpacing(8)
        dates.addWidget(QLabel("to"))
        dates.addWidget(self.date_to)
        dates.addStretch(1)

        self.languages_edit = QLineEdit()
        self.languages_edit.setPlaceholderText("English, Spanish — blank means no limit")
        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText(
            "Used to identify you to the Unpaywall API when retrieving full texts"
        )

        form.addRow("Title", self.title_edit)
        form.addRow("Objectives", self.objectives_edit)
        form.addRow("Research questions", self.questions_edit)
        form.addRow("Framework type", self.framework_combo)
        form.addRow("", self.framework_stages_label)
        form.addRow("Inclusion criteria", self.inclusion_edit)
        form.addRow("Publication years", dates)
        form.addRow("Languages", self.languages_edit)
        form.addRow("Contact email", self.email_edit)
        self._layout.addWidget(form_box)

        # -- how the framework gets built ---------------------------------
        #
        # A separate question from "Framework type" above, which is the
        # question framework (PCC, PICO) and says what the review is about.
        # This says where the constructs come from, and it changes what the
        # synthesis stage actually does.
        self._layout.addWidget(section_label("Framework development"))
        self._layout.addWidget(
            hint_label(
                "Where the constructs in the final framework come from. This "
                "is recorded in the protocol, so a top-down review's "
                "constructs are dated and hashed before the evidence is "
                "coded — which is what makes them a priori rather than a "
                "story told afterwards."
            )
        )

        approach_box = QGroupBox()
        approach_layout = QVBoxLayout(approach_box)
        approach_layout.setSpacing(6)

        self.approach_group = QButtonGroup(self)
        self.bottom_up_radio = QRadioButton(
            "Bottom-up — derive the constructs from what the studies measured"
        )
        self.top_down_radio = QRadioButton(
            "Top-down — start from constructs I specify, and code the studies "
            "into them"
        )
        self.approach_group.addButton(self.bottom_up_radio)
        self.approach_group.addButton(self.top_down_radio)
        approach_layout.addWidget(self.bottom_up_radio)
        approach_layout.addWidget(
            hint_label(
                "Inductive. The model proposes a codebook from the extracted "
                "author labels; you read, edit and approve it. Nothing has to "
                "be decided now."
            )
        )
        approach_layout.addSpacing(8)
        approach_layout.addWidget(self.top_down_radio)
        approach_layout.addWidget(
            hint_label(
                "Deductive. Your constructs are the codebook. The model only "
                "writes their definitions and boundaries, and cannot add to "
                "the list or take anything off it. Labels that fit none of "
                "them are reported rather than forced into the nearest one."
            )
        )

        self.constructs_edit = text_area(
            "One construct per line. Optionally: Name — your own definition\n"
            "Place attachment — residents' emotional bond with the locality\n"
            "Perceived benefit — residents' judgement of what they gain",
            lines=5,
        )
        # Which theory the constructs are deduced from, and its citation.
        # A reader cannot judge a deductive framework without knowing what
        # it deduces from: the Methods draft says "constructs drawn from
        # Social Exchange Theory (Ap, 1992)" when these are filled in, and
        # stays general when they are not, rather than inventing a source.
        self.theory_form = QFormLayout()
        self.theory_form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.theory_form.setHorizontalSpacing(18)
        self.theory_edit = QLineEdit()
        self.theory_edit.setPlaceholderText(
            "For example: Social Exchange Theory"
        )
        self.citation_edit = QLineEdit()
        self.citation_edit.setPlaceholderText(
            "Ap, J. (1992). Residents' perceptions on tourism impacts. "
            "Annals of Tourism Research, 19(4), 665–690."
        )
        self.theory_label = QLabel("Source theory")
        self.citation_label = QLabel("Citation")
        self.theory_form.addRow(self.theory_label, self.theory_edit)
        self.theory_form.addRow(self.citation_label, self.citation_edit)
        approach_layout.addSpacing(10)
        approach_layout.addLayout(self.theory_form)

        self.constructs_label = QLabel("Constructs, specified in advance")
        approach_layout.addSpacing(10)
        approach_layout.addWidget(self.constructs_label)
        approach_layout.addWidget(self.constructs_edit)
        self.constructs_count = hint_label("")
        approach_layout.addWidget(self.constructs_count)

        self.bottom_up_radio.toggled.connect(self._approach_changed)
        self.constructs_edit.textChanged.connect(self._count_constructs)
        self._layout.addWidget(approach_box)

        buttons = QHBoxLayout()
        self.save_button = QPushButton("Save protocol fields")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        self.lock_button = QPushButton("Draft and lock protocol")
        self.lock_button.setToolTip(
            "Builds the protocol document from these fields and stores it with "
            "a timestamp and a SHA-256 hash. Later changes are recorded as "
            "amendments."
        )
        self.lock_button.clicked.connect(self._lock_protocol)
        buttons.addWidget(self.lock_button)
        self.view_button = QPushButton("View protocol")
        self.view_button.clicked.connect(self._view_protocol)
        buttons.addWidget(self.view_button)
        buttons.addStretch(1)
        self._layout.addLayout(buttons)

        self.protocol_label = hint_label("")
        self._layout.addWidget(self.protocol_label)

        self._layout.addWidget(section_label("Exclusion codes"))
        self._layout.addWidget(
            hint_label(
                "Applied in this order during screening, cheapest and most "
                "objective first. Screening reports the first code a record "
                "fails."
            )
        )
        # The definition is the column a reviewer actually reads; the code
        # is three characters. Stretching the first column, which is the
        # default, gave acres of white space to "E1" and squeezed the
        # sentence that says what E1 means.
        self.codes_table = SimpleTable(
            ["Code", "Label", "Definition", "Order"], stretch_columns=[2]
        )
        self.codes_table.fit_to_rows(9)
        self._layout.addWidget(self.codes_table)

        self.amendments_label = section_label("Amendment log")
        self._layout.addWidget(self.amendments_label)
        self.amendments_table = SimpleTable(
            ["Version", "Changed", "Reason", "After data were seen"]
        )
        self.amendments_table.fit_to_rows(6)
        self._layout.addWidget(self.amendments_table)

        self._layout.addStretch(1)

    # -- the framework approach -------------------------------------------

    def _approach_changed(self) -> None:
        """Show the constructs box only where it means something.

        Left enabled under bottom-up it invites a reviewer to fill in a list
        that nothing will read, and the engine deliberately ignores it.
        """
        top_down = self.top_down_radio.isChecked()
        for widget in (
            self.constructs_label,
            self.constructs_edit,
            self.constructs_count,
            self.theory_label,
            self.theory_edit,
            self.citation_label,
            self.citation_edit,
        ):
            widget.setVisible(top_down)
        self._count_constructs()

    # -- the question framework -------------------------------------------

    #: The last entry in the dropdown, which opens the dialog rather than
    #: selecting anything.
    DEFINE_NEW = "Define a framework of my own…"

    def _fill_frameworks(self, selected: str = "") -> None:
        """Built-ins, then this review's own, then the way to add one."""
        if self.project is None:
            return
        wanted = selected or self.framework_combo.currentText()
        self.framework_combo.blockSignals(True)
        self.framework_combo.clear()
        for framework in self.project.frameworks():
            label = framework["name"]
            if not framework["is_built_in"]:
                label = f"{framework['name']}  (yours)"
            self.framework_combo.addItem(label, framework["name"])
        self.framework_combo.addItem(self.DEFINE_NEW, "")
        index = self.framework_combo.findData(wanted)
        self.framework_combo.setCurrentIndex(max(index, 0))
        self.framework_combo.blockSignals(False)
        self._show_framework_stages()

    def _current_framework(self) -> str:
        return self.framework_combo.currentData() or ""

    def _framework_chosen(self, index: int) -> None:
        if self.framework_combo.itemText(index) != self.DEFINE_NEW:
            self._show_framework_stages()
            return
        dialog = FrameworkDialog(self.project, self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.created:
            self._fill_frameworks(selected=dialog.created)
            self.status_message.emit(
                f"Framework {dialog.created} defined. Save the protocol "
                "fields to use it."
            )
        else:
            # Put the dropdown back where it was rather than leaving it on
            # an entry that is not a framework.
            self._fill_frameworks(
                selected=self.project.info()["framework_type"]
            )

    def _show_framework_stages(self) -> None:
        """Say what the Gap Finder will look for, under the dropdown."""
        from ...core.frameworks import format_stages

        name = self._current_framework()
        if not name or self.project is None:
            self.framework_stages_label.setText("")
            return
        stages = self.project.framework_stages(name)
        if stages:
            self.framework_stages_label.setText(
                f"Stages: {format_stages(stages)}. The Gap Finder reports "
                "which transitions between them your graded evidence does "
                "not cover."
            )
        else:
            self.framework_stages_label.setText(
                "This framework has no stages recorded, so stage-transition "
                "gaps cannot be computed for it."
            )

    def _count_constructs(self) -> None:
        if not self.top_down_radio.isChecked():
            return
        constructs = parse_a_priori_constructs(self.constructs_edit.toPlainText())
        if not constructs:
            self.constructs_count.setText(
                "No constructs yet. The synthesis stage cannot build a "
                "top-down codebook until there is at least one."
            )
            return
        with_definitions = sum(1 for _, definition in constructs if definition)
        self.constructs_count.setText(
            f"{len(constructs)} construct(s), {with_definitions} with a "
            "definition of your own. The rest will be defined by the model "
            "for you to edit."
        )

    # -- data --------------------------------------------------------------

    def refresh(self) -> None:
        if self.project is None:
            self.title_edit.clear()
            self.codes_table.fill([])
            return
        info = self.project.info()
        self.title_edit.setText(info["title"])
        self.objectives_edit.setPlainText(info["objectives"])
        self.questions_edit.setPlainText("\n".join(info["research_questions"]))
        self._fill_frameworks(selected=info["framework_type"])
        top_down = info.get("framework_approach") == "top_down"
        (self.top_down_radio if top_down else self.bottom_up_radio).setChecked(True)
        self.constructs_edit.setPlainText(info.get("a_priori_constructs") or "")
        self.theory_edit.setText(info.get("a_priori_theory") or "")
        self.citation_edit.setText(info.get("a_priori_citation") or "")
        self._approach_changed()
        self.inclusion_edit.setPlainText(info["inclusion_criteria"])
        self.date_from.setValue(info["date_from"] or 0)
        self.date_to.setValue(info["date_to"] or 0)
        self.languages_edit.setText(", ".join(info["languages"]))
        self.email_edit.setText(info["contact_email"] or "")
        self.codes_table.fill(
            [
                [c["code"], c["label"], c["definition"], c["position"]]
                for c in self.project.exclusion_codes()
            ]
        )
        self._refresh_protocol()

    def _refresh_protocol(self) -> None:
        protocol = self.project.protocol
        summary = protocol.summary()
        if not summary["locked"]:
            self.protocol_label.setText(
                "The protocol has not been locked. Screening needs a locked "
                "protocol, so that every decision can be traced to the exact "
                "wording it was judged against."
            )
            self.lock_button.setText("Draft and lock protocol")
        else:
            changed = (
                "  The form has changed since: locking again records an "
                "amendment."
                if summary["has_unlocked_changes"]
                else "  The form matches the locked version."
            )
            self.protocol_label.setText(
                f"Version {summary['version']}, locked {summary['locked_at']} "
                f"by {summary['locked_by']}. SHA-256 "
                f"{summary['content_hash'][:16]}…{changed}"
            )
            self.lock_button.setText(
                "Amend protocol" if summary["has_unlocked_changes"] else "Protocol locked"
            )
            self.lock_button.setEnabled(summary["has_unlocked_changes"])

        amendments = protocol.amendments()
        self.amendments_table.fill(
            [
                [
                    f"{a['from_number']} → {a['to_number']}",
                    (a["changed_at"] or "").replace("T", " ").replace("Z", ""),
                    a["reason"],
                    "yes" if a["after_data_seen"] else "no",
                ]
                for a in amendments
            ]
        )
        self.amendments_table.setVisible(bool(amendments))
        self.amendments_label.setVisible(bool(amendments))

    def _lock_protocol(self) -> None:
        if self.project is None:
            return
        protocol = self.project.protocol
        is_amendment = protocol.is_locked()
        dialog = ProtocolDialog(
            protocol.draft(),
            is_amendment=is_amendment,
            diff=protocol.pending_diff(),
            after_data_seen=protocol.data_seen(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            version = protocol.lock(reason=dialog.reason())
        except ProtocolError as exc:
            QMessageBox.warning(self, "Could not lock the protocol", str(exc))
            return
        protocol.export(self.project.exports_dir / f"protocol-v{version.version}.md")
        self.status_message.emit(
            f"Protocol version {version.version} locked ({version.short_hash})."
        )
        self.refresh()
        self.data_changed.emit()

    def _view_protocol(self) -> None:
        if self.project is None:
            return
        protocol = self.project.protocol
        current = protocol.current()
        dialog = ProtocolDialog(
            current.content if current else protocol.draft(),
            is_amendment=False,
            parent=self,
        )
        dialog.setWindowTitle(
            f"Protocol version {current.version}" if current else "Protocol draft"
        )
        dialog.exec()

    def _save(self) -> None:
        if self.project is None:
            return
        questions = [
            line.strip()
            for line in self.questions_edit.toPlainText().splitlines()
            if line.strip()
        ]
        languages = [
            part.strip()
            for part in self.languages_edit.text().split(",")
            if part.strip()
        ]
        self.project.update_info(
            title=self.title_edit.text().strip() or "Untitled review",
            objectives=self.objectives_edit.toPlainText().strip(),
            research_questions=json.dumps(questions),
            framework_type=(
                self._current_framework() or self.project.info()["framework_type"]
            ),
            framework_approach=(
                "top_down" if self.top_down_radio.isChecked() else "bottom_up"
            ),
            a_priori_constructs=self.constructs_edit.toPlainText().strip(),
            a_priori_theory=self.theory_edit.text().strip(),
            a_priori_citation=self.citation_edit.text().strip(),
            inclusion_criteria=self.inclusion_edit.toPlainText().strip(),
            date_from=self.date_from.value() or None,
            date_to=self.date_to.value() or None,
            languages=json.dumps(languages),
            contact_email=self.email_edit.text().strip(),
        )
        self.status_message.emit("Protocol fields saved.")
        self.data_changed.emit()
