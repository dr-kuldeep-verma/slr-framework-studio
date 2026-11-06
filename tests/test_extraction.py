"""Extraction: the schema rules, the engine, and the blind direction check."""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.dedup import DedupEngine
from slr_studio.extraction import ExtractionEngine, ExtractionSettings
from slr_studio.extraction.engine import ExtractionError
from slr_studio.extraction.prompts import direction_system, extraction_system
from slr_studio.extraction.schemas import (
    DESIGN_FAMILIES,
    DirectionCheck,
    ExtractionAnswer,
    Relationship,
    ReportExtraction,
    instrument_for,
)
from slr_studio.fulltext import EligibilityEngine, RetrievalService
from slr_studio.screening import ScreeningEngine, ScreeningSettings


# -- the schemas -----------------------------------------------------------


class TestTheExperimentRule:
    def test_a_real_experiment_stands(self):
        report = ReportExtraction(
            design="randomised controlled trial",
            design_family="randomised_experiment",
            manipulated_treatment=True,
            control_group=True,
        )
        assert report.design_family == "randomised_experiment"
        assert report.design_downgraded is False

    def test_an_experiment_without_a_control_is_downgraded(self):
        report = ReportExtraction(
            design_family="randomised_experiment",
            manipulated_treatment=True,
            control_group=False,
        )
        assert report.design_family == "quasi_experiment"
        assert report.design_downgraded is True
        assert "no control" in report.design_note

    def test_an_experiment_without_a_manipulation_is_downgraded(self):
        report = ReportExtraction(
            design_family="randomised_experiment",
            manipulated_treatment=False,
            control_group=True,
        )
        assert report.design_family == "quasi_experiment"
        assert "no manipulated treatment" in report.design_note

    def test_the_downgrade_says_what_was_missing(self):
        report = ReportExtraction(design_family="randomised_experiment")
        assert "no manipulated treatment" in report.design_note
        assert "no control" in report.design_note

    def test_the_rule_does_not_touch_other_designs(self):
        report = ReportExtraction(design_family="cross_sectional")
        assert report.design_family == "cross_sectional"
        assert report.design_downgraded is False


class TestAppraisalInstruments:
    @pytest.mark.parametrize(
        "family, expected",
        [
            ("randomised_experiment", "RoB 2"),
            ("cross_sectional", "AXIS"),
            ("qualitative", "CASP"),
            ("mixed_methods", "MMAT"),
            ("conceptual", "none"),
        ],
    )
    def test_the_usual_instrument_for_each_design(self, family, expected):
        assert instrument_for(family) == expected

    def test_a_matching_instrument_is_not_flagged(self):
        report = ReportExtraction(design_family="cross_sectional",
                                  appraisal_instrument="AXIS")
        assert report.instrument_matches_design is True

    def test_a_mismatch_is_flagged_but_not_overruled(self):
        report = ReportExtraction(design_family="cross_sectional",
                                  appraisal_instrument="CASP")
        assert report.instrument_matches_design is False
        # The choice stands: an unusual design may warrant another tool, and
        # that is the reviewer's judgement.
        assert report.appraisal_instrument == "CASP"

    def test_instrument_names_are_normalised(self):
        assert ReportExtraction(appraisal_instrument="rob2").appraisal_instrument == "RoB 2"
        assert ReportExtraction(appraisal_instrument="").appraisal_instrument == "none"

    def test_every_design_family_has_an_instrument_rule(self):
        for family in DESIGN_FAMILIES:
            assert instrument_for(family) in ("RoB 2", "AXIS", "CASP", "MMAT", "none")


class TestRelationshipSchema:
    def test_both_labels_are_required(self):
        with pytest.raises(ValueError, match="predictor and an outcome"):
            Relationship(predictor_label="", outcome_label="support")

    def test_a_quotation_needs_a_page(self):
        with pytest.raises(ValueError, match="which page"):
            Relationship(predictor_label="a", outcome_label="b", quote="some words")

    def test_a_page_without_a_quotation_is_dropped(self):
        rel = Relationship(predictor_label="a", outcome_label="b", quote_page=3)
        assert rel.quote_page is None

    def test_page_numbers_written_in_prose_are_understood(self):
        rel = Relationship(predictor_label="a", outcome_label="b",
                           quote="words", quote_page="p. 12")
        assert rel.quote_page == 12

    def test_a_statistical_claim_without_a_statistic_becomes_asserted(self):
        rel = Relationship(predictor_label="a", outcome_label="b",
                           evidence_basis="statistical")
        assert rel.evidence_basis == "asserted"

    def test_a_statistical_claim_with_a_statistic_stands(self):
        rel = Relationship(predictor_label="a", outcome_label="b",
                           evidence_basis="statistical", statistic="r = 0.41")
        assert rel.evidence_basis == "statistical"

    def test_a_mediation_needs_its_mediator(self):
        with pytest.raises(ValueError, match="mediator"):
            Relationship(predictor_label="a", outcome_label="b",
                         relationship_type="mediation")

    def test_a_moderation_needs_its_moderator(self):
        with pytest.raises(ValueError, match="moderator"):
            Relationship(predictor_label="a", outcome_label="b",
                         relationship_type="moderation")

    def test_directions_are_case_insensitive(self):
        assert Relationship(predictor_label="a", outcome_label="b",
                            direction="Positive").direction == "positive"

    def test_labels_keep_the_authors_wording(self):
        rel = Relationship(
            predictor_label="  Perceived fairness of benefit distribution ",
            outcome_label="support for further tourism development",
        )
        assert rel.predictor_label == "Perceived fairness of benefit distribution"


class TestDirectionCheck:
    def test_plain_answers_parse(self):
        assert DirectionCheck(direction="negative").direction == "negative"

    def test_common_phrasings_map_onto_the_categories(self):
        assert DirectionCheck(direction="no association").direction == "none"
        assert DirectionCheck(direction="cannot tell").direction == "unclear"

    def test_an_invented_category_is_refused(self):
        with pytest.raises(ValueError):
            DirectionCheck(direction="slightly upward")


class TestPrompts:
    def test_the_extractor_is_told_to_keep_the_authors_labels(self):
        system = extraction_system("PROTOCOL", DESIGN_FAMILIES)
        assert "Copy the authors' own labels" in system
        assert "PROTOCOL" in system

    def test_the_extractor_is_told_the_experiment_rule(self):
        system = extraction_system("p", DESIGN_FAMILIES)
        assert "manipulated" in system and "control or comparison group" in system

    def test_the_extractor_is_warned_quotations_are_checked(self):
        assert "checked against that page automatically" in extraction_system(
            "p", DESIGN_FAMILIES
        )

    def test_the_direction_check_never_sees_the_recorded_direction(self):
        from slr_studio.extraction.prompts import direction_user

        system = direction_system()
        user = direction_user("participation", "support", "more of one, more of the other")
        assert "positive" in system  # it is told the categories
        # but nothing in the prompt says what the extraction concluded
        assert "recorded" not in user and "extraction" not in user

    def test_the_direction_check_is_told_not_to_infer(self):
        assert "If the words do not say" in direction_system()


# -- the engine ------------------------------------------------------------


@pytest.fixture
def included(imported_project, fixtures):
    """A project with two reports included at full text, ready to extract."""
    DedupEngine(imported_project).run()
    imported_project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    imported_project.protocol.lock(actor="human:test")
    screening = ScreeningEngine(
        imported_project,
        AIClient(imported_project, MockProvider(), stage="screen"),
        ScreeningSettings(),
    )
    screening.waive_pilot("test fixture")
    screening.run_full(actor="human:test")

    retrieval = RetrievalService(imported_project)
    retrieval.attach_pdf(fixtures / "pdfs" / "menon_2021_kerala.pdf")
    retrieval.attach_pdf(fixtures / "pdfs" / "okonkwo_2019_benefit_sharing.pdf")
    eligibility = EligibilityEngine(
        imported_project,
        AIClient(imported_project, MockProvider(), stage="fulltext"),
    )
    eligibility.run(actor="human:test")
    for item in eligibility.review_queue():
        eligibility.resolve(item["record_id"], "include", actor="human:test")
    return imported_project


def engine_for(project, provider=None, **settings):
    client = AIClient(project, provider or MockProvider(), stage="extract",
                      sleep=lambda _: None)
    return ExtractionEngine(project, client, ExtractionSettings(**settings))


class TestGates:
    def test_extraction_needs_a_locked_protocol(self, imported_project):
        DedupEngine(imported_project).run()
        with pytest.raises(ExtractionError, match="has not been locked"):
            engine_for(imported_project).run()

    def test_only_included_reports_are_extracted(self, included):
        engine = engine_for(included)
        pending = {r["id"] for r in engine.pending_records()}
        expected = {
            r["record_id"]
            for r in included.db.query(
                "SELECT record_id FROM fulltext_status WHERE decision = 'include'"
            )
        }
        assert pending == expected and pending


class TestExtracting:
    @pytest.fixture
    def extracted(self, included):
        engine = engine_for(included)
        outcome = engine.run(actor="human:test")
        return included, engine, outcome

    def test_every_included_report_gets_an_extraction(self, extracted):
        project, engine, outcome = extracted
        assert outcome.n_done == outcome.n_records == 2
        assert engine.counts()["n_extracted"] == 2

    def test_relationships_are_recorded_with_the_authors_labels(self, extracted):
        _, engine, outcome = extracted
        assert outcome.n_relationships > 0
        inventory = engine.inventory()
        assert all(r["predictor_label"] and r["outcome_label"] for r in inventory)

    def test_every_quotation_is_checked_against_its_page(self, extracted):
        _, _, outcome = extracted
        assert outcome.n_quotes_checked == outcome.n_relationships
        assert outcome.n_quotes_on_cited_page == outcome.n_quotes_checked
        assert outcome.n_quotes_not_found == 0

    def test_every_direction_is_re_derived(self, extracted):
        project, _, outcome = extracted
        assert outcome.n_directions_checked == outcome.n_relationships
        rows = project.db.query(
            "SELECT verified_direction FROM relationship WHERE coder_role = 'ai'"
        )
        assert all(row["verified_direction"] for row in rows)

    def test_the_run_records_its_model_and_protocol(self, extracted):
        project, _, outcome = extracted
        row = project.db.query_one(
            "SELECT * FROM extraction_run WHERE id = ?", (outcome.run_id,)
        )
        assert row["model"] and row["prompt_version"] == "extract-1.0"
        assert row["protocol_version"] == project.protocol.current().id
        assert row["status"] == "finished"

    def test_a_second_run_asks_nothing_again(self, extracted):
        project, engine, _ = extracted
        before = project.db.scalar("SELECT COUNT(*) FROM ai_call")
        second = engine.run(actor="human:test")
        assert second.n_records == 0
        assert project.db.scalar("SELECT COUNT(*) FROM ai_call") == before

    def test_the_run_is_logged_and_the_chain_holds(self, extracted):
        project, _, _ = extracted
        assert any(
            e.action == "extraction_run" for e in project.audit.entries(stage="extract")
        )
        assert project.audit.verify() == []

    def test_every_relationship_traces_to_a_logged_call(self, extracted):
        project, _, _ = extracted
        orphans = project.db.scalar(
            "SELECT COUNT(*) FROM relationship r WHERE r.coder_role = 'ai' "
            "AND (r.ai_call_id IS NULL OR NOT EXISTS "
            "     (SELECT 1 FROM ai_call c WHERE c.id = r.ai_call_id))"
        )
        assert orphans == 0

    def test_the_inventory_carries_the_study_beside_the_relationship(self, extracted):
        _, engine, _ = extracted
        row = engine.inventory()[0]
        assert row["title"] and row["design_family"]

    def test_a_report_yielding_nothing_is_queued_not_ignored(self, included):
        silent = ScriptedProvider(
            default=json.dumps({"report": {"design_family": "cross_sectional"},
                                "relationships": []})
        )
        engine = engine_for(included, provider=silent)
        engine.run(actor="human:test")
        reasons = {item["reason"] for item in engine.review_queue()}
        assert "nothing_extracted" in reasons


class TestTheDirectionCheck:
    def test_a_disagreement_is_flagged_and_queued(self, included):
        # The extraction says negative; the quotation plainly says otherwise,
        # so the blind check reads it as positive.
        provider = ScriptedProvider(
            by_match=[
                (r"checking one sentence|Which direction",
                 json.dumps({"direction": "positive", "note": "more goes with more"})),
            ],
            default=json.dumps({
                "report": {"design_family": "cross_sectional"},
                "relationships": [{
                    "predictor_label": "Participation",
                    "outcome_label": "support for further tourism development",
                    "direction": "negative",
                    "quote": "Participation was strongly associated with support",
                    "quote_page": 2,
                }],
            }),
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_direction_mismatches == 1
        assert any("direction" in m for m in outcome.messages)
        row = engine.inventory()[0]
        assert row["direction"] == "negative"           # the extraction stands
        assert row["verified_direction"] == "positive"  # the check disagreed
        assert row["verify_agrees"] == 0
        assert row["needs_review"] == 1
        assert any(
            item["reason"] == "direction_mismatch" for item in engine.review_queue()
        )

    def test_agreement_is_recorded_as_such(self, included):
        provider = ScriptedProvider(
            by_match=[
                (r"Which direction",
                 json.dumps({"direction": "positive", "note": "more goes with more"})),
            ],
            default=json.dumps({
                "report": {"design_family": "cross_sectional"},
                "relationships": [{
                    "predictor_label": "Participation",
                    "outcome_label": "support for further tourism development",
                    "direction": "positive",
                    "quote": "Participation was strongly associated with support",
                    "quote_page": 2,
                }],
            }),
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_direction_mismatches == 0
        assert engine.inventory()[0]["verify_agrees"] == 1
        assert engine.inventory()[0]["needs_review"] == 0
        reasons = {item["reason"] for item in engine.review_queue()}
        assert "direction_mismatch" not in reasons

    def test_unclear_on_either_side_is_not_counted_as_a_disagreement(self, included):
        provider = ScriptedProvider(
            by_match=[
                (r"Which direction",
                 json.dumps({"direction": "unclear", "note": "no direction stated"})),
            ],
            default=json.dumps({
                "report": {"design_family": "cross_sectional"},
                "relationships": [{
                    "predictor_label": "Participation",
                    "outcome_label": "support for further tourism development",
                    "direction": "positive",
                    "quote": "Participation was strongly associated with support",
                    "quote_page": 2,
                }],
            }),
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_direction_mismatches == 0
        assert engine.inventory()[0]["verify_agrees"] is None

    def test_the_check_can_be_turned_off(self, included):
        engine = engine_for(included, verify_directions=False)
        outcome = engine.run(actor="human:test")
        assert outcome.n_directions_checked == 0

    def test_a_relationship_with_no_quotation_is_not_checked(self, included):
        provider = ScriptedProvider(
            default=json.dumps({
                "report": {"design_family": "cross_sectional"},
                "relationships": [{
                    "predictor_label": "a", "outcome_label": "b",
                    "direction": "positive",
                }],
            })
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_directions_checked == 0
        assert outcome.n_quotes_checked == 0


class TestQuotationFailures:
    def test_an_invented_quotation_is_flagged_and_queued(self, included):
        liar = ScriptedProvider(
            by_match=[(r"Which direction",
                       json.dumps({"direction": "positive", "note": ""}))],
            default=json.dumps({
                "report": {"design_family": "cross_sectional"},
                "relationships": [{
                    "predictor_label": "a", "outcome_label": "b",
                    "direction": "positive",
                    "quote": "words that appear in no report anywhere at all",
                    "quote_page": 1,
                }],
            }),
        )
        engine = engine_for(included, provider=liar)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_quotes_not_found == 1
        row = engine.inventory()[0]
        assert row["quote_verified"] == 0 and row["needs_review"] == 1
        assert any(item["reason"] == "quote_failed" for item in engine.review_queue())

    def test_a_quotation_on_the_wrong_page_is_its_own_case(self, included):
        provider = ScriptedProvider(
            by_match=[(r"Which direction",
                       json.dumps({"direction": "positive", "note": ""}))],
            default=json.dumps({
                "report": {"design_family": "cross_sectional"},
                "relationships": [{
                    "predictor_label": "Participation",
                    "outcome_label": "support for further tourism development",
                    "direction": "positive",
                    "quote": "Participation was strongly associated with support",
                    "quote_page": 1,
                }],
            }),
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_quotes_elsewhere == 1
        assert outcome.n_quotes_not_found == 0
        row = engine.inventory()[0]
        assert row["quote_page"] == 1


class TestTheDesignRule:
    def test_a_downgraded_design_is_queued(self, included):
        provider = ScriptedProvider(
            default=json.dumps({
                "report": {
                    "design": "we describe this as an experiment",
                    "design_family": "randomised_experiment",
                    "manipulated_treatment": False,
                    "control_group": False,
                },
                "relationships": [],
            })
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_designs_downgraded == 1
        record_id = engine.included_records()[0]["id"]
        stored = engine.extraction_for(record_id)
        assert stored["design_family"] == "quasi_experiment"
        assert stored["design_downgraded"] == 1
        assert any(item["reason"] == "design_rule" for item in engine.review_queue())

    def test_a_mismatched_instrument_is_flagged(self, included):
        provider = ScriptedProvider(
            default=json.dumps({
                "report": {"design_family": "cross_sectional",
                           "appraisal_instrument": "CASP"},
                "relationships": [],
            })
        )
        engine = engine_for(included, provider=provider)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_instrument_mismatches == 1
        record_id = engine.included_records()[0]["id"]
        assert engine.extraction_for(record_id)["instrument_matches_design"] == 0


class TestHumanCorrections:
    @pytest.fixture
    def extracted(self, included):
        engine = engine_for(included)
        engine.run(actor="human:test")
        return included, engine

    def test_changing_a_direction_is_logged_as_an_override(self, extracted):
        project, engine = extracted
        relationship = engine.inventory()[0]
        engine.correct_relationship(
            relationship["relationship_id"], direction="negative",
            reason="the table says the opposite", actor="human:test",
        )
        row = project.db.query_one(
            "SELECT direction, needs_review, coder FROM relationship WHERE id = ?",
            (relationship["relationship_id"],),
        )
        assert row["direction"] == "negative"
        assert row["needs_review"] == 0
        assert row["coder"] == "human:test"
        assert project.db.query_one(
            "SELECT * FROM human_override WHERE entity = 'relationship' "
            "AND entity_id = ?",
            (str(relationship["relationship_id"]),),
        ) is not None
        assert project.audit.verify() == []

    def test_an_unknown_field_is_refused(self, extracted):
        _, engine = extracted
        relationship = engine.inventory()[0]
        with pytest.raises(ExtractionError, match="Unknown relationship fields"):
            engine.correct_relationship(
                relationship["relationship_id"], conclusion="important"
            )

    def test_withdrawing_a_relationship_needs_a_reason(self, extracted):
        _, engine = extracted
        relationship = engine.inventory()[0]
        with pytest.raises(ExtractionError, match="Say why"):
            engine.delete_relationship(relationship["relationship_id"], "")

    def test_a_withdrawn_relationship_leaves_the_inventory_but_not_the_record(
        self, extracted
    ):
        project, engine = extracted
        before = len(engine.inventory())
        relationship = engine.inventory()[0]
        engine.delete_relationship(
            relationship["relationship_id"], "the authors state no such link",
            actor="human:test",
        )
        assert len(engine.inventory()) == before - 1
        # Still in the table, with why it was withdrawn.
        row = project.db.query_one(
            "SELECT coder_role, review_reason FROM relationship WHERE id = ?",
            (relationship["relationship_id"],),
        )
        assert row["coder_role"] == "human"
        assert "withdrawn" in row["review_reason"]

    def test_accepting_a_report_clears_its_queue_item(self, included):
        provider = ScriptedProvider(
            default=json.dumps({
                "report": {"design_family": "randomised_experiment"},
                "relationships": [],
            })
        )
        engine = engine_for(included, provider=provider)
        engine.run(actor="human:test", limit=1)
        item = engine.review_queue()[0]
        engine.resolve(item["record_id"], "looked at it myself", actor="human:test")
        assert engine.review_queue() == []
        assert any(
            e.action == "extraction_checked"
            for e in included.audit.entries(stage="extract")
        )


class TestPauseAndCost:
    def test_a_run_can_be_stopped_and_continued(self, included):
        engine = engine_for(included)
        state = {"n": 0}

        def stop_after_one() -> bool:
            state["n"] += 1
            return state["n"] > 1

        first = engine.run(actor="human:test", should_stop=stop_after_one)
        assert first.status == "paused" and first.n_done == 1
        second = engine.run(actor="human:test")
        assert second.n_done == 1
        assert engine.summary()["n_pending"] == 0

    def test_the_estimate_mentions_the_direction_checks(self, included):
        estimate = engine_for(included).estimate()
        assert estimate.n_calls == 2
        assert any("direction" in note for note in estimate.notes)

    def test_a_budget_stops_the_run_without_losing_work(self, included):
        engine = engine_for(included, budget_usd=0.0)
        engine.client.provider.model = "claude-opus-costly"
        outcome = engine.run(actor="human:test")
        assert outcome.status == "paused"
        assert any("limit" in m for m in outcome.messages)
