"""Layout tests: what the reviewer can actually see and reach.

These exist because a window that reports the right numbers is still broken
if the fields are a clipped line tall and the bottom of the page is behind
the status bar with no way to scroll to it. Both of those shipped in the
first Windows build, and neither was caught by a single one of the 1,328
tests that passed on the machine it was built on, because every one of them
read a widget's value rather than its geometry.

So each test here sets a real window size, shows the window, and then asks
whether a human sitting in front of it could read and reach the thing.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="the desktop interface is optional")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QPlainTextEdit,
    QScrollArea,
)

from slr_studio.core.project import Project  # noqa: E402


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    from slr_studio.ui.main_window import MainWindow

    win = MainWindow()
    project = Project.create(tmp_path / "review", "Layout review", actor="human:test")
    win._set_project(project)
    yield win
    win.close()


def _scroll_area(page) -> QScrollArea:
    areas = [
        area
        for area in page.findChildren(QScrollArea)
        if area.parent() is page
    ]
    assert areas, f"{page.title} has no scroll area of its own"
    return areas[0]


class TestScrolling:
    def test_every_stage_page_scrolls(self, window, qt_app):
        """A page taller than the window must be reachable by scrolling.

        The screenshot that prompted this showed the Project page's last
        table row half-drawn behind the status bar, with no scrollbar: the
        content below the fold was not merely hidden, it was unreachable.
        """
        window.resize(1180, 760)
        window.show()
        QApplication.processEvents()

        unreachable = []
        for row in range(window.nav.count()):
            window.nav.setCurrentRow(row)
            QApplication.processEvents()
            page = window.stack.currentWidget()
            area = _scroll_area(page)
            content = area.widget()
            assert content is not None, f"{page.title}: the scroll area is empty"
            # Whatever the content needs, the viewport is either big enough
            # or the scrollbar can get there.
            needed = content.minimumSizeHint().height()
            reachable = area.viewport().height() + area.verticalScrollBar().maximum()
            if reachable + 1 < needed:
                unreachable.append(
                    f"{page.title}: needs {needed}px, can reach {reachable}px"
                )
        assert not unreachable, "content below the fold cannot be reached:\n" + "\n".join(
            unreachable
        )

    def test_a_short_window_gets_a_scrollbar(self, window, qt_app):
        """Shrunk to a laptop's half-screen, the page still gives a way down."""
        window.resize(900, 420)
        window.show()
        window.nav.setCurrentRow(0)
        QApplication.processEvents()

        page = window.stack.currentWidget()
        area = _scroll_area(page)
        assert area.verticalScrollBar().maximum() > 0, (
            "the Project page fits no scrollbar into 420px, so the exclusion "
            "codes below the fold cannot be read at all"
        )


class TestFrameworkApproachControl:
    """The choice reaches the database, and the page reflects it back."""

    def _project_page(self, window, qt_app):
        window.resize(1180, 760)
        window.show()
        window.nav.setCurrentRow(0)
        QApplication.processEvents()
        return window.stack.currentWidget()

    def test_a_new_review_shows_bottom_up(self, window, qt_app):
        page = self._project_page(window, qt_app)
        assert page.bottom_up_radio.isChecked() is True
        assert page.constructs_edit.isVisible() is False

    def test_choosing_top_down_reveals_the_constructs_box(self, window, qt_app):
        """It is hidden under bottom-up because nothing reads it there."""
        page = self._project_page(window, qt_app)
        page.top_down_radio.setChecked(True)
        QApplication.processEvents()
        assert page.constructs_edit.isVisible() is True

    def test_the_choice_is_saved_and_read_back(self, window, qt_app):
        page = self._project_page(window, qt_app)
        page.top_down_radio.setChecked(True)
        page.constructs_edit.setPlainText("Place attachment\nPerceived benefit")
        page._save()

        info = window.project.info()
        assert info["framework_approach"] == "top_down"
        assert "Place attachment" in info["a_priori_constructs"]

        page.refresh()
        assert page.top_down_radio.isChecked() is True
        assert "Perceived benefit" in page.constructs_edit.toPlainText()

    def test_the_synthesis_page_says_which_approach_is_in_force(self, window, qt_app):
        """A reviewer pressing 'Propose a codebook' should know which of the
        two things that button does in their review."""
        from slr_studio import STAGES

        page = self._project_page(window, qt_app)
        page.top_down_radio.setChecked(True)
        page.constructs_edit.setPlainText("Place attachment")
        page._save()

        window.nav.setCurrentRow([label for _, label in STAGES].index("Synthesise"))
        QApplication.processEvents()
        synthesise = window.stack.currentWidget()
        text = synthesise.approach_label.text()
        assert "top-down" in text.lower()
        assert "Place attachment" in text


class TestTheFrameworkDropdown:
    """Built-ins, the reviewer's own, and a way to add one."""

    def _project_page(self, window):
        window.resize(1180, 760)
        window.show()
        window.nav.setCurrentRow(0)
        QApplication.processEvents()
        return window.stack.currentWidget()

    def test_it_offers_more_than_the_original_four(self, window, qt_app):
        page = self._project_page(window)
        names = {
            page.framework_combo.itemData(i)
            for i in range(page.framework_combo.count())
        }
        assert {"PICO", "PCC", "SPIDER", "SOBC", "SPICE", "CIMO"} <= names

    def test_the_last_entry_opens_the_dialog_rather_than_selecting(
        self, window, qt_app
    ):
        page = self._project_page(window)
        last = page.framework_combo.count() - 1
        assert page.framework_combo.itemText(last) == page.DEFINE_NEW
        assert page.framework_combo.itemData(last) == ""

    def test_a_defined_framework_appears_and_can_be_chosen(self, window, qt_app):
        page = self._project_page(window)
        window.project.define_framework(
            "Resident response", "antecedent → appraisal → response",
            actor="human:test",
        )
        page._fill_frameworks(selected="Resident response")
        page._save()

        assert window.project.info()["framework_type"] == "Resident response"
        assert window.project.framework_stages() == (
            "antecedent", "appraisal", "response",
        )

    def test_the_page_shows_the_stages_it_will_look_for(self, window, qt_app):
        """So a reviewer can see what choosing this framework buys them."""
        page = self._project_page(window)
        page._fill_frameworks(selected="CIMO")
        text = page.framework_stages_label.text()
        assert "context" in text and "mechanism" in text

    def test_saving_never_writes_the_define_entry_as_a_framework(
        self, window, qt_app
    ):
        """It is a verb in a list of nouns; it must not reach the database."""
        page = self._project_page(window)
        page.framework_combo.setCurrentIndex(page.framework_combo.count() - 1)
        page._save()
        assert window.project.info()["framework_type"] != page.DEFINE_NEW
        assert window.project.framework_stages(), (
            "the saved framework has no stages, so gap detection is off"
        )


class TestReadableFields:
    def test_multi_line_fields_show_more_than_one_line(self, window, qt_app):
        """Objectives and friends must not collapse to a clipped single line.

        In the first Windows build these rendered about 20px tall: the
        placeholder text was cut through the middle of its own letters. The
        cause was a maximum height with no minimum, so the layout squeezed
        them to nothing to fit the window.

        The window is deliberately short here. At 1180x760 these fields look
        fine, which is why the fault reached a user: it appears only once
        the page has more content than the window has room for, and then it
        is the fields that give way.

        This test passes on Linux even without the fix, because there the
        style's own minimum for a scrollable box is already three lines
        tall. On Windows it is not, which is why that is where it was seen.
        The guard belongs here rather than nowhere: CI runs this file on
        both platforms, and Windows is the one the application ships on.
        """
        window.resize(1000, 430)
        window.show()
        window.nav.setCurrentRow(0)
        QApplication.processEvents()

        page = window.stack.currentWidget()
        areas = page.findChildren(QPlainTextEdit)
        assert areas, "the Project page has no multi-line fields at all"
        for box in areas:
            line = box.fontMetrics().lineSpacing()
            assert box.height() >= line * 3, (
                f"a text box {box.height()}px tall shows fewer than three "
                f"{line}px lines of a reviewer's objectives"
            )

    def test_no_label_is_cut_off_on_any_page(self, window, qt_app):
        """Every wrapped label has room for all the lines it needs.

        The same check the Gap Finder page needed, applied to all eleven,
        because the fault was never specific to that page.
        """
        from PySide6.QtWidgets import QLabel

        window.resize(1180, 760)
        window.show()

        clipped = []
        for row in range(window.nav.count()):
            window.nav.setCurrentRow(row)
            QApplication.processEvents()
            page = window.stack.currentWidget()
            for label in page.findChildren(QLabel):
                if not label.wordWrap() or not label.text() or not label.isVisible():
                    continue
                needed = label.heightForWidth(label.width())
                if label.height() < needed:
                    clipped.append(
                        f"{page.title}: {label.text()[:50]!r} needs {needed}px, "
                        f"has {label.height()}px"
                    )
        assert not clipped, "clipped text:\n" + "\n".join(clipped)
