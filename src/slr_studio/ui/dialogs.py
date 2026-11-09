"""Dialogs shared across stages."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..ai import keys
from ..ai.providers import PROVIDERS
from ..ai.settings import ModelSettings, provider_from_settings
from .scrolling import make_scrollable

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
        make_scrollable(self)

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


#: The three ways to pay for a model, in the order a new reviewer meets them.
CHOICE_LOCAL = "local"
CHOICE_PLAN = "plan"
CHOICE_API = "api"

_PRIVACY = {
    CHOICE_LOCAL: "Privacy: runs on this computer; nothing is sent anywhere.",
    CHOICE_PLAN: (
        "Privacy: records are sent to OpenAI under your ChatGPT account. "
        "Each request asks OpenAI not to keep it."
    ),
    CHOICE_API: "Privacy: records are sent to this provider.",
}


def _choice_for(provider: str) -> str:
    if provider == "ollama":
        return CHOICE_LOCAL
    if provider == "chatgpt_subscription":
        return CHOICE_PLAN
    return CHOICE_API


def _muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #5b6677;")
    return label


class ModelSettingsDialog(QDialog):
    """Choose which model reads the records, in plain words.

    Three choices, each saying what it costs and where the records go:
    free on this computer (Ollama), the reviewer's ChatGPT subscription, or a
    pay-as-you-go API key. Everything else is under "More options".
    """

    _pull_progress = Signal(int, int, str)

    def __init__(self, settings: ModelSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Model settings")
        self.setMinimumWidth(620)
        self.resize(620, 600)
        self.settings = settings
        self._task = None
        self._stop_sign_in = False

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        title = QLabel("Which model should read your records?")
        font = title.font()
        font.setPointSize(font.pointSize() + 2)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        layout.addWidget(_muted(
            "Every answer a model gives is logged and can be overridden, "
            "whichever you choose. You can change this later; the AI-use "
            "statement reports every model a review used."
        ))

        # -- the three choices ------------------------------------------
        self.choice_group = QButtonGroup(self)
        self.radio_local = QRadioButton("Free, on this computer (Ollama)")
        self.radio_plan = QRadioButton("My ChatGPT subscription (Go, Plus or Pro)")
        self.radio_api = QRadioButton("Pay as you go, with an API key")
        blurbs = {
            self.radio_local: "No account and no cost. Slower, and smaller models "
            "make more mistakes, so check more of their decisions.",
            self.radio_plan: "No API key and no per-call charge. Calls count "
            "against your plan's usage limits.",
            self.radio_api: "Claude, OpenAI or a compatible service, charged per "
            "token by the provider. The most capable models.",
        }
        for radio, choice in (
            (self.radio_local, CHOICE_LOCAL),
            (self.radio_plan, CHOICE_PLAN),
            (self.radio_api, CHOICE_API),
        ):
            radio.setProperty("choice", choice)
            self.choice_group.addButton(radio)
            radio_font = radio.font()
            radio_font.setBold(True)
            radio.setFont(radio_font)
            layout.addWidget(radio)
            blurb = _muted(blurbs[radio])
            blurb.setContentsMargins(24, 0, 0, 4)
            layout.addWidget(blurb)

        # One panel per choice; only the chosen one is shown. Plain show and
        # hide rather than a stacked widget, which does not pass the height
        # of wrapped text through and clipped the status lines.
        self.panels = [self._local_panel(), self._plan_panel(), self._api_panel()]
        panel_frame = QFrame()
        panel_frame.setObjectName("card")
        panel_frame.setFrameShape(QFrame.Shape.StyledPanel)
        frame_layout = QVBoxLayout(panel_frame)
        for panel in self.panels:
            frame_layout.addWidget(panel)
        layout.addWidget(panel_frame)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        # -- more options -----------------------------------------------
        self.more_button = QToolButton()
        self.more_button.setText("More options")
        self.more_button.setCheckable(True)
        self.more_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.more_button.setArrowType(Qt.ArrowType.RightArrow)
        self.more_button.toggled.connect(self._toggle_more)
        layout.addWidget(self.more_button)
        self.more = QWidget()
        more_layout = QVBoxLayout(self.more)
        more_layout.setContentsMargins(18, 0, 0, 0)
        self.cache_check = QCheckBox(
            "Reuse earlier answers for identical prompts (free, and keeps re-runs identical)"
        )
        self.cache_check.setChecked(settings.use_cache)
        more_layout.addWidget(self.cache_check)
        self.offline_check = QCheckBox(
            "Use the built-in demonstration screener instead of a real model"
        )
        self.offline_check.setChecked(settings.offline)
        self.offline_check.setToolTip(
            "Keyword rules that run on this computer, for trying the workflow. "
            "Its answers are recorded as coming from the demonstration "
            "screener, so a demo run can never be mistaken for a real one."
        )
        self.offline_check.toggled.connect(self._update_note)
        more_layout.addWidget(self.offline_check)
        self.more.setVisible(False)
        layout.addWidget(self.more)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Save")
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test)
        buttons.addButton(self.test_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        make_scrollable(self)

        self._pull_progress.connect(self._show_pull_progress)
        self.choice_group.buttonToggled.connect(self._choice_changed)
        {
            CHOICE_LOCAL: self.radio_local,
            CHOICE_PLAN: self.radio_plan,
            CHOICE_API: self.radio_api,
        }[_choice_for(settings.provider)].setChecked(True)
        self._choice_changed()

    def _fit(self) -> None:
        """Fit the height to what is shown, wrapped text included.

        Qt's ordinary size hint ignores how tall wrapped labels become at the
        dialog's width, which squeezed the status lines into one another.
        """
        width = max(self.width(), self.minimumWidth())
        area = getattr(self, "_scroll_area", None)
        if area is not None:
            area.updateGeometry()
        self.layout().activate()
        self.resize(width, min(self.sizeHint().height(), self.maximumHeight()))

    # -- panels ------------------------------------------------------------

    def _local_panel(self) -> QWidget:
        from ..ai.ollama import RECOMMENDED_MODELS

        panel = QWidget()
        form = QFormLayout(panel)
        self.local_status = QLabel()
        self.local_status.setWordWrap(True)
        form.addRow("Status", self.local_status)
        status_row = QHBoxLayout()
        self.local_refresh_button = QPushButton("Check again")
        self.local_refresh_button.clicked.connect(self._refresh_local)
        status_row.addWidget(self.local_refresh_button)
        self.install_button = QPushButton("Get Ollama (free)")
        self.install_button.clicked.connect(self._open_install_page)
        status_row.addWidget(self.install_button)
        status_row.addStretch(1)
        form.addRow("", status_row)

        model_row = QHBoxLayout()
        self.local_model_combo = QComboBox()
        self.local_model_combo.setEditable(True)
        self.local_model_combo.setToolTip(
            "A model you have downloaded, or the name of one to download."
        )
        model_row.addWidget(self.local_model_combo, 1)
        self.pull_button = QPushButton("Download")
        self.pull_button.clicked.connect(self._pull)
        model_row.addWidget(self.pull_button)
        form.addRow("Model", model_row)
        self._recommended = RECOMMENDED_MODELS
        self.local_hint = _muted()
        form.addRow("", self.local_hint)
        self.pull_bar = QProgressBar()
        self.pull_bar.setVisible(False)
        form.addRow("", self.pull_bar)

        self.local_url_edit = QLineEdit(
            self.settings.base_url if self.settings.provider == "ollama" else ""
        )
        self.local_url_edit.setPlaceholderText(PROVIDERS["ollama"].base_url)
        self.local_url_edit.setToolTip("Only change this if Ollama runs somewhere unusual.")
        form.addRow("Address", self.local_url_edit)
        return panel

    def _plan_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        status_row = QHBoxLayout()
        self.plan_status = QLabel()
        self.plan_status.setWordWrap(True)
        status_row.addWidget(self.plan_status, 1)
        self.sign_in_button = QPushButton("Sign in with ChatGPT")
        self.sign_in_button.clicked.connect(self._sign_in)
        status_row.addWidget(self.sign_in_button)
        self.sign_out_button = QPushButton("Sign out")
        self.sign_out_button.clicked.connect(self._sign_out)
        status_row.addWidget(self.sign_out_button)
        form.addRow("Account", status_row)

        model_row = QHBoxLayout()
        self.plan_model_combo = QComboBox()
        self.plan_model_combo.setEditable(True)
        if self.settings.provider == "chatgpt_subscription" and self.settings.model:
            self.plan_model_combo.addItem(self.settings.model, self.settings.model)
        model_row.addWidget(self.plan_model_combo, 1)
        self.plan_models_button = QPushButton("Refresh list")
        self.plan_models_button.clicked.connect(self._load_plan_models)
        model_row.addWidget(self.plan_models_button)
        form.addRow("Model", model_row)
        form.addRow("", _muted(
            "Your browser opens at OpenAI to sign in; this application never "
            "sees your password. Tokens are kept in Windows Credential "
            "Manager. Before each run the cost screen shows your plan's limits "
            "instead of a price."
        ))
        return panel

    def _api_panel(self) -> QWidget:
        panel = QWidget()
        form = QFormLayout(panel)
        self.provider_combo = QComboBox()
        for spec in PROVIDERS.values():
            if spec.auth == "key":
                self.provider_combo.addItem(spec.label, spec.key)
        index = self.provider_combo.findData(self.settings.provider)
        self.provider_combo.setCurrentIndex(max(index, 0))
        self.provider_combo.currentIndexChanged.connect(self._api_service_changed)
        form.addRow("Service", self.provider_combo)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        key_row.addWidget(self.key_edit, 1)
        self.forget_button = QPushButton("Forget key")
        self.forget_button.clicked.connect(self._forget_key)
        key_row.addWidget(self.forget_button)
        form.addRow("API key", key_row)
        self.key_status = _muted()
        form.addRow("", self.key_status)

        api_now = _choice_for(self.settings.provider) == CHOICE_API
        self.model_edit = QLineEdit(self.settings.model if api_now else "")
        form.addRow("Model", self.model_edit)
        self.base_url_edit = QLineEdit(self.settings.base_url if api_now else "")
        self.base_url_edit.setToolTip(
            "Only needed for an Azure deployment or a university gateway."
        )
        form.addRow("Endpoint", self.base_url_edit)

        self.budget_spin = QDoubleSpinBox()
        self.budget_spin.setRange(0.0, 100_000.0)
        self.budget_spin.setDecimals(2)
        self.budget_spin.setPrefix("US$ ")
        self.budget_spin.setSpecialValueText("no limit")
        self.budget_spin.setValue(self.settings.budget_usd or 0.0)
        self.budget_spin.setToolTip(
            "A run stops when it reaches this. Everything already decided is "
            "kept, and the run can be continued after you raise the limit."
        )
        form.addRow("Spending limit", self.budget_spin)
        return panel

    # -- reacting ------------------------------------------------------------

    def choice(self) -> str:
        button = self.choice_group.checkedButton()
        return str(button.property("choice")) if button is not None else CHOICE_API

    def _choice_changed(self, *args) -> None:
        choice = self.choice()
        current = [CHOICE_LOCAL, CHOICE_PLAN, CHOICE_API].index(choice)
        for index, panel in enumerate(self.panels):
            panel.setVisible(index == current)
        self._fit()
        if choice == CHOICE_LOCAL:
            self._refresh_local()
        elif choice == CHOICE_PLAN:
            self._refresh_plan()
        else:
            self._api_service_changed()
        self._update_note()

    def _update_note(self, *args) -> None:
        if self.offline_check.isChecked():
            text = (
                "The demonstration screener is on: keyword rules on this "
                "computer answer instead of a model, and nothing is sent anywhere."
            )
        else:
            text = _PRIVACY[self.choice()]
        self.note.setText(text)

    def _toggle_more(self, shown: bool) -> None:
        self.more.setVisible(shown)
        self.more_button.setArrowType(
            Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow
        )
        self._fit()

    def _provider_key(self) -> str:
        choice = self.choice()
        if choice == CHOICE_LOCAL:
            return "ollama"
        if choice == CHOICE_PLAN:
            return "chatgpt_subscription"
        return str(self.provider_combo.currentData())

    # local

    def _refresh_local(self) -> None:
        from ..ai.ollama import ollama_status

        url = self.local_url_edit.text().strip() or PROVIDERS["ollama"].base_url
        status = ollama_status(url, timeout=1.0)
        self.local_status.setText(status.describe())
        self.install_button.setVisible(not status.running)
        self.pull_button.setEnabled(status.running)
        current = self.local_model_combo.currentText().strip()
        if not current and self.settings.provider == "ollama":
            current = self.settings.model
        self.local_model_combo.clear()
        for name in status.models:
            self.local_model_combo.addItem(name, name)
        installed = set(status.models)
        for name, size, why in self._recommended:
            if name not in installed:
                self.local_model_combo.addItem(f"{name}  (download {size})", name)
        if current:
            index = self.local_model_combo.findData(current)
            if index >= 0:
                self.local_model_combo.setCurrentIndex(index)
            else:
                self.local_model_combo.setEditText(current)
        elif status.models:
            self.local_model_combo.setCurrentIndex(0)
        name, _, why = self._recommended[0]
        self.local_hint.setText(
            f"Not sure? {name} is {why}. Hover over the list for the others."
        )
        self.local_model_combo.setToolTip(
            "\n".join(f"{n} ({size}): {w}" for n, size, w in self._recommended)
        )

    def _local_model(self) -> str:
        data = self.local_model_combo.currentData()
        text = self.local_model_combo.currentText().strip()
        index = self.local_model_combo.currentIndex()
        if data and index >= 0 and self.local_model_combo.itemText(index) == text:
            return str(data)
        return text.split("  (download")[0].strip()

    def _open_install_page(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from ..ai.ollama import INSTALL_PAGE

        QDesktopServices.openUrl(QUrl(INSTALL_PAGE))

    def _pull(self) -> None:
        from ..ai.ollama import pull_model

        name = self._local_model()
        if not name:
            return
        url = self.local_url_edit.text().strip() or PROVIDERS["ollama"].base_url
        self.pull_button.setEnabled(False)
        self.pull_bar.setVisible(True)
        self.pull_bar.setRange(0, 0)
        self._run(
            lambda: pull_model(
                name, url, on_progress=lambda d, t, s: self._pull_progress.emit(d, t, s)
            ),
            done=lambda _: self._pull_done(name),
            failed=self._pull_failed,
        )

    def _show_pull_progress(self, done: int, total: int, status: str) -> None:
        if total:
            self.pull_bar.setRange(0, 1000)
            self.pull_bar.setValue(int(1000 * done / total))
            self.pull_bar.setFormat(f"{status}: {done / 1e9:.1f} of {total / 1e9:.1f} GB")
        else:
            self.pull_bar.setRange(0, 0)
            self.pull_bar.setFormat(status)

    def _pull_done(self, name: str) -> None:
        self.pull_bar.setVisible(False)
        self._refresh_local()
        index = self.local_model_combo.findData(name)
        if index >= 0:
            self.local_model_combo.setCurrentIndex(index)

    def _pull_failed(self, message: str) -> None:
        self.pull_bar.setVisible(False)
        self.pull_button.setEnabled(True)
        QMessageBox.warning(self, "Download failed", message)

    # plan

    def _session(self):
        from ..ai.chatgpt import ChatGPTSession

        return ChatGPTSession()

    def _refresh_plan(self) -> None:
        session = self._session()
        signed_in = session.signed_in()
        self.plan_status.setText(session.status())
        self.sign_in_button.setText("Sign in again" if signed_in else "Sign in with ChatGPT")
        self.sign_out_button.setVisible(signed_in)
        self.plan_models_button.setEnabled(signed_in)

    def _sign_in(self) -> None:
        if self._task is not None and self._task.isRunning():
            self._stop_sign_in = True  # the button reads "Cancel" meanwhile
            return
        self._stop_sign_in = False
        session = self._session()
        self.plan_status.setText(
            "Finish signing in in your browser. This window waits for up to five minutes."
        )
        self.sign_in_button.setText("Cancel")

        def work():
            session.sign_in(should_stop=lambda: self._stop_sign_in)
            return session.list_models()

        self._run(work, done=self._signed_in, failed=self._sign_in_failed)

    def _signed_in(self, models) -> None:
        self._fill_plan_models(models)
        self._refresh_plan()

    def _sign_in_failed(self, message: str) -> None:
        self._refresh_plan()
        if "cancelled" not in message:
            QMessageBox.warning(self, "Sign-in did not finish", message)

    def _sign_out(self) -> None:
        confirmed = self._session().sign_out()
        self._refresh_plan()
        if not confirmed:
            QMessageBox.information(
                self, "Signed out on this computer",
                "The sign-in was removed from this computer, but OpenAI did "
                "not confirm it was revoked. To be sure, disconnect SLR "
                "Framework Studio under Settings in ChatGPT.",
            )

    def _load_plan_models(self) -> None:
        self._run(self._session().list_models, done=self._fill_plan_models,
                  failed=lambda m: QMessageBox.warning(self, "Could not list models", m))

    def _fill_plan_models(self, models) -> None:
        current = self.plan_model_combo.currentText().strip()
        self.plan_model_combo.clear()
        for row in models or []:
            label = row["display_name"]
            if label != row["slug"]:
                label = f"{label} ({row['slug']})"
            self.plan_model_combo.addItem(label, row["slug"])
        index = self.plan_model_combo.findData(current)
        self.plan_model_combo.setCurrentIndex(index if index >= 0 else 0)

    def _plan_model(self) -> str:
        index = self.plan_model_combo.currentIndex()
        text = self.plan_model_combo.currentText().strip()
        if index >= 0 and self.plan_model_combo.itemText(index) == text:
            return str(self.plan_model_combo.itemData(index) or text)
        return text

    # api

    def _api_service_changed(self, *args) -> None:
        spec = PROVIDERS[str(self.provider_combo.currentData())]
        self.model_edit.setPlaceholderText(spec.default_model)
        self.base_url_edit.setPlaceholderText(spec.base_url)
        stored = keys.has_key(spec.key)
        self.key_edit.setPlaceholderText(
            "Leave blank to keep the stored key" if stored else "Paste your API key"
        )
        self.key_status.setText(
            "A key is stored in Windows Credential Manager."
            if stored
            else "No key is stored yet. The provider's website issues one."
        )
        self.forget_button.setEnabled(stored)

    def _forget_key(self) -> None:
        keys.delete_key(str(self.provider_combo.currentData()))
        self.key_edit.clear()
        self._api_service_changed()

    def _save_key(self) -> None:
        if self.choice() != CHOICE_API:
            return
        text = self.key_edit.text().strip()
        if not text:
            return
        try:
            keys.set_key(str(self.provider_combo.currentData()), text)
        except Exception as exc:
            QMessageBox.warning(
                self, "Could not store the key",
                f"{exc}\n\nThe key has not been saved. You can set it as an "
                "environment variable instead.",
            )
            return
        self.key_edit.clear()
        self._api_service_changed()

    # -- background work ----------------------------------------------------

    def _run(self, fn, *, done, failed) -> None:
        from .workers import Task

        task = Task(fn)
        task.succeeded.connect(done)
        task.failed.connect(failed)
        self._task = task
        task.start()

    def done(self, result: int) -> None:  # noqa: D401 - Qt's name
        # Never leave a sign-in waiting on a closed window.
        if self._task is not None and self._task.isRunning():
            self._stop_sign_in = True
            self._task.wait(2000)
        super().done(result)

    # -- testing and saving ---------------------------------------------------

    def _test(self) -> None:
        settings = self.values()
        if settings.offline:
            QMessageBox.information(
                self, "Demonstration screener",
                "The demonstration screener needs no connection. It answers "
                "from keyword rules on this computer.",
            )
            return
        self._save_key()
        try:
            provider = provider_from_settings(settings)
        except Exception as exc:
            QMessageBox.critical(self, "Could not reach the model", str(exc))
            return
        self.test_button.setEnabled(False)
        self.test_button.setText("Testing…")

        def finish(result) -> None:
            self.test_button.setEnabled(True)
            self.test_button.setText("Test connection")
            ok, message = result
            if ok:
                QMessageBox.information(self, "Connected", message)
            else:
                QMessageBox.warning(self, "Could not reach the model", message)

        def crashed(message: str) -> None:
            finish((False, message))

        self._run(provider.check, done=finish, failed=crashed)

    def values(self) -> ModelSettings:
        choice = self.choice()
        if choice == CHOICE_LOCAL:
            provider, model = "ollama", self._local_model()
            base_url = self.local_url_edit.text().strip()
        elif choice == CHOICE_PLAN:
            provider, model, base_url = "chatgpt_subscription", self._plan_model(), ""
        else:
            provider = str(self.provider_combo.currentData())
            model = self.model_edit.text().strip()
            base_url = self.base_url_edit.text().strip()
        return ModelSettings(
            provider=provider,
            model=model,
            base_url=base_url,
            # A spending limit means something only when calls are priced.
            budget_usd=(self.budget_spin.value() or None) if choice == CHOICE_API else None,
            use_cache=self.cache_check.isChecked(),
            offline=self.offline_check.isChecked(),
        )

    def accept(self) -> None:
        if self.choice() == CHOICE_PLAN and not self._session().signed_in() and not self.offline_check.isChecked():
            QMessageBox.information(
                self, "Sign in first",
                "To use your ChatGPT subscription, sign in with ChatGPT first, "
                "or choose another option.",
            )
            return
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
        make_scrollable(self)

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

        if getattr(estimate, "billing", "tokens") == "plan":
            # A subscription has limits, not prices: show those instead of a
            # dollar figure that nobody would pay.
            for line in estimate.plan_limit_lines():
                label = QLabel(line)
                label.setWordWrap(True)
                label.setObjectName("planLimit")
                layout.addWidget(label)
            usage_button = QPushButton("See what is left of my plan")
            usage_button.clicked.connect(_open_usage_page)
            layout.addWidget(usage_button, 0, Qt.AlignmentFlag.AlignLeft)
        else:
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
        make_scrollable(self)


def _open_usage_page() -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    from ..ai.chatgpt import USAGE_PAGE

    QDesktopServices.openUrl(QUrl(USAGE_PAGE))


def confirm_cost(estimate: Any, title: str, parent: QWidget | None = None) -> bool:
    return CostDialog(estimate, title, parent).exec() == QDialog.DialogCode.Accepted
