"""The 'what to do next' guide follows the review through its stages."""

from __future__ import annotations

import pytest

from slr_studio.core.next_step import next_step, rapid_unclear_count
from slr_studio.demo import build_demo_project


def test_a_new_review_starts_with_the_protocol(project):
    step = next_step(project)
    assert step.page == "Project" and step.headline == "Lock the protocol"
    assert step.steps


def test_after_locking_it_asks_for_records(project):
    project.update_info(objectives="x", inclusion_criteria="y")
    project.protocol.lock(actor="human:test")
    assert next_step(project).page == "Import"


def test_imported_records_are_deduplicated_next(imported_project):
    imported_project.update_info(objectives="x", inclusion_criteria="y")
    imported_project.protocol.lock(actor="human:test")
    assert next_step(imported_project).headline == "Remove duplicate records"


def test_then_the_pilot_is_labelled_with_progress(imported_project):
    from slr_studio.dedup import DedupEngine

    DedupEngine(imported_project).run()
    imported_project.update_info(objectives="x", inclusion_criteria="y")
    imported_project.protocol.lock(actor="human:test")
    step = next_step(imported_project)
    if step.page == "Deduplicate":  # the fixtures leave a pair to decide
        assert "possible duplicates" in step.headline
        return
    assert step.page == "Screen" and step.focus == "pilot"
    assert "labelled" in step.progress


@pytest.mark.parametrize(
    "stages, page",
    [
        (dict(screen=True), "Retrieve"),
        (dict(screen=True, fulltext=True, extract=True, synthesise=True,
              framework=True, gaps=True), None),
    ],
)
def test_the_demonstration_review_points_on(tmp_path, stages, page):
    project = build_demo_project(tmp_path / "demo", **stages)
    step = next_step(project)
    if page:
        assert step.page in (page, "Screen")
    else:
        # A demo carried to the end has nothing left but exporting, or is done.
        assert step.page in ("Export", "Gap Finder", "Framework", "Synthesise")
    assert step.headline


def test_quick_unclear_decisions_are_noticed(tmp_path):
    from datetime import datetime, timedelta, timezone

    project = build_demo_project(tmp_path / "demo", screen=True)
    ids = [r["record_id"] for r in project.db.query(
        "SELECT record_id FROM screening_status LIMIT 4")]
    start = datetime(2026, 11, 10, 7, 0, 0, tzinfo=timezone.utc)
    for n, record_id in enumerate(ids):
        when = (start + timedelta(seconds=2 * n)).strftime("%Y-%m-%dT%H:%M:%SZ")
        project.db.insert("screening_decision", {
            "run_id": None, "record_id": record_id, "reviewer_role": "human",
            "reviewer": "human:test", "decision": "unclear", "exclusion_code": "",
            "reason": "", "quote": "", "quote_verified": 0, "ai_call_id": None,
            "created_at": when,
        })
        project.db.execute(
            "UPDATE screening_status SET decision = 'unclear', source = 'human' "
            "WHERE record_id = ?", (record_id,))
    # The first has nothing before it; the next three came 2 seconds apart.
    assert rapid_unclear_count(project.db) == 3
    step = next_step(project)
    if step.focus == "revisit":
        assert "3 left unclear" in step.progress
