"""Interface tests for the Extract page."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="the desktop interface is optional")

from PySide6.QtWidgets import QApplication  # noqa: E402

from slr_studio.ai.settings import ModelSettings, save_settings  # noqa: E402
from slr_studio.core.project import Project  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    """A window over a demo project taken through extraction."""
    from slr_studio.demo import build_demo_project
    from slr_studio.ui.main_window import MainWindow

    project = build_demo_project(tmp_path / "review", extract=True)
    save_settings(project, ModelSettings(offline=True))
    win = MainWindow()
    win._set_project(project)
    yield win
    win.close()


@pytest.fixture
def extract(window):
    window.nav.setCurrentRow(6)
    page = window.pages[6]
    page.refresh()
    return window, page


def settle(page, app) -> None:
    task = getattr(page, "_task", None)
    if task is not None:
        task.wait(60_000)
    app.processEvents()


class TestNavigator:
    def test_extract_is_a_real_page_now(self, window):
        from slr_studio.ui.pages import ExtractPage

        assert isinstance(window.pages[6], ExtractPage)



class TestExtractPage:
    def test_it_reports_what_has_been_extracted(self, extract):
        _, page = extract
        assert "relationship(s)" in page.status_label.text()
        assert "extracted" in page.status_label.text()

    def test_the_inventory_lists_every_relationship(self, extract, window):
        _, page = extract
        expected = window.project.db.scalar(
            "SELECT COUNT(*) FROM relationship WHERE coder_role = 'ai'"
        )
        assert page.inventory_table.rowCount() == expected > 0

    def test_the_inventory_shows_the_authors_labels(self, extract, window):
        _, page = extract
        headers = [
            page.inventory_table.horizontalHeaderItem(i).text()
            for i in range(page.inventory_table.columnCount())
        ]
        column = headers.index("Predictor") + 1
        shown = {
            page.inventory_table.item(r, column - 1).text()
            for r in range(page.inventory_table.rowCount())
        }
        stored = {
            r["predictor_label"]
            for r in window.project.db.query(
                "SELECT predictor_label FROM relationship WHERE coder_role = 'ai'"
            )
        }
        assert shown == stored

    def test_it_blocks_without_a_locked_protocol(self, qt_app, tmp_path):
        from slr_studio.ui.main_window import MainWindow

        project = Project.create(tmp_path / "bare", "Bare", actor="human:test")
        win = MainWindow()
        win._set_project(project)
        page = win.pages[6]
        page.refresh()
        assert page.blocker_label.isVisibleTo(page) is True
        assert page.run_button.isEnabled() is False
        win.close()

    def test_the_second_coding_sample_is_listed(self, extract, window):
        _, page = extract
        expected = window.project.db.scalar(
            "SELECT COUNT(*) FROM second_coding_sample"
        )
        assert page.sample_table.rowCount() == expected > 0

    def test_exporting_the_matrix_writes_a_file(self, extract, window, monkeypatch):
        _, page = extract
        monkeypatch.setattr(
            "slr_studio.ui.pages.extract_page.QMessageBox.information",
            lambda *a, **k: None,
        )
        page._export()
        assert (window.project.exports_dir / "extraction-matrix.xlsx").is_file()

    def test_comparing_coders_with_none_coded_says_so(self, extract, monkeypatch):
        _, page = extract
        shown = {}
        monkeypatch.setattr(
            "slr_studio.ui.pages.extract_page.QMessageBox.information",
            lambda parent, title, text, *a, **k: shown.setdefault("text", text),
        )
        page._compare()
        assert "No report has been coded twice" in shown["text"]


class TestReviewingAFlaggedExtraction:
    """A direction the blind check disagreed with, reviewed on the page."""

    @pytest.fixture
    def flagged(self, extract, window, qt_app):
        # Turn one recorded direction around so the stored check disagrees,
        # then re-open the queue the way a run would have left it.
        project = window.project
        row = project.db.query_one(
            "SELECT id, record_id, predictor_label, outcome_label FROM relationship "
            "WHERE coder_role = 'ai' AND quote <> '' LIMIT 1"
        )
        project.db.execute(
            "UPDATE relationship SET direction = 'negative', verified_direction = "
            "'positive', verify_agrees = 0, needs_review = 1, review_reason = "
            "'the extraction recorded a negative direction, but the quotation "
            "alone reads as positive' WHERE id = ?",
            (row["id"],),
        )
        project.db.insert(
            "review_queue",
            {
                "record_id": row["record_id"],
                "reason": "direction_mismatch",
                "detail": f"{row['predictor_label']} → {row['outcome_label']}: "
                          "the check read it the other way",
                "stage": "extract",
                "created_at": "2026-03-01T09:00:00Z",
            },
        )
        _, page = extract
        page.refresh()
        qt_app.processEvents()
        return window, page, row["id"]

    def test_the_queue_shows_the_flagged_report(self, flagged):
        _, page, _ = flagged
        assert page.queue_list.count() == 1
        assert "Direction disagreed" in page.queue_list.item(0).text()

    def test_the_relationships_are_shown_with_the_check(self, flagged, qt_app):
        _, page, _ = flagged
        page.queue_list.setCurrentRow(0)
        qt_app.processEvents()
        assert page.relationship_table.rowCount() > 0
        headers = [
            page.relationship_table.horizontalHeaderItem(i).text()
            for i in range(page.relationship_table.columnCount())
        ]
        column = headers.index("Check")
        states = {
            page.relationship_table.item(r, column).text()
            for r in range(page.relationship_table.rowCount())
        }
        # A disagreement shows the direction the check read instead of
        # "agrees", so the two are distinguishable at a glance.
        assert "agrees" in states
        assert "positive" in states

    def test_changing_a_direction_is_recorded_as_an_override(
        self, flagged, qt_app, monkeypatch
    ):
        window, page, relationship_id = flagged
        monkeypatch.setattr(
            "slr_studio.ui.pages.extract_page.QInputDialog.getText",
            lambda *a, **k: ("the results table says positive", True),
        )
        page.queue_list.setCurrentRow(0)
        qt_app.processEvents()
        row = next(
            index for index, rel in enumerate(page._relationships)
            if rel["id"] == relationship_id
        )
        page.relationship_table.selectRow(row)
        page._select_relationship(row)
        index = page.direction_combo.findData("positive")
        page.direction_combo.setCurrentIndex(index)
        page._apply_direction()
        qt_app.processEvents()
        assert window.project.db.scalar(
            "SELECT direction FROM relationship WHERE id = ?", (relationship_id,)
        ) == "positive"
        assert window.project.db.query_one(
            "SELECT * FROM human_override WHERE entity = 'relationship' "
            "AND entity_id = ?",
            (str(relationship_id),),
        ) is not None
        assert window.project.audit.verify() == []

    def test_accepting_the_report_clears_the_queue(self, flagged, qt_app):
        window, page, _ = flagged
        page.queue_list.setCurrentRow(0)
        page._accept()
        qt_app.processEvents()
        assert page.queue_list.count() == 0

    def test_withdrawing_a_relationship_removes_it_from_the_inventory(
        self, flagged, qt_app, monkeypatch
    ):
        window, page, relationship_id = flagged
        monkeypatch.setattr(
            "slr_studio.ui.pages.extract_page.QInputDialog.getText",
            lambda *a, **k: ("the paper states no such link", True),
        )
        before = page.inventory_table.rowCount()
        page.queue_list.setCurrentRow(0)
        qt_app.processEvents()
        row = next(
            index for index, rel in enumerate(page._relationships)
            if rel["id"] == relationship_id
        )
        page.relationship_table.selectRow(row)
        page._select_relationship(row)
        page._withdraw()
        qt_app.processEvents()
        assert page.inventory_table.rowCount() == before - 1
