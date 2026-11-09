"""Pop-ups scroll instead of running off the screen."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QMessageBox, QScrollArea  # noqa: E402

from slr_studio.ai.settings import ModelSettings  # noqa: E402
from slr_studio.ui import scrolling  # noqa: E402
from slr_studio.ui.dialogs import CostDialog, ModelSettingsDialog  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


def test_a_dialog_body_scrolls_and_its_buttons_stay_outside(app):
    dialog = ModelSettingsDialog(ModelSettings(provider="ollama"))
    area = dialog.findChild(QScrollArea)
    assert area is not None and area.widget().isAncestorOf(dialog.radio_local)
    buttons = dialog.findChild(QDialogButtonBox)
    assert not area.widget().isAncestorOf(buttons)


def test_no_dialog_opens_taller_than_the_screen(app):
    dialog = ModelSettingsDialog(ModelSettings(provider="ollama"))
    dialog.more_button.setChecked(True)
    assert dialog.height() <= scrolling._screen_height(dialog)


def test_the_cost_screen_scrolls_too(app):
    from slr_studio.ai.client import CostEstimate

    estimate = CostEstimate(n_calls=1, prompt_tokens=1, completion_tokens=1,
                            cost_usd=0.0, price_known=True, model="m")
    assert CostDialog(estimate, "Run").findChild(QScrollArea) is not None


def test_a_long_message_scrolls_and_returns_the_button_pressed(app, monkeypatch):
    scrolling.install_scrolling_messages()
    text = "\n\n".join(f"Paragraph {i} of a long explanation." for i in range(40))
    seen = {}

    def press_yes():
        for widget in app.topLevelWidgets():
            if widget.isVisible() and widget.windowTitle() == "Long":
                seen["scrolls"] = widget.findChild(QScrollArea) is not None
                box = widget.findChild(QDialogButtonBox)
                box.button(QDialogButtonBox.StandardButton.Yes).click()

    QTimer.singleShot(200, press_yes)
    answer = QMessageBox.question(None, "Long", text)
    assert seen["scrolls"]
    assert answer == QMessageBox.StandardButton.Yes


def test_closing_a_long_question_answers_no(app):
    scrolling.install_scrolling_messages()
    text = "x" * 2000

    def close():
        for widget in app.topLevelWidgets():
            if widget.isVisible() and widget.windowTitle() == "Close me":
                widget.reject()

    QTimer.singleShot(200, close)
    assert QMessageBox.question(None, "Close me", text) == QMessageBox.StandardButton.No


def test_short_messages_keep_the_ordinary_box(app, monkeypatch):
    calls = []
    monkeypatch.setitem(scrolling._ORIGINAL, "information", lambda *a, **k: calls.append(a))
    scrolling.install_scrolling_messages()
    QMessageBox.information(None, "Short", "Saved.")
    assert calls and calls[0][2] == "Saved."
