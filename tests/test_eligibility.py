"""Full-text eligibility, and what happens when a quotation does not check out."""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.dedup import DedupEngine
from slr_studio.fulltext import EligibilityEngine, RetrievalService
from slr_studio.fulltext.engine import EligibilityError, EligibilitySettings
from slr_studio.fulltext.prompts import assessor_system, render_report
from slr_studio.fulltext.schemas import EligibilityAnswer
from slr_studio.screening import ScreeningEngine, ScreeningSettings


# -- the answer schema -----------------------------------------------------


class TestEligibilityAnswer:
    def test_a_quotation_must_say_which_page_it_is_on(self):
        with pytest.raises(ValueError, match="which page"):
            EligibilityAnswer(decision="include", reason="r", quote="some words here")

    def test_a_quotation_with_a_page_is_accepted(self):
        answer = EligibilityAnswer(
            decision="include", reason="r", quote="some words", quote_page=3
        )
        assert answer.quote_page == 3

    def test_an_exclusion_must_name_a_code(self):
        with pytest.raises(ValueError, match="exclusion code"):
            EligibilityAnswer(decision="exclude", reason="r")

    def test_page_numbers_written_in_prose_are_understood(self):
        answer = EligibilityAnswer(
            decision="include", reason="r", quote="words", quote_page="p. 12"
        )
        assert answer.quote_page == 12

    def test_a_page_range_takes_the_first_page(self):
        answer = EligibilityAnswer(
            decision="include", reason="r", quote="words", quote_page="7-8"
        )
        assert answer.quote_page == 7

    def test_page_zero_is_refused(self):
        with pytest.raises(ValueError, match="start at 1"):
            EligibilityAnswer(
                decision="include", reason="r", quote="words", quote_page=0
            )

    def test_a_page_without_a_quotation_is_dropped(self):
        answer = EligibilityAnswer(decision="include", reason="r", quote_page=4)
        assert answer.quote_page is None

    def test_a_reason_is_required(self):
        with pytest.raises(ValueError, match="reason is required"):
            EligibilityAnswer(decision="include", reason="")


# -- how a report is laid out ----------------------------------------------


class TestRenderReport:
    def test_pages_are_labelled_with_their_number(self):
        text, shown = render_report(
            [{"page_no": 1, "text": "first page"}, {"page_no": 2, "text": "second page"}]
        )
        assert "[PDF page 1]" in text and "[PDF page 2]" in text
        assert shown == [1, 2]

    def test_an_ocr_page_is_marked_as_possibly_imperfect(self):
        text, _ = render_report([{"page_no": 1, "text": "scanned", "ocr": True}])
        assert "read by OCR" in text

    def test_pages_appear_in_reading_order(self):
        text, shown = render_report(
            [{"page_no": 2, "text": "second"}, {"page_no": 1, "text": "first"}]
        )
        assert shown == [1, 2]
        assert text.index("[PDF page 1]") < text.index("[PDF page 2]")

    def test_a_long_report_is_trimmed_and_says_which_pages_went(self):
        pages = [{"page_no": n, "text": "word " * 500} for n in range(1, 30)]
        text, shown = render_report(pages, max_chars=8_000, head_pages=2)
        assert len(shown) < len(pages)
        assert "were omitted for length" in text
        assert 'answer "unclear"' in text

    def test_the_opening_pages_are_kept(self):
        pages = [{"page_no": n, "text": "word " * 500} for n in range(1, 30)]
        _, shown = render_report(pages, max_chars=8_000, head_pages=2)
        assert 1 in shown

    def test_a_report_with_no_text_says_so(self):
        text, shown = render_report([])
        assert "no text could be read" in text and shown == []


# -- the engine ------------------------------------------------------------


@pytest.fixture
def retrieved(imported_project, fixtures):
    """A project screened and with two reports attached."""
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
    return imported_project


def engine_for(project, provider=None, **settings):
    client = AIClient(project, provider or MockProvider(), stage="fulltext",
                      sleep=lambda _: None)
    return EligibilityEngine(project, client, EligibilitySettings(**settings))


class TestGates:
    def test_assessment_needs_a_locked_protocol(self, imported_project):
        DedupEngine(imported_project).run()
        engine = engine_for(imported_project)
        with pytest.raises(EligibilityError, match="has not been locked"):
            engine.run()

    def test_only_retrieved_reports_are_assessed(self, retrieved):
        engine = engine_for(retrieved)
        pending = {r["id"] for r in engine.pending_records()}
        retrieved_ids = {
            r["record_id"]
            for r in retrieved.db.query(
                "SELECT record_id FROM report WHERE status = 'retrieved'"
            )
        }
        assert pending == retrieved_ids


class TestAssessment:
    @pytest.fixture
    def assessed(self, retrieved):
        engine = engine_for(retrieved)
        outcome = engine.run(actor="human:test")
        return retrieved, engine, outcome

    def test_every_retrieved_report_gets_a_decision(self, assessed):
        project, engine, outcome = assessed
        assert outcome.n_records == 2 and outcome.n_done == 2
        assert engine.counts()["n_assessed"] == 2

    def test_quotations_are_checked_against_the_page_cited(self, assessed):
        _, _, outcome = assessed
        assert outcome.n_quotes_checked == 2
        assert outcome.n_quotes_on_cited_page == 2
        assert outcome.n_quotes_not_found == 0

    def test_the_decision_records_the_page_and_the_check(self, assessed):
        project, _, _ = assessed
        rows = project.db.query(
            "SELECT quote, quote_page, quote_verified, quote_found_on "
            "FROM fulltext_decision WHERE reviewer_role = 'assessor'"
        )
        assert rows
        for row in rows:
            assert row["quote_page"] is not None
            assert row["quote_verified"] == 1
            assert row["quote_found_on"] == row["quote_page"]

    def test_the_run_is_recorded_with_its_model_and_protocol(self, assessed):
        project, _, outcome = assessed
        row = project.db.query_one(
            "SELECT * FROM fulltext_run WHERE id = ?", (outcome.run_id,)
        )
        assert row["model"] and row["prompt_version"] == "fulltext-1.0"
        assert row["protocol_version"] == project.protocol.current().id
        assert row["status"] == "finished"

    def test_a_second_run_asks_nothing_again(self, assessed):
        project, engine, _ = assessed
        before = project.db.scalar("SELECT COUNT(*) FROM ai_call")
        second = engine.run(actor="human:test")
        assert second.n_records == 0
        assert project.db.scalar("SELECT COUNT(*) FROM ai_call") == before

    def test_the_run_is_in_the_audit_log_and_verifies(self, assessed):
        project, _, _ = assessed
        assert any(
            e.action == "eligibility_run" for e in project.audit.entries(stage="fulltext")
        )
        assert project.audit.verify() == []

    def test_every_assessment_traces_to_a_logged_call(self, assessed):
        project, _, _ = assessed
        orphans = project.db.scalar(
            "SELECT COUNT(*) FROM fulltext_decision d WHERE d.reviewer_role = 'assessor' "
            "AND (d.ai_call_id IS NULL OR NOT EXISTS "
            "     (SELECT 1 FROM ai_call c WHERE c.id = d.ai_call_id))"
        )
        assert orphans == 0

    def test_an_exclusion_names_a_code(self, retrieved):
        excluder = ScriptedProvider(
            default=json.dumps({
                "decision": "exclude", "exclusion_code": "E1",
                "reason": "not a primary study", "quote": "", "quote_page": None,
            })
        )
        engine = engine_for(retrieved, provider=excluder)
        engine.run(actor="human:test")
        rows = retrieved.db.query(
            "SELECT exclusion_code FROM fulltext_status WHERE decision = 'exclude'"
        )
        assert rows and all(r["exclusion_code"] == "E1" for r in rows)
        assert engine.exclusion_reasons()[0]["exclusion_code"] == "E1"


class TestQuotationFailures:
    def test_an_invented_quotation_is_flagged_and_queued(self, retrieved):
        liar = ScriptedProvider(
            default=json.dumps({
                "decision": "exclude", "exclusion_code": "E6",
                "reason": "off topic",
                "quote": "this sentence appears in no report anywhere",
                "quote_page": 1,
            })
        )
        engine = engine_for(retrieved, provider=liar)
        outcome = engine.run(actor="human:test")
        assert outcome.n_quotes_not_found == 2
        assert any("not in the report at all" in m for m in outcome.messages)
        row = retrieved.db.query_one(
            "SELECT quote_verified, quote_found_on, verify_detail "
            "FROM fulltext_decision LIMIT 1"
        )
        assert row["quote_verified"] == 0
        assert row["quote_found_on"] is None
        assert "do not appear anywhere" in row["verify_detail"]
        assert all(i["reason"] == "quote_failed" for i in engine.review_queue())

    def test_an_invented_quotation_does_not_stop_the_decision_being_recorded(
        self, retrieved
    ):
        liar = ScriptedProvider(
            default=json.dumps({
                "decision": "exclude", "exclusion_code": "E6", "reason": "off topic",
                "quote": "words that are simply not there at all", "quote_page": 1,
            })
        )
        engine = engine_for(retrieved, provider=liar)
        engine.run(actor="human:test")
        # The decision stands but is marked for review, rather than being
        # thrown away or quietly accepted.
        statuses = retrieved.db.query("SELECT decision, needs_review FROM fulltext_status")
        assert statuses and all(row["needs_review"] == 1 for row in statuses)

    def test_a_right_quotation_on_the_wrong_page_is_its_own_case(self, retrieved):
        wrong_page = ScriptedProvider(
            default=json.dumps({
                "decision": "include", "reason": "eligible",
                "quote": "Participation was strongly associated with support",
                "quote_page": 1,
            })
        )
        engine = engine_for(retrieved, provider=wrong_page)
        outcome = engine.run(actor="human:test", limit=1)
        assert outcome.n_quotes_elsewhere == 1
        assert outcome.n_quotes_not_found == 0
        row = retrieved.db.query_one(
            "SELECT quote_page, quote_found_on, verify_detail FROM fulltext_decision"
        )
        assert row["quote_page"] == 1 and row["quote_found_on"] == 2
        assert "not page 1 as cited" in row["verify_detail"]

    def test_an_exclusion_quoting_nothing_is_queued(self, retrieved):
        silent = ScriptedProvider(
            default=json.dumps({
                "decision": "exclude", "exclusion_code": "E7",
                "reason": "no usable outcome", "quote": "", "quote_page": None,
            })
        )
        engine = engine_for(retrieved, provider=silent)
        engine.run(actor="human:test")
        assert all(i["reason"] == "quote_failed" for i in engine.review_queue())
        assert "without quoting" in engine.review_queue()[0]["detail"]


class TestTheHumanQueue:
    @pytest.fixture
    def assessed(self, retrieved):
        engine = engine_for(retrieved)
        engine.run(actor="human:test")
        return retrieved, engine

    def test_inclusions_are_put_in_front_of_a_human(self, assessed):
        _, engine = assessed
        reasons = {item["reason"] for item in engine.review_queue()}
        assert "confirm_inclusion" in reasons

    def test_confirming_inclusions_can_be_turned_off(self, retrieved):
        engine = engine_for(retrieved, review_all_inclusions=False)
        engine.run(actor="human:test")
        assert engine.review_queue() == []

    def test_the_queue_carries_the_assessment_and_the_page(self, assessed):
        _, engine = assessed
        item = engine.review_queue()[0]
        assert item["assessments"]
        assessment = item["assessments"][0]
        assert assessment["quote"] and assessment["quote_page"]
        assert engine.page_text(item["record_id"], assessment["quote_page"])

    def test_a_human_decision_overrides_and_is_logged(self, assessed):
        project, engine = assessed
        item = engine.review_queue()[0]
        engine.resolve(
            item["record_id"], "exclude", exclusion_code="E4",
            reason="wrong population on a closer read", actor="human:test",
        )
        row = project.db.query_one(
            "SELECT decision, source, decided_by FROM fulltext_status WHERE record_id = ?",
            (item["record_id"],),
        )
        assert row["decision"] == "exclude"
        assert row["source"] == "human"
        override = project.db.query_one(
            "SELECT * FROM human_override WHERE entity_id = ? AND stage = 'fulltext'",
            (str(item["record_id"]),),
        )
        assert override is not None
        assert project.audit.verify() == []

    def test_confirming_the_ai_is_logged_but_is_not_an_override(self, assessed):
        project, engine = assessed
        item = engine.review_queue()[0]
        engine.resolve(item["record_id"], item["current_decision"], actor="human:test")
        assert any(
            e.action == "human_review" for e in project.audit.entries(stage="fulltext")
        )

    def test_resolving_clears_the_item(self, assessed):
        _, engine = assessed
        before = len(engine.review_queue())
        item = engine.review_queue()[0]
        engine.resolve(item["record_id"], "include", actor="human:test")
        assert len(engine.review_queue()) == before - 1

    def test_an_exclusion_needs_a_code(self, assessed):
        _, engine = assessed
        item = engine.review_queue()[0]
        with pytest.raises(EligibilityError, match="needs a code"):
            engine.resolve(item["record_id"], "exclude", actor="human:test")

    def test_included_records_are_what_the_synthesis_will_use(self, assessed):
        _, engine = assessed
        included = engine.included_records()
        assert len(included) == engine.counts()["n_included"]


class TestPauseAndCost:
    def test_a_run_can_be_stopped_and_continued(self, retrieved):
        engine = engine_for(retrieved)
        state = {"n": 0}

        def stop_after_one() -> bool:
            state["n"] += 1
            return state["n"] > 1

        first = engine.run(actor="human:test", should_stop=stop_after_one)
        assert first.status == "paused" and first.n_done == 1
        second = engine.run(actor="human:test")
        assert second.n_done == 1
        assert engine.summary()["n_pending"] == 0

    def test_the_estimate_warns_that_full_texts_cost_more(self, retrieved):
        estimate = engine_for(retrieved).estimate()
        assert estimate.n_calls == 2
        assert any("cost" in note for note in estimate.notes)

    def test_a_budget_stops_the_run_without_losing_work(self, retrieved):
        engine = engine_for(retrieved, budget_usd=0.0)
        engine.client.provider.model = "claude-opus-costly"
        outcome = engine.run(actor="human:test")
        assert outcome.status == "paused"
        assert any("limit" in m for m in outcome.messages)


class TestPrompt:
    def test_the_assessor_is_told_the_exclusion_order(self):
        system = assessor_system(
            "PROTOCOL BODY",
            [{"position": 1, "code": "E1", "label": "Not research", "definition": "d"}],
        )
        assert "PROTOCOL BODY" in system
        assert "E1 — Not research" in system
        assert "name the FIRST one" in system
        assert "Do not skip to a later code" in system

    def test_the_assessor_is_warned_the_quotation_is_checked(self):
        system = assessor_system("p", [])
        assert "checked against that page automatically" in system

    def test_the_assessor_is_told_an_exclusion_here_is_final(self):
        assert "final" in assessor_system("p", [])
