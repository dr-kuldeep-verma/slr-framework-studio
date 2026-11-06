"""Dialogs shared across stages."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..ai import keys
from ..ai.providers import PROVIDERS
from ..ai.settings import ModelSettings, provider_from_settings

__all__ = [
    "ModelSettingsDialog",
    "ProtocolDialog",
    "CostDialog",
    "FrameworkDialog",
    "confirm_cost",
]


class FrameworkDialog(QDialog):
    """Define a question framework the application does not know.

    The stages are not optional, and the dialog says why rather than just
    refusing: the Gap Finder reports which transitions between a
    framework's stages no graded link covers, so a framework with no
    stages silently produces no gaps of that kind. Asking for them here is
    the price of letting a reviewer use a framework from their own field.
    """

    def __init__(self, project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.setWindowTitle("Define a question framework")
        self.setMinimumWidth(540)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            "A question framework is the shape your question and eligibility "
            "criteria are written in. Give it a name and its stages, in "
            "order. The Gap Finder reports which transitions between "
            "consecutive stages your evidence does not cover, so a framework "
            "without stages would quietly report no gaps of that kind."
        )
        intro.setWordWrap(True)
        intro.setObjectName("hintLabel")
        layout.addWidget(intro)

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("For example: Resident response")
        self.stages_edit = QLineEdit()
        self.stages_edit.setPlaceholderText(
            "antecedent → appraisal → response   (arrows, commas or new lines)"
        )
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText(
            "Optional: where this framework comes from, so a reader can look it up"
        )
        form.addRow("Name", self.name_edit)
        form.addRow("Stages, in order", self.stages_edit)
        form.addRow("Source", self.note_edit)
        layout.addLayout(form)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setObjectName("hintLabel")
        layout.addWidget(self.preview)
        self.stages_edit.textChanged.connect(self._update_preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.created: str = ""

    def _update_preview(self) -> None:
        from ..core.frameworks import format_stages, parse_stages

        stages = parse_stages(self.stages_edit.text())
        if len(stages) < 2:
            self.preview.setText(
                "At least two stages: one stage has no transition, so nothing "
                "would be looked for."
            )
            return
        transitions = ", ".join(
            f"{a} → {b}" for a, b in zip(stages, stages[1:])
        )
        self.preview.setText(
            f"{len(stages)} stages: {format_stages(stages)}. The Gap Finder "
            f"will check these transitions: {transitions}."
        )

    def _accept(self) -> None:
        from ..core.frameworks import FrameworkError

        try:
            self.project.define_framework(
                self.name_edit.text(),
                self.stages_edit.text(),
                note=self.note_edit.text().strip(),
                actor=None,
            )
        except FrameworkError as exc:
            QMessageBox.warning(self, "Cannot define this framework", str(exc))
            return
        self.created = " ".join(self.name_edit.text().split())
        self.accept()


class ModelSettingsDialog(QDialog):
    """Choose the provider, model and spending limit for this review."""

    def __init__(self, settings: ModelSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Model settings")
        self.setMinimumWidth(560)
        self.settings = settings

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.provider_combo = QComboBox()
        for spec in PROVIDERS.values():
            self.provider_combo.addItem(spec.label, spec.key)
        index = self.provider_combo.findData(settings.provider)
        self.provider_combo.setCurrentIndex(max(index, 0))
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        form.addRow("Provider", self.provider_combo)

        self.model_edit = QLineEdit(settings.model)
        self.model_edit.setPlaceholderText(settings.resolved_model())
        form.addRow("Model", self.model_edit)

        self.base_url_edit = QLineEdit(settings.base_url)
        self.base_url_edit.setPlaceholderText(settings.resolved_base_url())
        form.addRow("Endpoint", self.base_url_edit)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Leave blank to keep the stored key")
        key_row.addWidget(self.key_edit, 1)
        self.forget_button = QPushButton("Forget key")
        self.forget_button.clicked.connect(self._forget_key)
        key_row.addWidget(self.forget_button)
        form.addRow("API key", key_row)

        self.budget_spin = QDoubleSpinBox()
        self.budget_spin.setRange(0.0, 100_000.0)
        self.budget_spin.setDecimals(2)
        self.budget_spin.setPrefix("US$ ")
        self.budget_spin.setSpecialValueText("no limit")
        self.budget_spin.setValue(settings.budget_usd or 0.0)
        self.budget_spin.setToolTip(
            "A run stops when it reaches this. Everything already decided is "
            "kept, and the run can be continued after you raise the limit."
        )
        form.addRow("Spending limit", self.budget_spin)

        self.cache_check = QCheckBox(
            "Reuse earlier answers for identical prompts (free, and keeps re-runs identical)"
        )
        self.cache_check.setChecked(settings.use_cache)
        form.addRow("", self.cache_check)

        self.offline_check = QCheckBox(
            "Use the built-in offline screener instead of a real model"
        )
        self.offline_check.setChecked(settings.offline)
        self.offline_check.setToolTip(
            "Keyword rules that run on this computer. Useful for trying the "
            "workflow without an API key. Its answers are recorded as coming "
            "from the offline screener, so a demo run can never be mistaken "
            "for a real one."
        )
        form.addRow("", self.offline_check)
        layout.addLayout(form)

        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #5b6677;")
        layout.addWidget(self.note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        buttons.addButton(self.test_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._provider_changed()

    def _provider_key(self) -> str:
        return str(self.provider_combo.currentData())

    def _provider_changed(self) -> None:
        spec = PROVIDERS[self._provider_key()]
        self.model_edit.setPlaceholderText(spec.default_model)
        self.base_url_edit.setPlaceholderText(spec.base_url)
        needs_key = spec.needs_key
        self.key_edit.setEnabled(needs_key)
        self.forget_button.setEnabled(needs_key)
        stored = keys.has_key(spec.key) if needs_key else False
        where = (
            "Records are sent to this provider."
            if spec.sends_data_off_device
            else "Runs on this computer; nothing is sent anywhere."
        )
        key_note = (
            ""
            if not needs_key
            else (" A key is stored." if stored else " No key is stored yet.")
        )
        self.note.setText(f"{spec.description} {where}{key_note}")

    def _forget_key(self) -> None:
        keys.delete_key(self._provider_key())
        self.key_edit.clear()
        self._provider_changed()

    def _test(self) -> None:
        settings = self.values()
        if settings.offline:
            QMessageBox.information(
                self, "Offline screener",
                "The offline screener needs no connection. It answers from "
                "keyword rules on this computer.",
            )
            return
        self._save_key()
        try:
            provider = provider_from_settings(settings)
        except Exception as exc:
            QMessageBox.critical(self, "Could not reach the model", str(exc))
            return
        ok, message = provider.check()
        if ok:
            QMessageBox.information(self, "Connected", message)
        else:
            QMessageBox.warning(self, "Could not reach the model", message)

    def _save_key(self) -> None:
        text = self.key_edit.text().strip()
        if not text:
            return
        try:
            keys.set_key(self._provider_key(), text)
        except Exception as exc:
            QMessageBox.warning(
                self, "Could not store the key",
                f"{exc}\n\nThe key has not been saved. You can set it as an "
                "environment variable instead.",
            )
            return
        self.key_edit.clear()
        self._provider_changed()

    def values(self) -> ModelSettings:
        return ModelSettings(
            provider=self._provider_key(),
            model=self.model_edit.text().strip(),
            base_url=self.base_url_edit.text().strip(),
            budget_usd=self.budget_spin.value() or None,
            use_cache=self.cache_check.isChecked(),
            offline=self.offline_check.isChecked(),
        )

    def accept(self) -> None:
        self._save_key()
        super().accept()


class ProtocolDialog(QDialog):
    """Show the drafted protocol and lock it, with a reason when amending."""

    def __init__(
        self,
        draft: str,
        *,
        is_amendment: bool,
        diff: str = "",
        after_data_seen: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Amend the protocol" if is_amendment else "Lock the protocol")
        self.setMinimumSize(760, 620)
        self.is_amendment = is_amendment

        layout = QVBoxLayout(self)
        intro = QLabel(
            "This document is built from the project form. Locking it stores "
            "it with a timestamp and a SHA-256 hash, so any later change shows "
            "up in the amendment log."
            if not is_amendment
            else "The form has changed since the protocol was locked. Locking "
            "again records a new version and an amendment."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        if after_data_seen:
            warning = QLabel(
                "Records have already been imported, so this amendment will be "
                "recorded as made after the data were seen. That is allowed, "
                "and it is reported in the Methods section."
            )
            warning.setWordWrap(True)
            warning.setObjectName("blockerLabel")
            layout.addWidget(warning)

        if diff:
            layout.addWidget(QLabel("What changed:"))
            diff_view = QPlainTextEdit(diff)
            diff_view.setReadOnly(True)
            diff_view.setMaximumHeight(170)
            diff_view.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
            layout.addWidget(diff_view)

        layout.addWidget(QLabel("The protocol:"))
        self.view = QPlainTextEdit(draft)
        self.view.setReadOnly(True)
        layout.addWidget(self.view, 1)

        if is_amendment:
            layout.addWidget(QLabel("Reason for the amendment (required):"))
            self.reason_edit = QLineEdit()
            self.reason_edit.setPlaceholderText(
                "e.g. the search found a body of work in a language the protocol did not cover"
            )
            layout.addWidget(self.reason_edit)
        else:
            self.reason_edit = None

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Record amendment" if is_amendment else "Lock protocol"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reason(self) -> str:
        return self.reason_edit.text().strip() if self.reason_edit else ""

    def accept(self) -> None:
        if self.is_amendment and not self.reason():
            QMessageBox.warning(
                self, "A reason is needed",
                "An amendment is recorded with its reason. Say briefly why the "
                "protocol changed.",
            )
            return
        super().accept()


class CostDialog(QDialog):
    """Integrity rule 5: nothing bulk runs before the cost is shown."""

    def __init__(self, estimate: Any, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        headline = QLabel(estimate.describe())
        headline.setWordWrap(True)
        font = headline.font()
        font.setPointSize(font.pointSize() + 1)
        headline.setFont(font)
        layout.addWidget(headline)

        details = QLabel(
            f"Prompt tokens: about {estimate.prompt_tokens:,}\n"
            f"Completion tokens: about {estimate.completion_tokens:,}\n"
            f"Model: {estimate.model}"
        )
        details.setStyleSheet("color: #5b6677;")
        layout.addWidget(details)

        for note in getattr(estimate, "notes", []):
            label = QLabel(note)
            label.setWordWrap(True)
            label.setStyleSheet("color: #5b6677;")
            layout.addWidget(label)

        caveat = QLabel(
            "This is an estimate from a rough token count and the price table "
            "in Model settings, not a quotation. What the run actually costs "
            "is recorded per call. You can stop the run at any point and keep "
            "what it has already decided."
        )
        caveat.setWordWrap(True)
        caveat.setStyleSheet("color: #5b6677;")
        layout.addWidget(caveat)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start the run")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def confirm_cost(estimate: Any, title: str, parent: QWidget | None = None) -> bool:
    return CostDialog(estimate, title, parent).exec() == QDialog.DialogCode.Accepted
