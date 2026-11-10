"""Screening: the statistics, the pipeline, and who gets the last word."""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.screening import ScreeningEngine, ScreeningSettings
from slr_studio.screening.engine import ScreeningError
from slr_studio.screening.prompts import (
    PERSONAS,
    PROMPT_VERSION,
    adjudicator_system,
    render_record,
    screener_system,
)
from slr_studio.screening.stats import (
    agreement,
    cohens_kappa,
    collapse_unclear,
    interpret_kappa,
    pabak,
    percentage_agreement,
)


# -- statistics ------------------------------------------------------------


class TestAgreementStatistics:
    def test_kappa_matches_a_worked_example(self):
        # 2x2 with 20 / 5 / 10 / 15: p_o = 0.70, p_e = 0.50, kappa = 0.40.
        a = ["y"] * 25 + ["n"] * 25
        b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
        kappa, p_o, p_e = cohens_kappa(a, b)
        assert p_o == pytest.approx(0.70)
        assert p_e == pytest.approx(0.50)
        assert kappa == pytest.approx(0.40)

    def test_perfect_agreement_across_two_categories_gives_kappa_one(self):
        a = ["include", "exclude", "include", "exclude"]
        assert cohens_kappa(a, list(a))[0] == pytest.approx(1.0)

    def test_kappa_is_undefined_rather_than_zero_for_a_single_category(self):
        result = agreement(["include"] * 10, ["include"] * 10)
        assert result.kappa is None
        assert "undefined" in result.note

    def test_kappa_is_negative_when_reviewers_disagree_systematically(self):
        a = ["include", "exclude"] * 10
        b = ["exclude", "include"] * 10
        assert cohens_kappa(a, b)[0] < 0

    def test_pabak_is_twice_observed_minus_one_for_two_categories(self):
        assert pabak(0.9, 2) == pytest.approx(0.8)
        assert pabak(0.5, 2) == pytest.approx(0.0)

    def test_pabak_generalises_to_three_categories(self):
        assert pabak(1.0, 3) == pytest.approx(1.0)
        assert pabak(1 / 3, 3) == pytest.approx(0.0)

    def test_pabak_rescues_the_lopsided_case_kappa_punishes(self):
        # 98 agreed excludes and 2 disagreements: 98% agreement, but kappa
        # collapses because one category dominates. This is the everyday
        # situation in a systematic review.
        a = ["exclude"] * 98 + ["include", "include"]
        b = ["exclude"] * 98 + ["exclude", "exclude"]
        result = agreement(a, b, categories=["include", "exclude"])
        assert result.percent_agreement == pytest.approx(0.98)
        assert result.kappa is not None and result.kappa < 0.1
        assert result.pabak == pytest.approx(0.96)

    def test_percentage_agreement_is_straightforward(self):
        assert percentage_agreement(["a", "b", "c"], ["a", "b", "x"]) == pytest.approx(2 / 3)

    def test_mismatched_lengths_are_refused(self):
        with pytest.raises(ValueError, match="same records"):
            percentage_agreement(["a"], ["a", "b"])

    def test_the_confusion_matrix_uses_the_fixed_category_list(self):
        result = agreement(
            ["include"] * 3, ["include"] * 3,
            categories=["include", "exclude", "unclear"],
        )
        assert set(result.matrix) == {"include", "exclude", "unclear"}
        assert result.matrix["include"]["include"] == 3

    def test_empty_input_does_not_explode(self):
        result = agreement([], [])
        assert result.n == 0 and result.kappa is None
        assert "No records" in result.describe()

    def test_kappa_readings_follow_the_usual_bands(self):
        assert interpret_kappa(0.9) == "almost perfect"
        assert interpret_kappa(0.5) == "moderate"
        assert interpret_kappa(None) == "undefined"

    def test_unclear_collapses_towards_inclusion(self):
        assert collapse_unclear(["unclear", "exclude"]) == ["include", "exclude"]


# -- prompts ---------------------------------------------------------------


class TestPrompts:
    def test_the_two_personas_differ_in_how_they_treat_ambiguity(self):
        a = PERSONAS["screener_a"]["stance"]
        b = PERSONAS["screener_b"]["stance"]
        assert "prefer to include" in a
        assert "unclear" in b
        assert a != b

    def test_a_screener_is_told_the_protocol_and_the_codes(self):
        system = screener_system(
            "screener_a", "THE PROTOCOL BODY",
            [{"position": 1, "code": "E1", "label": "Not research", "definition": "d"}],
        )
        assert "THE PROTOCOL BODY" in system
        assert "E1 — Not research" in system

    def test_a_screener_is_never_told_what_the_other_said(self):
        system = screener_system("screener_a", "p", [])
        assert "screener b" not in system.casefold()

    def test_the_record_shown_withholds_the_database(self):
        text = render_record(
            {"title": "T", "abstract": "A", "source_database": "Scopus", "year": 2020}
        )
        assert "Scopus" not in text
        assert "T" in text and "A" in text

    def test_a_record_with_no_abstract_says_so_plainly(self):
        assert "(no abstract in this record)" in render_record({"title": "T"})

    def test_authors_and_keywords_survive_being_stored_as_json(self):
        text = render_record(
            {"title": "T", "authors": json.dumps(["Menon, R."]),
             "keywords": json.dumps(["trust"])}
        )
        assert "Menon, R." in text and "trust" in text

    def test_a_long_author_list_is_summarised(self):
        text = render_record({"title": "T", "authors": [f"A{i}" for i in range(20)]})
        assert "and 12 more" in text

    def test_the_adjudicator_sees_both_answers(self):
        system = adjudicator_system("p", [])
        assert "adjudicating" in system.casefold()
        assert "sided_with" in system


# -- the pipeline ----------------------------------------------------------


@pytest.fixture
def ready(imported_project):
    """A deduplicated project with a locked protocol, ready to screen."""
    from slr_studio.dedup import DedupEngine

    DedupEngine(imported_project).run()
    imported_project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    imported_project.protocol.lock(actor="human:test")
    return imported_project


def make_engine(project, provider=None, **settings):
    client = AIClient(project, provider or MockProvider(), stage="screen",
                      sleep=lambda _: None)
    return ScreeningEngine(project, client, ScreeningSettings(**settings))


class TestGates:
    def test_a_run_the_program_died_under_is_marked_interrupted(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("Testing an interrupted run")
        crashed = engine._start_run("full", 5, 1)  # what a crash leaves behind
        engine.run_full()
        row = ready.db.query_one(
            "SELECT status, finished_at, note FROM screening_run WHERE id = ?", (crashed,)
        )
        assert row["status"] == "failed" and row["finished_at"]
        assert row["note"].startswith("Interrupted")
        assert any(e.action == "run_interrupted" for e in ready.audit.entries())

    def test_a_run_stopped_by_a_hard_error_is_not_left_running(self, ready):
        from slr_studio.ai.base import ProviderError
        from slr_studio.ai.providers import Provider

        class Refusing(Provider):
            name = "refusing"

            def complete(self, **kwargs):
                error = ProviderError("request rejected")
                error.retryable = False
                raise error

        engine = make_engine(ready, provider=Refusing("m"))
        engine.waive_pilot("Testing a hard failure")
        with pytest.raises(ProviderError):
            engine.run_full()
        row = ready.db.query_one("SELECT status, note FROM screening_run ORDER BY id DESC")
        assert row["status"] == "failed" and "request rejected" in row["note"]

    def test_screening_without_a_locked_protocol_is_refused(self, imported_project):
        engine = make_engine(imported_project)
        with pytest.raises(ScreeningError, match="has not been locked"):
            engine.run_full()

    def test_the_full_run_is_blocked_until_the_pilot_has_run(self, ready):
        engine = make_engine(ready)
        with pytest.raises(ScreeningError, match="Run the pilot first"):
            engine.run_full()

    def test_waiving_the_pilot_needs_a_reason(self, ready):
        engine = make_engine(ready)
        with pytest.raises(ScreeningError, match="needs a reason"):
            engine.waive_pilot("")

    def test_a_waiver_is_recorded_and_unblocks_the_run(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("Protocol reused from a published review")
        assert engine.pilot_waived() is True
        row = ready.db.query_one("SELECT * FROM screening_waiver")
        assert row["reason"].startswith("Protocol reused")
        assert any(e.action == "pilot_waived" for e in ready.audit.entries())
        engine.run_full()  # no longer raises

    def test_the_pilot_needs_some_labels_first(self, ready):
        engine = make_engine(ready)
        with pytest.raises(ScreeningError, match="Label some pilot records"):
            engine.run_pilot()


class TestPilot:
    def test_the_sample_is_the_same_every_time(self, ready):
        first = [r["id"] for r in make_engine(ready, pilot_size=5).pilot_sample()]
        second = [r["id"] for r in make_engine(ready, pilot_size=5).pilot_sample()]
        assert first == second and len(first) == 5

    def test_a_pilot_bigger_than_the_corpus_takes_everything(self, ready):
        sample = make_engine(ready, pilot_size=1000).pilot_sample()
        assert len(sample) == ready.counts()["n_screened"]

    def test_the_pilot_compares_the_reviewer_with_the_ai(self, ready):
        engine = make_engine(ready, pilot_size=4)
        for record in engine.pilot_sample():
            engine.record_pilot_label(record["id"], "include", actor="human:test")
        outcome = engine.run_pilot(actor="human:test")
        assert outcome.agreement is not None
        assert outcome.agreement.n == 4
        assert engine.pilot_done() is True

    def test_low_agreement_is_reported_rather_than_blocking(self, ready):
        engine = make_engine(ready, pilot_size=4, pilot_target_kappa=0.99)
        for index, record in enumerate(engine.pilot_sample()):
            engine.record_pilot_label(
                record["id"], "exclude" if index % 2 else "include",
                exclusion_code="E6", actor="human:test",
            )
        outcome = engine.run_pilot(actor="human:test")
        assert outcome.agreement.percent_agreement < 1.0
        assert any("below" in m for m in outcome.messages)

    def test_a_pilot_label_is_logged(self, ready):
        engine = make_engine(ready, pilot_size=2)
        record = engine.pilot_sample()[0]
        engine.record_pilot_label(record["id"], "include", actor="human:test")
        assert any(e.action == "pilot_label" for e in ready.audit.entries())

    def test_an_invented_category_is_refused(self, ready):
        engine = make_engine(ready, pilot_size=2)
        record = engine.pilot_sample()[0]
        with pytest.raises(ScreeningError, match="not one of"):
            engine.record_pilot_label(record["id"], "probably", actor="human:test")

    def test_the_agreement_figure_is_stored_not_recomputed(self, ready):
        engine = make_engine(ready, pilot_size=3)
        for record in engine.pilot_sample():
            engine.record_pilot_label(record["id"], "include", actor="human:test")
        engine.run_pilot(actor="human:test")
        stored = engine.latest_agreement("pilot:human_vs_ai")
        assert stored is not None and stored["n"] == 3


class TestFullRun:
    @pytest.fixture
    def screened(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test fixture")
        outcome = engine.run_full(actor="human:test")
        return ready, engine, outcome

    def test_every_record_gets_two_screeners(self, screened):
        project, engine, outcome = screened
        n = outcome.n_records
        for role in ("screener_a", "screener_b"):
            assert project.db.scalar(
                "SELECT COUNT(*) FROM screening_decision WHERE reviewer_role = ?",
                (role,),
            ) == n

    def test_every_active_record_ends_with_a_status(self, screened):
        project, engine, _ = screened
        counts = engine.counts()
        assert counts["n_not_yet_screened"] == 0
        assert (
            counts["n_included"] + counts["n_excluded"] + counts["n_unclear"]
            == counts["n_to_screen"]
        )

    def test_disagreements_are_adjudicated(self, screened):
        _, _, outcome = screened
        assert outcome.n_conflicts > 0
        assert outcome.n_adjudicated > 0

    def test_an_adjudicated_record_still_goes_to_a_human(self, screened):
        project, engine, _ = screened
        reasons = {item["reason"] for item in engine.review_queue()}
        assert "conflict" in reasons

    def test_agreement_between_the_screeners_is_reported(self, screened):
        _, engine, outcome = screened
        assert outcome.agreement is not None
        assert engine.latest_agreement("full:a_vs_b") is not None

    def test_exclusions_name_the_first_failing_code(self, ready):
        excluder = ScriptedProvider(
            default=json.dumps({
                "decision": "exclude",
                "exclusion_code": "E4",
                "reason": "the participants are not residents",
                "quote": "",
            })
        )
        engine = make_engine(ready, provider=excluder)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test")
        rows = ready.db.query(
            "SELECT exclusion_code FROM screening_status WHERE decision = 'exclude'"
        )
        assert rows and all(r["exclusion_code"] == "E4" for r in rows)
        assert engine.exclusion_reasons()[0]["exclusion_code"] == "E4"

    def test_a_second_run_asks_nothing_again(self, screened):
        project, engine, _ = screened
        before = project.db.scalar("SELECT COUNT(*) FROM ai_call")
        second = engine.run_full(actor="human:test")
        assert second.n_records == 0
        assert project.db.scalar("SELECT COUNT(*) FROM ai_call") == before

    def test_the_run_is_recorded_with_its_model_and_prompt_version(self, screened):
        project, _, outcome = screened
        row = project.db.query_one(
            "SELECT * FROM screening_run WHERE id = ?", (outcome.run_id,)
        )
        assert row["model"] and row["prompt_version"] == PROMPT_VERSION
        assert row["protocol_version"] is not None
        assert row["status"] == "finished"

    def test_the_whole_run_is_in_the_audit_log_and_verifies(self, screened):
        project, _, _ = screened
        actions = [e.action for e in project.audit.entries(stage="screen")]
        assert "screening_run" in actions
        assert project.audit.verify() == []


class TestQuotations:
    def test_a_quotation_that_is_in_the_record_is_marked_verified(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test")
        verified = ready.db.scalar(
            "SELECT COUNT(*) FROM screening_decision "
            "WHERE quote <> '' AND quote_verified = 1"
        )
        assert verified > 0

    def test_an_invented_quotation_is_flagged(self, ready):
        provider = ScriptedProvider(
            default=json.dumps({
                "decision": "include",
                "reason": "on topic",
                "quote": "a sentence that appears nowhere in the record at all",
            })
        )
        engine = make_engine(ready, provider=provider)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test", limit=1)
        row = ready.db.query_one(
            "SELECT quote_verified FROM screening_decision WHERE quote <> '' LIMIT 1"
        )
        assert row["quote_verified"] == 0
        assert engine.summary()["unverified_quotes"] > 0

    def test_whitespace_differences_do_not_count_as_fabrication(self, ready):
        engine = make_engine(ready)
        record = engine.pending_records()[0]
        words = (record["title"] or "").split()[:6]
        assert engine._quote_in_record("  ".join(words).upper(), record) is True

    def test_an_empty_quote_is_not_treated_as_verified(self, ready):
        engine = make_engine(ready)
        assert engine._quote_in_record("", {"title": "anything"}) is False


class TestQualityControl:
    def test_the_qc_sample_is_reproducible(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test")
        first, _ = engine.qc_candidates()
        second, _ = make_engine(ready).qc_candidates()
        assert first == second

    def test_keywords_come_from_the_protocol_itself(self, ready):
        engine = make_engine(ready)
        keywords = engine.protocol_keywords()
        assert "participation" in keywords
        assert "study" not in keywords  # stop-worded

    def test_a_rescued_exclusion_is_queued_not_silently_reversed(self, ready):
        # Screen everything with a provider that excludes, then re-read with
        # one that disagrees.
        excluder = ScriptedProvider(
            default=json.dumps({
                "decision": "exclude", "exclusion_code": "E6",
                "reason": "off topic", "quote": "",
            })
        )
        engine = make_engine(ready, provider=excluder)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test")
        excluded = ready.db.query("SELECT record_id FROM screening_status "
                                  "WHERE decision = 'exclude'")
        assert excluded, "the excluding provider should have excluded everything"
        rescuer = ScriptedProvider(
            default=json.dumps({"decision": "include", "reason": "on topic after all"})
        )
        qc_engine = make_engine(ready, provider=rescuer)
        outcome = qc_engine.run_qc(actor="human:test")
        assert outcome.n_qc_rescued > 0
        # The status is untouched; only a review item and a flag appear.
        still_excluded = ready.db.scalar(
            "SELECT decision FROM screening_status WHERE record_id = ?",
            (excluded[0]["record_id"],),
        )
        assert still_excluded == "exclude"
        assert any(i["reason"] == "qc_rescue" for i in qc_engine.review_queue())

    def test_qc_is_recorded_as_its_own_run(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test")
        engine.run_qc(actor="human:test")
        kinds = {r["kind"] for r in engine.runs()}
        assert {"full", "qc"} <= kinds


class TestHumanDecisions:
    @pytest.fixture
    def screened(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test")
        engine.run_full(actor="human:test")
        return ready, engine

    def test_the_human_overrides_the_ai(self, screened):
        project, engine = screened
        item = engine.review_queue()[0]
        before = project.db.scalar(
            "SELECT decision FROM screening_status WHERE record_id = ?",
            (item["record_id"],),
        )
        other = "exclude" if before != "exclude" else "include"
        engine.resolve(
            item["record_id"], other, exclusion_code="E6" if other == "exclude" else "",
            reason="read it myself", actor="human:test",
        )
        after = project.db.query_one(
            "SELECT decision, source, decided_by FROM screening_status WHERE record_id = ?",
            (item["record_id"],),
        )
        assert after["decision"] == other
        assert after["source"] == "human"
        assert after["decided_by"] == "human:test"

    def test_an_override_is_recorded_as_one(self, screened):
        project, engine = screened
        item = next(i for i in engine.review_queue() if i["current_decision"] == "include")
        engine.resolve(item["record_id"], "exclude", exclusion_code="E6",
                       reason="wrong population", actor="human:test")
        row = project.db.query_one(
            "SELECT * FROM human_override WHERE entity_id = ?",
            (str(item["record_id"]),),
        )
        assert row is not None and row["stage"] == "screen"
        assert project.audit.verify() == []

    def test_confirming_the_ai_is_logged_but_is_not_an_override(self, screened):
        project, engine = screened
        item = next(i for i in engine.review_queue() if i["current_decision"])
        engine.resolve(item["record_id"], item["current_decision"],
                       exclusion_code=item["current_code"] or "",
                       actor="human:test")
        assert any(e.action == "human_review" for e in project.audit.entries())

    def test_resolving_clears_the_item_from_the_queue(self, screened):
        _, engine = screened
        item = engine.review_queue()[0]
        before = len(engine.review_queue())
        engine.resolve(item["record_id"], "include", actor="human:test")
        assert len(engine.review_queue()) == before - 1

    def test_an_exclusion_without_a_code_is_refused(self, screened):
        _, engine = screened
        item = engine.review_queue()[0]
        with pytest.raises(ScreeningError, match="needs a code"):
            engine.resolve(item["record_id"], "exclude", actor="human:test")

    def test_the_human_decision_survives_a_rerun(self, screened):
        project, engine = screened
        item = engine.review_queue()[0]
        engine.resolve(item["record_id"], "exclude", exclusion_code="E6",
                       actor="human:test")
        engine.run_full(actor="human:test")
        assert project.db.scalar(
            "SELECT source FROM screening_status WHERE record_id = ?",
            (item["record_id"],),
        ) == "human"


class TestPauseAndResume:
    def test_a_run_can_be_stopped_and_continued(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test")
        total = len(engine.pending_records())
        state = {"done": 0}

        def stop_after_two() -> bool:
            state["done"] += 1
            return state["done"] > 2

        first = engine.run_full(actor="human:test", should_stop=stop_after_two)
        assert first.status == "paused"
        assert 0 < first.n_done < total
        assert any("Paused" in m for m in first.messages)

        second = engine.run_full(actor="human:test")
        assert second.n_done == total - first.n_done
        assert engine.counts()["n_not_yet_screened"] == 0

    def test_a_paused_run_is_recorded_as_paused(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("test")
        outcome = engine.run_full(actor="human:test", should_stop=lambda: True)
        row = ready.db.query_one(
            "SELECT status FROM screening_run WHERE id = ?", (outcome.run_id,)
        )
        assert row["status"] == "paused"

    def test_a_budget_stops_the_run_without_losing_work(self, ready):
        engine = make_engine(ready, budget_usd=0.0)
        engine.client.provider.model = "claude-opus-costly"
        engine.waive_pilot("test")
        outcome = engine.run_full(actor="human:test")
        assert outcome.status == "paused"
        assert any("limit" in m for m in outcome.messages)


class TestRevisitingUnclearDecisions:
    def test_records_left_unclear_by_the_reviewer_can_be_decided_again(self, ready):
        engine = make_engine(ready)
        engine.waive_pilot("Testing revisits")
        engine.run_full()
        ids = [r["record_id"] for r in ready.db.query("SELECT record_id FROM screening_status LIMIT 3")]
        for record_id in ids:
            engine.resolve(record_id, "unclear", reason="first pass")
        assert [r["record_id"] for r in engine.revisit_list()] == sorted(ids)

        engine.resolve(ids[0], "exclude", exclusion_code="E1", reason="read it properly")
        assert [r["record_id"] for r in engine.revisit_list()] == sorted(ids[1:])
        # The new decision replaces the current one; the earlier one stays in
        # the audit log, recorded as an override with its previous value.
        current = ready.db.query_one(
            "SELECT decision FROM screening_status WHERE record_id = ?", (ids[0],)
        )
        assert current["decision"] == "exclude"
        overrides = [e for e in ready.audit.entries()
                     if e.action == "human_override" and str(e.entity_id) == str(ids[0])]
        assert overrides and "unclear" in str(overrides[-1].detail)
