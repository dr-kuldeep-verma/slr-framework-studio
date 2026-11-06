"""Question frameworks, including ones this application has never heard of.

PCC, PICO, SPIDER and SOBC were hard-coded, and a reviewer working to
SPICE, CIMO or a framework of their own had nowhere to put it. The fix is
not a free-text box: `framework_type` is not a label, it drives the
stage-transition gap detector, and an unrecognised name there returns no
stages, which drops a whole class of gap without saying so. So a
reviewer-defined framework has to carry its stages, and a framework with
no stages has to be reported rather than quietly producing less.
"""

from __future__ import annotations

import pytest

from slr_studio.core.frameworks import (
    BUILT_IN,
    FrameworkError,
    parse_stages,
    validate_definition,
)
from slr_studio.core.project import Project


# -- 1. the built-in catalogue ----------------------------------------------


class TestTheBuiltInCatalogue:
    def test_the_original_four_are_unchanged(self):
        """Reviews in progress were built against these exact stages."""
        assert BUILT_IN["SOBC"] == ("stimulus", "organism", "behaviour", "consequence")
        assert BUILT_IN["PCC"] == ("population", "concept", "context")
        assert BUILT_IN["PICO"] == (
            "population", "intervention", "comparison", "outcome"
        )
        assert BUILT_IN["SPIDER"] == (
            "sample", "phenomenon", "design", "evaluation", "research type"
        )

    @pytest.mark.parametrize(
        "name", ["PICOS", "PICOT", "PECO", "PEO", "SPICE", "ECLIPSE", "CIMO"]
    )
    def test_the_common_ones_are_there_too(self, name):
        """Four was too few: all of these are in routine use."""
        assert name in BUILT_IN
        assert len(BUILT_IN[name]) >= 3

    def test_every_built_in_has_ordered_distinct_stages(self):
        for name, stages in BUILT_IN.items():
            assert len(stages) >= 3, f"{name} has too few stages to transition"
            assert len(set(stages)) == len(stages), f"{name} repeats a stage"
            assert all(stage == stage.casefold() for stage in stages), (
                f"{name} has stages that are not in the folded form the "
                "reports are coded in, so none of them would ever match"
            )


# -- 2. reading what a reviewer typed ---------------------------------------


class TestParsingStages:
    @pytest.mark.parametrize(
        "text",
        [
            "antecedent -> mechanism -> outcome",
            "antecedent → mechanism → outcome",
            "antecedent, mechanism, outcome",
            "antecedent\nmechanism\noutcome",
            "  antecedent  →  Mechanism \n outcome ",
        ],
    )
    def test_the_separators_a_person_actually_types(self, text):
        """Arrow, comma or one per line, and whatever spacing they used."""
        assert parse_stages(text) == ("antecedent", "mechanism", "outcome")

    def test_order_is_kept(self):
        """The order is the whole point: it is what a transition is."""
        assert parse_stages("c → a → b") == ("c", "a", "b")

    def test_a_repeated_stage_is_dropped_not_duplicated(self):
        assert parse_stages("a → b → a") == ("a", "b")

    def test_empty_text_gives_nothing(self):
        assert parse_stages("") == ()
        assert parse_stages("   \n  ") == ()


class TestValidatingADefinition:
    def test_a_good_definition_passes(self):
        validate_definition("Mine", ("a", "b", "c"), existing=())

    def test_a_framework_needs_at_least_two_stages(self):
        """One stage has no transition, so it would detect nothing."""
        with pytest.raises(FrameworkError, match="two"):
            validate_definition("Mine", ("only",), existing=())

    def test_a_built_in_name_cannot_be_redefined(self):
        """Otherwise one review's PICO means something else than another's."""
        with pytest.raises(FrameworkError, match="built-in"):
            validate_definition("PICO", ("a", "b"), existing=())

    def test_the_same_name_twice_is_refused(self):
        with pytest.raises(FrameworkError, match="already"):
            validate_definition("Mine", ("a", "b"), existing=("mine",))

    def test_a_nameless_framework_is_refused(self):
        with pytest.raises(FrameworkError, match="name"):
            validate_definition("   ", ("a", "b"), existing=())


# -- 3. storing one on a project --------------------------------------------


class TestDefiningOneOnAProject:
    def test_a_new_project_offers_the_built_ins(self, project):
        names = {f["name"] for f in project.frameworks()}
        assert "PICO" in names and "SPICE" in names
        assert all(f["is_built_in"] for f in project.frameworks())

    def test_a_reviewer_can_define_their_own(self, project):
        project.define_framework(
            "Resident response",
            "antecedent → appraisal → response",
            actor="human:test",
        )
        mine = next(
            f for f in project.frameworks() if f["name"] == "Resident response"
        )
        assert mine["stages"] == ("antecedent", "appraisal", "response")
        assert mine["is_built_in"] is False

    def test_the_project_can_then_use_it(self, project):
        project.define_framework(
            "Resident response", "antecedent → appraisal → response",
            actor="human:test",
        )
        project.update_info(framework_type="Resident response")
        assert project.info()["framework_type"] == "Resident response"
        assert project.framework_stages() == (
            "antecedent", "appraisal", "response",
        )

    def test_a_framework_nobody_defined_is_refused(self, project):
        """The check that used to be a CHECK constraint, still enforced.

        Widening the column to allow reviewer-defined names must not turn
        it into a field that accepts a typo.
        """
        with pytest.raises(Exception, match="not a framework|unknown|defined"):
            project.update_info(framework_type="PICOO")

    def test_defining_one_is_audited(self, project):
        """It changes what the Gap Finder reports, so it is not a preference."""
        project.define_framework(
            "Resident response", "antecedent → appraisal → response",
            actor="human:test",
        )
        actions = [
            row["action"]
            for row in project.db.query("SELECT action FROM audit_log")
        ]
        assert "define_framework" in actions

    def test_an_older_project_opens_and_keeps_its_framework(self, tmp_path):
        """The column's CHECK constraint has to be rebuilt to widen it, and
        a rebuild that loses the reviewer's setting is not acceptable."""
        import sqlite3

        from slr_studio import SCHEMA_VERSION

        folder = tmp_path / "old"
        project = Project.create(folder, "Older review", actor="human:test")
        project.update_info(framework_type="SPIDER", objectives="Keep me.")
        path = project.db.path
        project.close()

        connection = sqlite3.connect(path)
        with connection:
            connection.execute(
                "UPDATE meta SET value = '10' WHERE key = 'schema_version'"
            )
            connection.execute("DROP TABLE IF EXISTS framework_definition")
        connection.close()

        reopened = Project.open(path, actor="human:test")
        try:
            assert reopened.db.get_meta("schema_version") == str(SCHEMA_VERSION)
            info = reopened.info()
            assert info["framework_type"] == "SPIDER"
            assert info["objectives"] == "Keep me."
        finally:
            reopened.close()


# -- 4. the gap detector no longer goes quiet -------------------------------


class TestTheGapFinderSaysWhenItCannot:
    def test_a_custom_framework_still_gets_stage_transitions(self, project):
        """The point of making reviewers declare stages."""
        from slr_studio.gaps.detect import Corpus, find_theoretical_gaps

        found = find_theoretical_gaps(
            Corpus(
                framework_type="Resident response",
                framework_stage_order=("antecedent", "appraisal", "response"),
            )
        )
        assert found, "a reviewer-defined framework produced no transitions"
        assert any("antecedent" in g.statement.body for g in found)

    def test_a_framework_with_no_stages_produces_nothing(self, project):
        """Still nothing -- but the run has to say so. See the next test."""
        from slr_studio.gaps.detect import Corpus, find_theoretical_gaps

        found = find_theoretical_gaps(
            Corpus(framework_type="Mine", framework_stage_order=())
        )
        assert [g for g in found if g.counts.get("kind") == "stage_transition"] == []

    def test_the_run_reports_that_it_could_not_compute_them(self, project, monkeypatch):
        """Silence and 'there are no gaps' look identical, and are not.

        This is the fault the whole change exists to prevent: a reviewer
        reading a gap register with no stage-transition gaps in it should
        be told whether that means none were found or none were looked for.
        """
        from slr_studio.gaps.engine import GapEngine

        messages = GapEngine.stage_transition_note(stages=(), framework="Mine")
        assert messages, "an undefined framework produced no note at all"
        assert "Mine" in messages
        assert "not computed" in messages.lower()

    def test_a_defined_framework_gets_no_such_note(self, project):
        from slr_studio.gaps.engine import GapEngine

        assert GapEngine.stage_transition_note(
            stages=("a", "b"), framework="PCC"
        ) == ""
