"""Interface tests for the Retrieve and Full-text pages."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="the desktop interface is optional")

from PySide6.QtWidgets import QApplication  # noqa: E402

from slr_studio.ai import AIClient, MockProvider  # noqa: E402
from slr_studio.ai.settings import ModelSettings, save_settings  # noqa: E402
from slr_studio.core.project import Project  # noqa: E402
from slr_studio.dedup import DedupEngine  # noqa: E402
from slr_studio.importers.service import ImportService  # noqa: E402
from slr_studio.screening import ScreeningEngine, ScreeningSettings  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path, fixtures):
    from slr_studio.ui.main_window import MainWindow

    project = Project.create(
        tmp_path / "review", "UI stage 3", actor="human:test",
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
        contact_email="reviewer@example.org",
    )
    service = ImportService(project)
    service.import_file(fixtures / "scopus_sample.ris")
    service.import_file(fixtures / "wos_sample.ris")
    DedupEngine(project).run()
    project.protocol.lock(actor="human:test")
    save_settings(project, ModelSettings(offline=True))
    screening = ScreeningEngine(
        project, AIClient(project, MockProvider(), stage="screen"), ScreeningSettings()
    )
    screening.waive_pilot("test fixture")
    screening.run_full(actor="human:test")

    win = MainWindow()
    win._set_project(project)
    yield win
    win.close()


def settle(page, app) -> None:
    task = getattr(page, "_task", None)
    if task is not None:
        task.wait(60_000)
    app.processEvents()


@pytest.fixture
def retrieve(window):
    window.nav.setCurrentRow(4)
    return window, window.pages[4]


@pytest.fixture
def fulltext(window):
    window.nav.setCurrentRow(5)
    return window, window.pages[5]


class TestNavigator:
    def test_retrieve_and_fulltext_are_real_pages_now(self, window):
        from slr_studio.ui.pages import FullTextPage, RetrievePage

        assert isinstance(window.pages[4], RetrievePage)
        assert isinstance(window.pages[5], FullTextPage)



class TestRetrievePage:
    def test_it_lists_the_reports_sought(self, retrieve, window):
        _, page = retrieve
        expected = window.project.db.scalar(
            "SELECT COUNT(*) FROM screening_status WHERE decision IN "
            "('include', 'unclear')"
        )
        assert page.record_list.count() == expected
        assert "report(s) sought" in page.status_label.text()

    def test_it_says_plainly_what_it_will_not_do(self, retrieve):
        _, page = retrieve
        from PySide6.QtWidgets import QLabel

        text = " ".join(label.text() for label in page.findChildren(QLabel))
        assert "never downloads from a paywalled site" in text

    def test_without_an_email_the_lookup_asks_for_one_and_saves_it(
        self, retrieve, window, monkeypatch
    ):
        # The button used to sit greyed out, with the reason in small print.
        from PySide6.QtWidgets import QInputDialog

        _, page = retrieve
        window.project.update_info(contact_email="")
        page.refresh()
        assert page.lookup_button.isEnabled() is True
        assert "will ask for" in page.status_label.text()
        asked = []
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(
            lambda parent, title, label, *a, **k: asked.append(label) or ("me@example.org", True)))
        started = []
        monkeypatch.setattr(page, "_start", lambda *a, **k: started.append(k))
        page._lookup()
        assert asked and "Only the records' DOIs and this email" in asked[0]
        assert window.project.info()["contact_email"] == "me@example.org"
        assert started

    def test_cancelling_the_email_question_looks_nothing_up(self, retrieve, window, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        _, page = retrieve
        window.project.update_info(contact_email="")
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
        started = []
        monkeypatch.setattr(page, "_start", lambda *a, **k: started.append(k))
        page._lookup()
        assert not started and window.project.info()["contact_email"] == ""

    def test_dropping_a_pdf_attaches_and_reads_it(self, retrieve, qt_app, fixtures):
        window, page = retrieve
        page.drop.files_dropped.emit([str(fixtures / "pdfs" / "menon_2021_kerala.pdf")])
        settle(page, qt_app)
        assert window.project.db.scalar(
            "SELECT COUNT(*) FROM report WHERE status = 'retrieved'"
        ) == 1
        assert window.project.db.scalar("SELECT COUNT(*) FROM report_page") > 0
        assert "1 retrieved" in page.status_label.text()

    def test_an_unmatched_pdf_goes_to_the_manual_tab(
        self, retrieve, qt_app, fixtures, monkeypatch
    ):
        window, page = retrieve
        monkeypatch.setattr(
            "slr_studio.ui.pages.retrieve_page.QMessageBox.information",
            lambda *a, **k: None,
        )
        page.drop.files_dropped.emit(
            [str(fixtures / "pdfs" / "pereira_2020_scoping_review.pdf")]
        )
        settle(page, qt_app)
        assert page.unmatched_list.count() == 1
        assert "pereira_2020_scoping_review.pdf" in page.unmatched_detail.text()
        assert page.match_button.isEnabled()

    def test_matching_by_hand_attaches_the_file(
        self, retrieve, qt_app, fixtures, monkeypatch
    ):
        window, page = retrieve
        monkeypatch.setattr(
            "slr_studio.ui.pages.retrieve_page.QMessageBox.information",
            lambda *a, **k: None,
        )
        page.drop.files_dropped.emit(
            [str(fixtures / "pdfs" / "pereira_2020_scoping_review.pdf")]
        )
        settle(page, qt_app)
        page.unmatched_list.setCurrentRow(0)
        page.match_combo.setCurrentIndex(0)
        page._match_selected()
        qt_app.processEvents()
        assert page.unmatched_list.count() == 0
        assert window.project.db.scalar(
            "SELECT matched_by FROM report WHERE status = 'retrieved'"
        ) == "manual"

    def test_marking_not_retrieved_requires_a_reason(
        self, retrieve, qt_app, monkeypatch
    ):
        window, page = retrieve
        monkeypatch.setattr(
            "slr_studio.ui.pages.retrieve_page.QInputDialog.getText",
            lambda *a, **k: ("", True),
        )
        page.record_list.setCurrentRow(0)
        page._set_status("not_retrieved")
        qt_app.processEvents()
        assert window.project.db.scalar(
            "SELECT COUNT(*) FROM report WHERE status = 'not_retrieved'"
        ) == 0

    def test_marking_not_retrieved_with_a_reason_is_recorded(
        self, retrieve, qt_app, monkeypatch
    ):
        window, page = retrieve
        monkeypatch.setattr(
            "slr_studio.ui.pages.retrieve_page.QInputDialog.getText",
            lambda *a, **k: ("no copy held by the library", True),
        )
        page.record_list.setCurrentRow(0)
        page._set_status("not_retrieved")
        qt_app.processEvents()
        row = window.project.db.query_one(
            "SELECT status, reason FROM report WHERE status = 'not_retrieved'"
        )
        assert row is not None and "library" in row["reason"]
        assert "1 not retrieved" in page.status_label.text()

    def test_open_access_links_are_shown_for_the_selected_record(
        self, retrieve, window
    ):
        _, page = retrieve
        record_id = window.project.db.scalar(
            "SELECT record_id FROM screening_status WHERE decision IN "
            "('include','unclear') LIMIT 1"
        )
        window.project.db.insert(
            "oa_location",
            {"record_id": record_id, "url": "https://repo.example.org/a.pdf",
             "host_type": "repository", "version": "publishedVersion",
             "license": "cc-by", "is_best": 1, "found_at": "2026-01-01T00:00:00Z"},
        )
        page.refresh()
        index = next(
            i for i, row in enumerate(page._rows) if row["id"] == record_id
        )
        page.record_list.setCurrentRow(index)
        rows = [
            page.links_table.item(r, 0).text()
            for r in range(page.links_table.rowCount())
        ]
        assert rows == ["https://repo.example.org/a.pdf"]


class TestFullTextPage:
    @pytest.fixture
    def assessed(self, window, qt_app, fixtures, monkeypatch):
        retrieve_page = window.pages[4]
        retrieve_page.drop.files_dropped.emit([
            str(fixtures / "pdfs" / "menon_2021_kerala.pdf"),
            str(fixtures / "pdfs" / "okonkwo_2019_benefit_sharing.pdf"),
        ])
        settle(retrieve_page, qt_app)
        window.nav.setCurrentRow(5)
        page = window.pages[5]
        monkeypatch.setattr(
            "slr_studio.ui.pages.fulltext_page.confirm_cost", lambda *a, **k: True
        )
        page._run()
        settle(page, qt_app)
        return window, page

    def test_it_blocks_without_a_locked_protocol(self, qt_app, tmp_path):
        from slr_studio.ui.main_window import MainWindow

        project = Project.create(tmp_path / "bare", "Bare", actor="human:test")
        win = MainWindow()
        win._set_project(project)
        page = win.pages[5]
        page.refresh()
        assert page.blocker_label.isVisibleTo(page) is True
        assert page.run_button.isEnabled() is False
        win.close()

    def test_assessing_fills_the_queue(self, assessed):
        window, page = assessed
        assert page.queue_list.count() == 2
        assert "2 assessed" in page.status_label.text()

    def test_the_assessment_and_its_page_are_shown(self, assessed):
        _, page = assessed
        page.queue_list.setCurrentRow(0)
        assert page.assessment_table.rowCount() >= 1
        assert "page" in page.page_label.text().casefold()
        assert len(page.page_view.toPlainText()) > 50

    def test_a_verified_quotation_says_so(self, assessed):
        _, page = assessed
        page.queue_list.setCurrentRow(0)
        headers = [
            page.assessment_table.horizontalHeaderItem(i).text()
            for i in range(page.assessment_table.columnCount())
        ]
        column = headers.index("Checked")
        assert page.assessment_table.item(0, column).text() == "on the cited page"

    def test_including_a_report_clears_it_from_the_queue(self, assessed, qt_app):
        window, page = assessed
        before = page.queue_list.count()
        page.queue_list.setCurrentRow(0)
        page.note_edit.setText("read it myself")
        page._resolve("include")
        qt_app.processEvents()
        assert page.queue_list.count() == before - 1
        assert window.project.audit.verify() == []

    def test_excluding_without_a_code_is_refused(self, assessed, qt_app, monkeypatch):
        window, page = assessed
        warned = {}
        monkeypatch.setattr(
            "slr_studio.ui.pages.fulltext_page.QMessageBox.warning",
            lambda *a, **k: warned.setdefault("yes", True),
        )
        page.queue_list.setCurrentRow(0)
        page.code_combo.setCurrentIndex(0)  # "— none —"
        page._resolve("exclude")
        qt_app.processEvents()
        assert warned

    def test_the_included_tab_lists_the_studies(self, assessed):
        _, page = assessed
        assert page.included_table.rowCount() == 2

    def test_the_runs_table_records_the_model(self, assessed):
        _, page = assessed
        assert page.runs_table.rowCount() == 1
        assert page.runs_table.item(0, 2).text()

    def test_cancelling_the_cost_dialog_runs_nothing(
        self, window, qt_app, fixtures, monkeypatch
    ):
        retrieve_page = window.pages[4]
        retrieve_page.drop.files_dropped.emit(
            [str(fixtures / "pdfs" / "menon_2021_kerala.pdf")]
        )
        settle(retrieve_page, qt_app)
        page = window.pages[5]
        monkeypatch.setattr(
            "slr_studio.ui.pages.fulltext_page.confirm_cost", lambda *a, **k: False
        )
        before = window.project.db.scalar(
            "SELECT COUNT(*) FROM ai_call WHERE stage = 'fulltext'"
        )
        page._run()
        settle(page, qt_app)
        assert window.project.db.scalar(
            "SELECT COUNT(*) FROM ai_call WHERE stage = 'fulltext'"
        ) == before
