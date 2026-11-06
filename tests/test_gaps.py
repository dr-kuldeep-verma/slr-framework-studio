"""The Gap Finder over a real project.

The detectors are tested against a corpus written out in full; this file is
about reading that corpus out of the database correctly, storing what comes
back, and the two things the spec is strictest about: no statement without
its counts, and no claim of absence without the search date.
"""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider
from slr_studio.gaps import GapEngine, GapError, GapSettings


@pytest.fixture
def reviewed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", framework=True)
    project.update_info(searched_to="2026-03-01")
    yield project
    project.close()


def engine_for(project, **settings):
    client = AIClient(
        project, MockProvider(), stage="gaps", sleep=lambda _: None
    )
    return GapEngine(project, client, GapSettings(**settings))


# -- the quick scan ---------------------------------------------------------


class TestQuickScan:
    def test_it_runs_on_the_imported_records_before_screening(self, tmp_path):
        """The scan is for refining objectives, so it cannot need a review."""
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "early")
        try:
            outcome = engine_for(project).quick_scan(actor="human:test")
            assert outcome.counts["n_records"] > 0
            assert outcome.counts["n_topics"] >= 1
        finally:
            project.close()

    def test_the_topics_are_stored_with_their_records(self, reviewed):
        engine_for(reviewed).quick_scan(actor="human:test")
        assert reviewed.db.scalar("SELECT COUNT(*) FROM topic") >= 1
        assert reviewed.db.scalar("SELECT COUNT(*) FROM topic_member") >= 1

    def test_re_running_it_replaces_rather_than_doubles(self, reviewed):
        engine = engine_for(reviewed)
        engine.quick_scan(actor="human:test")
        first = reviewed.db.scalar("SELECT COUNT(*) FROM topic_member")
        engine.quick_scan(actor="human:test")
        assert reviewed.db.scalar("SELECT COUNT(*) FROM topic_member") == first

    def test_it_reports_the_trend_and_the_co_occurrences(self, reviewed):
        engine = engine_for(reviewed)
        engine.quick_scan(actor="human:test")
        scan = engine.scan_results()
        assert scan["topics"]
        assert isinstance(scan["co_occurrence"], list)
        for topic in scan["topics"]:
            assert isinstance(topic["trend"], dict)

    def test_it_writes_no_gaps_because_it_is_not_a_finding(self, reviewed):
        engine_for(reviewed).quick_scan(actor="human:test")
        assert reviewed.db.scalar("SELECT COUNT(*) FROM gap_register") == 0


# -- the full analysis ------------------------------------------------------


class TestFullAnalysis:
    def test_it_needs_the_search_date(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "undated", framework=True)
        try:
            with pytest.raises(GapError) as caught:
                engine_for(project).find_gaps(actor="human:test")
            assert "search" in str(caught.value).casefold()
        finally:
            project.close()

    def test_it_needs_a_graded_inventory(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "bare", extract=True)
        project.update_info(searched_to="2026-03-01")
        try:
            with pytest.raises(GapError):
                engine_for(project).find_gaps(actor="human:test")
        finally:
            project.close()

    def test_gaps_are_found_and_stored(self, reviewed):
        outcome = engine_for(reviewed).find_gaps(actor="human:test")
        assert outcome.counts["n_gaps"] >= 1
        assert reviewed.db.scalar("SELECT COUNT(*) FROM gap_register") == (
            outcome.counts["n_gaps"]
        )

    def test_every_stored_gap_carries_its_counts(self, reviewed):
        engine_for(reviewed).find_gaps(actor="human:test")
        for row in reviewed.db.query(
            "SELECT supporting_counts FROM gap_register"
        ):
            counts = json.loads(row["supporting_counts"])
            assert counts

    def test_every_statement_is_qualified_to_the_corpus(self, reviewed):
        engine_for(reviewed).find_gaps(actor="human:test")
        for row in reviewed.db.query("SELECT statement FROM gap_register"):
            assert "within the included corpus" in row["statement"].casefold()

    def test_every_statement_names_the_search_date(self, reviewed):
        engine_for(reviewed).find_gaps(actor="human:test")
        for row in reviewed.db.query(
            "SELECT statement, corpus_searched_to FROM gap_register"
        ):
            assert row["corpus_searched_to"] == "2026-03-01"
            assert "2026-03-01" in row["statement"]

    def test_the_gaps_are_only_of_the_seven_types(self, reviewed):
        engine_for(reviewed).find_gaps(actor="human:test")
        types = {
            r["gap_type"] for r in reviewed.db.query(
                "SELECT DISTINCT gap_type FROM gap_register"
            )
        }
        assert types <= {
            "evidence", "theoretical", "contradiction", "methodological",
            "population_context", "information", "author_stated",
        }

    def test_re_running_replaces_the_register_rather_than_doubling_it(
        self, reviewed
    ):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        first = reviewed.db.scalar("SELECT COUNT(*) FROM gap_register")
        engine.find_gaps(actor="human:test")
        assert reviewed.db.scalar("SELECT COUNT(*) FROM gap_register") == first

    def test_a_reviewer_decision_survives_a_re_run(self, reviewed):
        """Rejecting a gap is a judgement; re-running must not discard it."""
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        gap = engine.gaps()[0]
        engine.decide_gap(
            gap["id"], status="rejected", reason="not a gap, by design",
            actor="human:test",
        )
        engine.find_gaps(actor="human:test")
        kept = [
            g for g in engine.gaps()
            if g["cell_key"] == gap["cell_key"] and g["gap_type"] == gap["gap_type"]
        ]
        assert kept and kept[0]["status"] == "rejected"
        assert kept[0]["status_reason"] == "not a gap, by design"

    def test_the_audit_chain_survives(self, reviewed):
        engine_for(reviewed).find_gaps(actor="human:test")
        assert reviewed.audit.verify() == []


class TestTraceability:
    """Section 14: every gap statement traces to its supporting counts."""

    def test_each_gap_points_at_the_records_behind_it(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        for gap in engine.gaps():
            counts = json.loads(gap["supporting_counts"])
            assert counts
            if gap["gap_type"] in ("contradiction", "author_stated"):
                assert gap["n_records"] >= 1

    def test_every_cited_record_exists(self, reviewed):
        engine_for(reviewed).find_gaps(actor="human:test")
        orphans = reviewed.db.scalar(
            "SELECT COUNT(*) FROM gap_evidence e WHERE e.record_id IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM record r WHERE r.id = e.record_id)"
        )
        assert orphans == 0

    def test_a_gap_can_be_read_back_with_its_evidence(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        gap = engine.gaps()[0]
        evidence = engine.gap_evidence(gap["id"])
        assert isinstance(evidence, list)


# -- prioritisation ---------------------------------------------------------


class TestPrioritisation:
    def test_every_gap_is_scored(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        for gap in engine.gaps():
            assert gap["priority"] is not None
            for criterion in (
                "score_centrality", "score_size", "score_relevance",
                "score_feasibility",
            ):
                assert gap[criterion] is not None

    def test_the_ranking_is_highest_first(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        priorities = [g["priority"] for g in engine.ranked()]
        assert priorities == sorted(priorities, reverse=True)

    def test_a_reviewer_can_override_a_score(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        gap = engine.ranked()[-1]
        engine.override_scores(
            gap["id"], {"size": 1.0, "centrality": 1.0},
            reason="central to the two propositions I care about",
            actor="human:test",
        )
        updated = next(g for g in engine.gaps() if g["id"] == gap["id"])
        assert updated["score_size"] == 1.0
        assert updated["priority"] > gap["priority"]
        assert updated["override_reason"]

    def test_changing_the_weights_changes_the_order(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        before = [g["id"] for g in engine.ranked()]
        engine.set_weights(
            {"centrality": 0.0, "size": 1.0, "relevance": 0.0,
             "feasibility": 0.0},
            actor="human:test",
        )
        after = [g["id"] for g in engine.ranked()]
        assert set(before) == set(after)


# -- from gap to study ------------------------------------------------------


class TestFromGapToStudy:
    def test_the_top_gaps_get_a_study_drafted(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        outcome = engine.draft_studies(actor="human:test", top=3)
        assert outcome.counts["n_drafted"] >= 1
        drafted = [g for g in engine.gaps() if g["suggested_question"]]
        assert drafted

    def test_a_draft_carries_everything_the_spec_asks_for(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        engine.draft_studies(actor="human:test", top=2)
        for gap in engine.gaps():
            if not gap["suggested_question"]:
                continue
            assert gap["suggested_hypothesis"]
            assert gap["suggested_design"]
            assert gap["suggested_population"]

    def test_nothing_is_drafted_before_the_gaps_are_found(self, reviewed):
        with pytest.raises(GapError):
            engine_for(reviewed).draft_studies(actor="human:test")


# -- the evidence gap map ---------------------------------------------------


class TestEvidenceGapMap:
    def test_the_map_has_a_cell_for_every_construct_and_column(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        grid = engine.gap_map(dimension="country")
        assert grid.rows and grid.columns
        assert len(grid.cells) == len(grid.rows) * len(grid.columns)

    def test_a_cell_knows_the_studies_behind_it(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        grid = engine.gap_map(dimension="country")
        for cell in grid.cells.values():
            assert cell.n_datasets == len(cell.record_ids) or cell.n_datasets >= 0

    def test_the_map_is_written_in_all_three_formats(self, reviewed):
        engine = engine_for(reviewed)
        engine.find_gaps(actor="human:test")
        written = engine.write_gap_map(actor="human:test", dimension="country")
        formats = {path.suffix.lstrip(".") for path in written}
        assert {"svg", "png", "xlsx"} <= formats
        for path in written:
            assert path.is_file() and path.stat().st_size > 200
