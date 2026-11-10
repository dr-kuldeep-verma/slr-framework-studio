"""Keeping a record unclear asks what the abstract cannot settle."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402

from slr_studio.demo import build_demo_project  # noqa: E402


@pytest.fixture
def page(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    from slr_studio.ui.pages import ScreenPage

    import functools

    import slr_studio.screening as screening

    # The older routing leaves records in the queue for these tests to decide.
    monkeypatch.setattr(screening, "ScreeningSettings", functools.partial(
        screening.ScreeningSettings, unclear_goes_to_fulltext=False))
    project = build_demo_project(tmp_path / "demo", screen=True)
    screen = ScreenPage()
    screen.set_project(project)
    assert screen.queue_list.count(), "the demo leaves records for the reviewer"
    screen.queue_list.setCurrentRow(0)
    return screen


def test_unclear_names_the_criterion_the_abstract_cannot_settle(page, monkeypatch):
    asked = {}

    def pick(parent, title, label, items, current, editable):
        asked["items"] = items
        return items[1], True

    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(pick))
    record_id = page._queue[0]["record_id"]
    page._resolve("unclear")
    row = page.project.db.query_one(
        "SELECT decision, reason FROM screening_decision WHERE record_id = ? "
        "AND reviewer_role = 'human'", (record_id,))
    assert row["decision"] == "unclear"
    assert row["reason"].startswith("Abstract does not settle " + asked["items"][1])
    assert not any("full text" in item.lower() for item in asked["items"])
    assert asked["items"][0].startswith("No abstract")
    assert asked["items"][-1].startswith("Other")
    assert "kept 1 record unclear" in page.unclear_note.text()


def test_cancelling_the_question_records_nothing(page, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getItem",
                        staticmethod(lambda *a, **k: ("", False)))
    record_id = page._queue[0]["record_id"]
    page._resolve("unclear")
    assert page.project.db.query_one(
        "SELECT COUNT(*) AS n FROM screening_decision WHERE record_id = ? "
        "AND reviewer_role = 'human'", (record_id,))["n"] == 0


def test_very_fast_decisions_bring_up_a_note(page, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getItem",
                        staticmethod(lambda p, t, l, items, c, e: (items[0], True)))
    for _ in range(4):
        if not page._queue:
            break
        page.queue_list.setCurrentRow(0)
        page._resolve("unclear")
    if len(page._decision_times) == 4:
        assert not page.pace_note.isHidden()


def test_unclear_for_another_reason_asks_what(page, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getItem",
                        staticmethod(lambda p, t, l, items, c, e: (items[-1], True)))
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("a retracted paper", True)))
    record_id = page._queue[0]["record_id"]
    page._resolve("unclear")
    row = page.project.db.query_one(
        "SELECT reason FROM screening_decision WHERE record_id = ? AND reviewer_role = 'human'",
        (record_id,))
    assert row["reason"] == "Unclear: a retracted paper"


def test_other_is_an_exclusion_code_and_needs_a_reason(page, monkeypatch):
    from slr_studio.ui.pages.screen_page import OTHER_CODE

    index = page.code_combo.findData(OTHER_CODE)
    assert index > 0
    page.code_combo.setCurrentIndex(index)
    monkeypatch.setattr(QInputDialog, "getText",
                        staticmethod(lambda *a, **k: ("duplicate publication", True)))
    record_id = page._queue[0]["record_id"]
    page._resolve("exclude")
    row = page.project.db.query_one(
        "SELECT exclusion_code, reason FROM screening_decision WHERE record_id = ? "
        "AND reviewer_role = 'human'", (record_id,))
    assert row["exclusion_code"] == OTHER_CODE and row["reason"] == "duplicate publication"


def test_the_criteria_are_on_screen(page):
    text = page.criteria_panel.view.toPlainText()
    for code in page.project.exclusion_codes():
        assert code["code"] in text
    assert "Other" in text
    assert "Include" in text
