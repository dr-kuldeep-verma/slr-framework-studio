"""Interface tests, run offscreen.

They drive the window the way a reviewer does -- dropping files on the import
area, pressing Run deduplication, deciding a pair -- rather than calling the
services underneath, because the wiring between the two is exactly what the
other test files cannot catch.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="the desktop interface is optional")

from PySide6.QtWidgets import QApplication  # noqa: E402

from slr_studio.core.project import Project  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qt_app, tmp_path):
    from slr_studio.ui.main_window import MainWindow

    win = MainWindow()
    project = Project.create(tmp_path / "review", "UI review", actor="human:test")
    win._set_project(project)
    yield win
    win.close()


def _settle(page, app) -> None:
    """Wait for a page's background task, then let its signals be delivered."""
    task = getattr(page, "_task", None)
    if task is not None:
        task.wait(30_000)
    app.processEvents()


class TestWindow:
    def test_every_stage_is_in_the_navigator(self, window):
        """The navigator shows the declared stages, all of them, in order.

        The literal list is pinned against the spec text in
        test_spec_conformance; here the point is that nothing is dropped
        between the declaration and the window.
        """
        from slr_studio import STAGES

        labels = [window.nav.item(i).text() for i in range(window.nav.count())]
        assert labels == [label for _, label in STAGES]

    def test_the_navigator_and_the_stack_stay_in_step(self, window):
        window.nav.setCurrentRow(2)
        assert window.stack.currentIndex() == 2
        assert window.stack.currentWidget().title == "Deduplicate"


    def test_a_placeholder_shows_every_line_of_its_text(self, window):
        """Wrapped prose must not be cut off at the bottom of its label.

        A word-wrapped QLabel reports the height it would need at its
        parent's full width, so inside a narrow centred column the last
        lines fall outside the widget and are silently never drawn. The
        Gap Finder's summary is the longest, which is why it is the one
        that found this.
        """
        from PySide6.QtWidgets import QLabel

        from slr_studio import STAGES

        window.resize(1180, 760)
        window.show()
        window.nav.setCurrentRow([label for _, label in STAGES].index("Gap Finder"))
        QApplication.processEvents()

        page = window.stack.currentWidget()
        assert page.title == "Gap Finder"
        for label in page.findChildren(QLabel):
            if not label.wordWrap() or not label.text():
                continue
            needed = label.heightForWidth(label.width())
            assert label.height() >= needed, (
                f"{label.text()[:40]!r} needs {needed}px, has {label.height()}px"
            )

    def test_synthesise_is_a_working_page_not_a_placeholder(self, window):
        """Build stage 5 replaces the placeholder with the real stage."""
        from slr_studio.ui.pages import PlaceholderPage, SynthesisePage
        from slr_studio import STAGES

        page = window.pages[[label for _, label in STAGES].index("Synthesise")]
        assert isinstance(page, SynthesisePage)
        assert not isinstance(page, PlaceholderPage)

    def test_synthesise_needs_a_project_open(self, window):
        from slr_studio import STAGES

        page = window.pages[[label for _, label in STAGES].index("Synthesise")]
        assert page.requires_project() is True

    def test_framework_is_a_working_page_not_a_placeholder(self, window):
        """Build stage 6 replaces the placeholder with the real stage."""
        from slr_studio import STAGES
        from slr_studio.ui.pages import FrameworkPage, PlaceholderPage

        page = window.pages[[label for _, label in STAGES].index("Framework")]
        assert isinstance(page, FrameworkPage)
        assert not isinstance(page, PlaceholderPage)

    def test_framework_needs_a_project_open(self, window):
        from slr_studio import STAGES

        page = window.pages[[label for _, label in STAGES].index("Framework")]
        assert page.requires_project() is True

    def test_the_gap_finder_is_a_working_page_not_a_placeholder(self, window):
        """Build stage 7 replaces the placeholder with the real stage."""
        from slr_studio import STAGES
        from slr_studio.ui.pages import GapFinderPage, PlaceholderPage

        page = window.pages[[label for _, label in STAGES].index("Gap Finder")]
        assert isinstance(page, GapFinderPage)
        assert not isinstance(page, PlaceholderPage)

    def test_the_gap_finder_needs_a_project_open(self, window):
        from slr_studio import STAGES

        page = window.pages[[label for _, label in STAGES].index("Gap Finder")]
        assert page.requires_project() is True

    def test_export_is_a_working_page_not_a_placeholder(self, window):
        """Build stage 8 replaces the last placeholder."""
        from slr_studio import STAGES
        from slr_studio.ui.pages import ExportPage, PlaceholderPage

        page = window.pages[[label for _, label in STAGES].index("Export")]
        assert isinstance(page, ExportPage)
        assert not isinstance(page, PlaceholderPage)

    def test_every_stage_is_now_built(self, window):
        """The last one. There is no placeholder left in the navigator."""
        from slr_studio.ui.pages import PlaceholderPage

        assert not [p for p in window.pages if isinstance(p, PlaceholderPage)]

    def test_the_header_wraps_rather_than_truncating(self, window):
        assert window.header.wordWrap() is True

    def test_closing_a_project_disables_the_working_pages(self, window):
        from slr_studio.ui.pages import PlaceholderPage

        window._set_project(None)
        for page in window.pages:
            if isinstance(page, PlaceholderPage):
                # A stage that is not built yet stays readable with nothing open.
                assert page.isEnabled() is True, page.title
            elif page.requires_project():
                assert page.isEnabled() is False, page.title

    def test_the_status_bar_shows_prisma_counts(self, window):
        assert "identified 0" in window.counts_label.text()


class TestImportPage:
    def test_dropping_files_imports_them(self, window, qt_app, fixtures):
        page = window.pages[1]
        page.drop.files_dropped.emit(
            [str(fixtures / "scopus_sample.ris"), str(fixtures / "wos_sample.ris")]
        )
        _settle(page, qt_app)
        assert page.files_table.rowCount() == 2
        assert page.db_table.rowCount() == 2
        assert window.project.counts()["n_identified"] == 12

    def test_the_file_table_reports_missing_abstracts(self, window, qt_app, fixtures):
        page = window.pages[1]
        page.drop.files_dropped.emit([str(fixtures / "scopus_sample.ris")])
        _settle(page, qt_app)
        headers = [
            page.files_table.horizontalHeaderItem(i).text()
            for i in range(page.files_table.columnCount())
        ]
        column = headers.index("No abstract")
        assert page.files_table.item(0, column).text() == "1"

    def test_a_seed_is_added_and_reported_as_missing(self, window, qt_app):
        page = window.pages[1]
        page.seed_edit.setText("10.9999/never-published.2020")
        page._add_seed()
        qt_app.processEvents()
        assert page.seed_table.rowCount() == 1
        assert page.seed_table.item(0, 1).text() == "NOT FOUND"

    def test_a_seed_found_after_import_flips_to_yes(self, window, qt_app, fixtures):
        page = window.pages[1]
        page.seed_edit.setText("10.1080/jsts.2021.004512")
        page._add_seed()
        page.drop.files_dropped.emit([str(fixtures / "scopus_sample.ris")])
        _settle(page, qt_app)
        assert page.seed_table.item(0, 1).text() == "yes"

    def test_the_drop_area_only_accepts_known_extensions(self, window):
        page = window.pages[1]
        accepted = page.drop._accepted(["a.ris", "b.nbib", "c.docx", "d.csv"])
        assert accepted == ["a.ris", "b.nbib", "d.csv"]


class TestDedupPage:
    @pytest.fixture
    def loaded(self, window, qt_app, fixtures):
        page = window.pages[1]
        page.drop.files_dropped.emit(
            [str(fixtures / "scopus_sample.ris"), str(fixtures / "wos_sample.ris")]
        )
        _settle(page, qt_app)
        window.nav.setCurrentRow(2)
        return window, window.pages[2]

    def test_before_running_it_says_so(self, loaded):
        _, page = loaded
        assert "Not deduplicated yet" in page.summary_label.text()

    def test_running_fills_the_summary_and_the_merge_table(self, loaded, qt_app):
        window, page = loaded
        page._run()
        _settle(page, qt_app)
        assert "3 duplicates removed" in page.summary_label.text()
        assert "9 to screen" in page.summary_label.text()
        assert page.merged_table.rowCount() == 3

    def test_the_review_queue_shows_the_undecided_pair(self, loaded, qt_app):
        _, page = loaded
        page._run()
        _settle(page, qt_app)
        assert page.pair_list.count() == 1
        assert "conflicting DOIs" in page.evidence_label.text()

    def test_both_sides_of_the_pair_are_rendered(self, loaded, qt_app):
        _, page = loaded
        page._run()
        _settle(page, qt_app)
        assert "Measuring empowerment" in page.left_view.toPlainText()
        assert "Measuring empowerment" in page.right_view.toPlainText()
        assert "10.5281/zenodo.2020201" in page.left_view.toPlainText()

    def test_keeping_both_empties_the_queue_and_leaves_the_counts(self, loaded, qt_app):
        window, page = loaded
        page._run()
        _settle(page, qt_app)
        page.reason_edit.setText("conference paper, then journal article")
        page._decide(False)
        qt_app.processEvents()
        assert page.pair_list.count() == 0
        assert window.project.counts()["n_duplicates_removed"] == 3

    def test_merging_the_pair_increases_the_duplicate_count(self, loaded, qt_app):
        window, page = loaded
        page._run()
        _settle(page, qt_app)
        page._decide(True)
        qt_app.processEvents()
        assert window.project.counts()["n_duplicates_removed"] == 4

    def test_the_decision_reaches_the_audit_log(self, loaded, qt_app):
        window, page = loaded
        page._run()
        _settle(page, qt_app)
        page.reason_edit.setText("different reports")
        page._decide(False)
        qt_app.processEvents()
        assert any(
            e.action == "human_override" for e in window.project.audit.entries()
        )
        assert window.project.audit.verify() == []

    def test_raising_the_threshold_before_running_changes_the_outcome(
        self, loaded, qt_app
    ):
        _, page = loaded
        page.threshold.setValue(1.00)
        page.auto_threshold.setValue(1.00)
        page._run()
        _settle(page, qt_app)
        assert page.merged_table.rowCount() == 2


class TestProjectPage:
    def test_the_exclusion_ladder_is_shown_in_order(self, window):
        page = window.pages[0]
        assert page.codes_table.rowCount() == 8
        assert page.codes_table.item(0, 0).text() == "E1"

    def test_saving_the_form_writes_through_to_the_project(self, window, qt_app):
        page = window.pages[0]
        page.title_edit.setText("A renamed review")
        page.questions_edit.setPlainText("How does participation work?\nWhat helps?")
        page.framework_combo.setCurrentText("SPIDER")
        page._save()
        qt_app.processEvents()
        info = window.project.info()
        assert info["title"] == "A renamed review"
        assert info["framework_type"] == "SPIDER"
        assert len(info["research_questions"]) == 2

    def test_the_protocol_can_be_locked_and_says_what_that_does(self, window):
        page = window.pages[0]
        assert page.lock_button.isEnabled() is True
        assert "SHA-256" in page.lock_button.toolTip()
        assert "has not been locked" in page.protocol_label.text()


class TestTheDemoMenuItem:
    """The menu item a first-time reviewer uses before they have a key.

    Every line of it calls something on the window, and a typo in an
    attribute name is invisible until someone clicks it. These call the
    handler for real.
    """

    def test_the_tools_menu_offers_it(self, window):
        texts = [
            action.text().replace("&", "")
            for top in window.menuBar().actions()
            if top.menu() is not None
            for action in top.menu().actions()
        ]
        assert any("demonstration" in text.casefold() for text in texts), texts

    def test_the_handler_exists_and_is_callable(self, window):
        assert callable(window.build_demo)

    def test_it_does_nothing_when_no_folder_is_chosen(self, window, monkeypatch):
        """The cancel path must not touch anything that does not exist."""
        from PySide6.QtWidgets import QFileDialog

        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: "")
        )
        window.build_demo()

    def test_it_refuses_a_folder_that_is_not_empty(
        self, window, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        (tmp_path / "something.txt").write_text("not empty")
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: str(tmp_path)),
        )
        warned = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: warned.append(a)),
        )
        window.build_demo()
        assert warned, "an occupied folder has to be refused"

    def test_declining_the_confirmation_builds_nothing(
        self, window, monkeypatch, tmp_path
    ):
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        target = tmp_path / "empty"
        target.mkdir()
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            staticmethod(lambda *a, **k: str(target)),
        )
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
        )
        window.build_demo()
        assert not any(target.iterdir())
