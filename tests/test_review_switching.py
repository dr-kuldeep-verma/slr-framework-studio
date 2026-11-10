"""Opening a review reaches every page, and nothing goes online to refresh."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from slr_studio.ai.base import ProviderError  # noqa: E402
from slr_studio.ai.settings import ModelSettings, provider_from_settings, save_settings  # noqa: E402
from slr_studio.core.project import Project  # noqa: E402


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_network(monkeypatch):
    def refuse(self):
        raise AssertionError("went online to refresh a page")

    monkeypatch.setattr("slr_studio.ai.chatgpt.ChatGPTSession.list_models", refuse)


def test_a_chatgpt_review_without_a_model_builds_offline_and_says_so_when_asked(no_network):
    provider = provider_from_settings(ModelSettings(provider="chatgpt_subscription", model=""))
    with pytest.raises(ProviderError, match="No ChatGPT model is chosen") as caught:
        provider.complete(system="s", user="u")
    assert caught.value.retryable is False


def test_opening_a_review_reaches_every_page_even_if_one_fails(app, tmp_path, monkeypatch, no_network):
    from slr_studio.ui.main_window import MainWindow
    from slr_studio.ui.pages import ImportPage, SynthesisePage

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: warnings.append(a[2])))
    monkeypatch.setattr("slr_studio.errorlog.log_path", lambda: tmp_path / "log.txt")

    window = MainWindow()
    first = Project.create(tmp_path / "first", "First")
    window._set_project(first)

    def broken(self):
        raise RuntimeError("this page could not refresh")

    monkeypatch.setattr(SynthesisePage, "refresh", broken)
    second = Project.create(tmp_path / "second", "Second")
    save_settings(second, ModelSettings(provider="chatgpt_subscription", model=""))
    window._set_project(second)

    assert all(page.project is second for page in window.pages)
    assert warnings and "could not refresh" in warnings[0]
    assert "this page could not refresh" in (tmp_path / "log.txt").read_text()
    # And the import on the page the reviewer uses next works.
    page = next(p for p in window.pages if isinstance(p, ImportPage))
    assert page.project.counts()["n_identified"] == 0


def test_saving_the_subscription_without_a_model_is_refused(app, monkeypatch):
    from slr_studio.ui.dialogs import ModelSettingsDialog

    shown = []
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: shown.append(a[1])))
    monkeypatch.setattr("slr_studio.ai.chatgpt.ChatGPTSession.signed_in", lambda self: True)
    monkeypatch.setattr("slr_studio.ai.chatgpt.ChatGPTSession.status", lambda self: "Signed in.")
    monkeypatch.setattr(
        "slr_studio.ui.dialogs.ModelSettingsDialog._load_plan_models", lambda self: None
    )
    dialog = ModelSettingsDialog(ModelSettings(provider="chatgpt_subscription", model=""))
    dialog.accept()
    assert shown == ["Choose a model"]
    assert dialog.result() == 0


def test_the_error_log_keeps_the_traceback(tmp_path, monkeypatch):
    from slr_studio import errorlog

    monkeypatch.setattr(errorlog, "log_path", lambda: tmp_path / "log.txt")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        errorlog.log_exception(exc, where="a test")
    text = (tmp_path / "log.txt").read_text()
    assert "a test" in text and "ValueError: boom" in text and "Traceback" in text
