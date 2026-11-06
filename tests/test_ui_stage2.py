"""Interface tests for the protocol controls and the Screen page."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="the desktop interface is optional")

from PySide6.QtWidgets import QApplication, QDialog, QLabel  # noqa: E402

from slr_studio.ai.settings import ModelSettings, save_settings  # noqa: E402
from slr_studio.core.project import Project  # noqa: E402
from slr_studio.dedup import DedupEngine  # noqa: E402
from slr_studio.importers.service import ImportService  # noqa: E402
from slr_studio.ui.dialogs import CostDialog, ModelSettingsDialog, ProtocolDialog  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path, fixtures):
    from slr_studio.ui.main_window import MainWindow

    project = Project.create(
        tmp_path / "review", "UI stage 2", actor="human:test",
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    service = ImportService(project)
    service.import_file(fixtures / "scopus_sample.ris")
    service.import_file(fixtures / "wos_sample.ris")
    DedupEngine(project).run()
    # The offline screener keeps these tests free of any network.
    save_settings(project, ModelSettings(offline=True))
    win = MainWindow()
    win._set_project(project)
    yield win
    win.close()


def settle(page, app) -> None:
    task = getattr(page, "_task", None)
    if task is not None:
        task.wait(60_000)
    app.processEvents()


class TestProtocolControls:
    def test_an_unlocked_protocol_says_so(self, window):
        page = window.pages[0]
        assert "has not been locked" in page.protocol_label.text()
        assert page.lock_button.isEnabled()

    def test_locking_updates_the_page_and_writes_the_file(self, window, monkeypatch):
        monkeypatch.setattr(ProtocolDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        page = window.pages[0]
        page._lock_protocol()
        assert "Version 1" in page.protocol_label.text()
        assert page.lock_button.text() == "Protocol locked"
        assert page.lock_button.isEnabled() is False
        assert (window.project.exports_dir / "protocol-v1.md").is_file()

    def test_editing_the_form_offers_an_amendment(self, window, monkeypatch):
        monkeypatch.setattr(ProtocolDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        page = window.pages[0]
        page._lock_protocol()
        page.objectives_edit.setPlainText("A narrower objective")
        page._save()
        assert page.lock_button.text() == "Amend protocol"
        assert page.lock_button.isEnabled()

    def test_an_amendment_appears_in_the_log_table(self, window, monkeypatch):
        monkeypatch.setattr(ProtocolDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(ProtocolDialog, "reason", lambda self: "Reviewer asked")
        page = window.pages[0]
        page._lock_protocol()
        page.objectives_edit.setPlainText("A narrower objective")
        page._save()
        page._lock_protocol()
        assert page.amendments_table.rowCount() == 1
        assert page.amendments_table.item(0, 2).text() == "Reviewer asked"

    def test_the_amendment_table_is_hidden_until_there_is_one(self, window):
        assert window.pages[0].amendments_table.isVisibleTo(window.pages[0]) is False

    def test_an_amendment_after_an_import_warns_in_the_dialog(self, window):
        dialog = ProtocolDialog(
            "draft", is_amendment=True, after_data_seen=True, parent=window
        )
        text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "after the data were seen" in text

    def test_an_amendment_without_a_reason_is_refused(self, window, monkeypatch):
        dialog = ProtocolDialog("draft", is_amendment=True, parent=window)
        warned = {}
        monkeypatch.setattr(
            "slr_studio.ui.dialogs.QMessageBox.warning",
            lambda *a, **k: warned.setdefault("yes", True),
        )
        dialog.accept()
        assert warned and dialog.result() != QDialog.DialogCode.Accepted


class TestScreenPage:
    def test_it_blocks_until_the_protocol_is_locked(self, window):
        page = window.pages[3]
        window.nav.setCurrentRow(3)
        assert page.blocker_label.isVisibleTo(page) is True
        assert page.run_button.isEnabled() is False

    @pytest.fixture
    def ready(self, window, monkeypatch):
        monkeypatch.setattr(ProtocolDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
        window.pages[0]._lock_protocol()
        window.nav.setCurrentRow(3)
        return window, window.pages[3]

    def test_locking_unblocks_the_buttons(self, ready):
        _, page = ready
        assert page.run_button.isEnabled() is True
        assert page.blocker_label.isVisibleTo(page) is False

    def test_the_pilot_tab_lists_a_reproducible_sample(self, ready):
        _, page = ready
        assert page.pilot_list.count() == len(page._pilot_records)
        assert "0 of" in page.pilot_progress_label.text()

    def test_labelling_a_pilot_record_marks_it(self, ready, qt_app):
        _, page = ready
        page.pilot_list.setCurrentRow(0)
        page._label_pilot("include")
        qt_app.processEvents()
        assert page.pilot_list.item(0).text().startswith("[include]")
        assert "1 of" in page.pilot_progress_label.text()

    def test_screening_fills_the_queue_and_the_counts(self, ready, qt_app, monkeypatch):
        window, page = ready
        monkeypatch.setattr(
            "slr_studio.ui.pages.screen_page.confirm_cost", lambda *a, **k: True
        )
        page._engine().waive_pilot("test")
        page._run_full()
        settle(page, qt_app)
        assert "not yet screened" in page.status_label.text()
        assert page.queue_list.count() > 0
        assert window.project.db.scalar(
            "SELECT COUNT(*) FROM screening_status"
        ) > 0

    def test_cancelling_the_cost_dialog_runs_nothing(self, ready, qt_app, monkeypatch):
        window, page = ready
        monkeypatch.setattr(
            "slr_studio.ui.pages.screen_page.confirm_cost", lambda *a, **k: False
        )
        page._engine().waive_pilot("test")
        page._run_full()
        settle(page, qt_app)
        assert window.project.db.scalar("SELECT COUNT(*) FROM ai_call") == 0

    @pytest.fixture
    def screened(self, ready, qt_app, monkeypatch):
        window, page = ready
        monkeypatch.setattr(
            "slr_studio.ui.pages.screen_page.confirm_cost", lambda *a, **k: True
        )
        page._engine().waive_pilot("test")
        page._run_full()
        settle(page, qt_app)
        return window, page

    def test_the_queue_shows_what_each_reviewer_said(self, screened):
        _, page = screened
        page.queue_list.setCurrentRow(0)
        assert page.decisions_table.rowCount() >= 2
        reviewers = {
            page.decisions_table.item(r, 0).text()
            for r in range(page.decisions_table.rowCount())
        }
        assert {"Screener A", "Screener B"} <= reviewers

    def test_a_human_decision_clears_the_item(self, screened, qt_app):
        window, page = screened
        before = page.queue_list.count()
        page.queue_list.setCurrentRow(0)
        page.note_edit.setText("read it myself")
        page._resolve("include")
        qt_app.processEvents()
        assert page.queue_list.count() == before - 1
        assert window.project.audit.verify() == []

    def test_the_agreement_tab_reports_kappa_and_pabak(self, screened):
        _, page = screened
        headers = [
            page.agreement_table.horizontalHeaderItem(i).text()
            for i in range(page.agreement_table.columnCount())
        ]
        assert "κ" in headers and "PABAK" in headers
        assert page.agreement_table.rowCount() >= 1

    def test_the_runs_table_records_the_model(self, screened):
        _, page = screened
        assert page.runs_table.rowCount() >= 1
        assert page.runs_table.item(0, 3).text()

    def test_exporting_the_log_writes_a_file(self, screened, monkeypatch):
        window, page = screened
        monkeypatch.setattr(
            "slr_studio.ui.pages.screen_page.QMessageBox.information",
            lambda *a, **k: None,
        )
        page._export()
        assert (window.project.exports_dir / "screening-log.xlsx").is_file()


class TestDialogs:
    def test_the_cost_dialog_states_the_estimate(self, qt_app):
        from slr_studio.ai.client import CostEstimate

        estimate = CostEstimate(
            n_calls=100, prompt_tokens=50_000, completion_tokens=16_000,
            cost_usd=0.42, price_known=True, model="test-model",
        )
        dialog = CostDialog(estimate, "Screen all records")
        text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "100 calls" in text
        assert "US$0.42" in text
        assert "estimate" in text

    def test_an_unpriced_model_is_not_shown_as_free(self, qt_app):
        from slr_studio.ai.client import CostEstimate

        estimate = CostEstimate(
            n_calls=10, prompt_tokens=100, completion_tokens=100,
            cost_usd=0.0, price_known=False, model="brand-new",
        )
        dialog = CostDialog(estimate, "Screen")
        text = " ".join(label.text() for label in dialog.findChildren(QLabel))
        assert "cannot be estimated" in text
        assert "US$0.00" not in text

    def test_the_model_dialog_marks_a_local_model_as_staying_put(self, qt_app):
        dialog = ModelSettingsDialog(ModelSettings(provider="ollama"))
        assert "nothing is sent anywhere" in dialog.note.text()

    def test_the_model_dialog_says_when_data_leaves(self, qt_app):
        dialog = ModelSettingsDialog(ModelSettings(provider="anthropic"))
        assert "sent to this provider" in dialog.note.text()

    def test_the_api_key_field_is_masked(self, qt_app):
        from PySide6.QtWidgets import QLineEdit

        dialog = ModelSettingsDialog(ModelSettings())
        assert dialog.key_edit.echoMode() == QLineEdit.EchoMode.Password

    def test_choosing_a_local_model_disables_the_key_field(self, qt_app):
        dialog = ModelSettingsDialog(ModelSettings(provider="anthropic"))
        index = dialog.provider_combo.findData("ollama")
        dialog.provider_combo.setCurrentIndex(index)
        assert dialog.key_edit.isEnabled() is False
