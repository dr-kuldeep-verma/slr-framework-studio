"""The framework builder over a project with a graded inventory.

The one rule that matters more than the rest: a proposition may cite only
links that exist in the graded inventory, and anything else is rejected rather
than trimmed. A framework whose propositions cite evidence that is not there
is the failure this whole application is built to prevent, so it is tested
from several directions -- a made-up id, a plausible id that was never graded,
a proposition citing nothing at all, and a model that returns a mixture.
"""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.framework import FrameworkEngine, FrameworkError, FrameworkSettings
from slr_studio.synthesis import SynthesisEngine, SynthesisSettings


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def graded(tmp_path):
    """A demonstration project carried through to a graded inventory."""
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", synthesise=True)
    yield project
    project.close()


def engine_for(project, provider=None, **settings):
    client = AIClient(
        project, provider or MockProvider(), stage="framework",
        sleep=lambda _: None,
    )
    return FrameworkEngine(project, client, FrameworkSettings(**settings))


def link_ids(project) -> list[int]:
    return [row["id"] for row in project.db.query("SELECT id FROM graded_link")]


def proposition_json(cited, *, statement="Trust raises participation."):
    return json.dumps(
        {
            "propositions": [
                {
                    "statement": statement,
                    "mechanism": "Trust lowers the cost of speaking up.",
                    "theories": ["social exchange theory"],
                    "moderators": "community size",
                    "boundary_conditions": "Where a forum exists.",
                    "falsifying_test": "A trust intervention that moves nothing.",
                    "element_kind": "evidenced_link",
                    "cited_links": cited,
                }
            ]
        }
    )


# -- themes -----------------------------------------------------------------


class TestThemes:
    def test_themes_are_proposed_from_the_graded_constructs(self, graded):
        engine = engine_for(graded)
        outcome = engine.propose_themes(actor="human:test")
        assert outcome.counts["n_themes"] >= 1
        assert engine.themes()

    def test_a_theme_is_not_approved_by_proposing_it(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        assert all(t["status"] == "proposed" for t in engine.themes())

    def test_the_human_can_approve_and_reject(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        themes = engine.themes()
        engine.decide_theme(themes[0]["id"], approved=True, actor="human:test")
        assert engine.themes()[0]["status"] == "approved"

    def test_a_rejected_theme_gets_no_figure(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        for theme in engine.themes():
            engine.decide_theme(
                theme["id"], approved=False, reason="not a theme",
                actor="human:test",
            )
        outcome = engine.draw_evidence_maps(actor="human:test")
        assert outcome.counts["n_figures"] == 0

    def test_every_graded_construct_lands_in_some_theme(self, graded):
        """A construct left out of every theme is a link left out of every
        figure, which is how evidence quietly disappears.
        """
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        assigned = {
            row["construct_id"]
            for row in graded.db.query("SELECT construct_id FROM theme_member")
        }
        in_links = {
            row["predictor_construct"] for row in graded.db.query(
                "SELECT predictor_construct FROM graded_link"
            )
        } | {
            row["outcome_construct"] for row in graded.db.query(
                "SELECT outcome_construct FROM graded_link"
            )
        }
        assert in_links <= assigned


# -- propositions: the grounding rule ----------------------------------------


class TestPropositionsCiteOnlyGradedLinks:
    def test_a_proposition_citing_a_real_link_is_kept(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        provider = ScriptedProvider(
            default=proposition_json([f"L{link_ids(graded)[0]}"])
        )
        outcome = engine_for(graded, provider).draft_propositions(actor="human:test")
        assert outcome.counts["n_kept"] >= 1
        assert outcome.counts["n_rejected"] == 0

    def test_a_proposition_citing_an_invented_link_is_rejected(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        provider = ScriptedProvider(default=proposition_json(["L9999"]))
        outcome = engine_for(graded, provider).draft_propositions(actor="human:test")
        assert outcome.counts["n_rejected"] >= 1
        assert outcome.counts["n_kept"] == 0

    def test_the_rejected_proposition_is_not_stored_as_a_proposition(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        provider = ScriptedProvider(default=proposition_json(["L9999"]))
        engine_for(graded, provider).draft_propositions(actor="human:test")
        assert graded.db.scalar(
            "SELECT COUNT(*) FROM proposition WHERE status <> 'rejected'"
        ) == 0

    def test_the_rejection_says_which_link_was_not_in_the_inventory(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        provider = ScriptedProvider(default=proposition_json(["L9999"]))
        outcome = engine_for(graded, provider).draft_propositions(actor="human:test")
        assert any("9999" in message for message in outcome.messages)

    def test_a_proposition_citing_nothing_is_rejected(self, graded):
        """An evidenced link with no link is not evidenced."""
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        provider = ScriptedProvider(default=proposition_json([]))
        outcome = engine_for(graded, provider).draft_propositions(actor="human:test")
        assert outcome.counts["n_rejected"] >= 1

    def test_one_bad_citation_rejects_the_whole_proposition(self, graded):
        """Not a trim: a proposition is an argument, and an argument resting
        partly on evidence that does not exist is not repaired by deleting
        the citation.
        """
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        real = link_ids(graded)[0]
        provider = ScriptedProvider(default=proposition_json([f"L{real}", "L9999"]))
        outcome = engine_for(graded, provider).draft_propositions(actor="human:test")
        assert outcome.counts["n_rejected"] >= 1
        assert outcome.counts["n_kept"] == 0

    def test_a_mechanism_may_rest_on_no_link_because_it_claims_none(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        payload = json.loads(proposition_json([]))
        payload["propositions"][0]["element_kind"] = "proposed_mechanism"
        provider = ScriptedProvider(default=json.dumps(payload))
        outcome = engine_for(graded, provider).draft_propositions(actor="human:test")
        assert outcome.counts["n_kept"] >= 1

    def test_every_stored_citation_resolves(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        engine_for(graded).draft_propositions(actor="human:test")
        orphans = graded.db.scalar(
            "SELECT COUNT(*) FROM proposition_link l "
            "WHERE NOT EXISTS (SELECT 1 FROM graded_link g WHERE g.id = l.graded_link_id)"
        )
        assert orphans == 0


class TestPropositionContent:
    def test_a_proposition_without_a_falsifying_test_is_refused(self):
        from slr_studio.framework.schemas import ProposedProposition

        with pytest.raises(ValueError):
            ProposedProposition(
                statement="Trust raises participation.",
                mechanism="m",
                falsifying_test="",
                cited_links=["L1"],
            )

    def test_a_proposition_without_a_mechanism_is_refused(self):
        from slr_studio.framework.schemas import ProposedProposition

        with pytest.raises(ValueError):
            ProposedProposition(
                statement="Trust raises participation.",
                mechanism="",
                falsifying_test="t",
                cited_links=["L1"],
            )

    def test_the_theories_the_user_listed_are_what_the_model_is_given(self, graded):
        from slr_studio.framework.prompts import proposition_system

        system = proposition_system(
            "THE PROTOCOL", ["social exchange theory"], [], []
        )
        assert "social exchange theory" in system

    def test_a_citation_is_accepted_with_or_without_the_l(self):
        from slr_studio.framework.schemas import ProposedProposition

        prop = ProposedProposition(
            statement="Trust raises participation.", mechanism="m",
            falsifying_test="t", cited_links=["L4", "7"],
        )
        assert prop.link_ids() == [4, 7]

    def test_a_citation_that_is_not_a_number_is_refused(self):
        from slr_studio.framework.schemas import ProposedProposition

        with pytest.raises(ValueError):
            ProposedProposition(
                statement="Trust raises participation.", mechanism="m",
                falsifying_test="t", cited_links=["the trust paper"],
            )


# -- the human's say ---------------------------------------------------------


class TestTheHumanDecides:
    def test_a_proposition_can_be_accepted(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        engine.draft_propositions(actor="human:test")
        first = engine.propositions()[0]
        engine.decide_proposition(first["id"], accepted=True, actor="human:test")
        assert engine.propositions()[0]["status"] == "accepted"

    def test_rejecting_one_records_why(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        engine.draft_propositions(actor="human:test")
        first = engine.propositions()[0]
        engine.decide_proposition(
            first["id"], accepted=False, reason="mechanism is circular",
            actor="human:test",
        )
        assert engine.propositions()[0]["status_reason"] == "mechanism is circular"

    def test_an_edit_keeps_what_the_model_said(self, graded):
        """The audit trail has to show the model's wording and the human's."""
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        engine.draft_propositions(actor="human:test")
        first = engine.propositions()[0]
        engine.edit_proposition(
            first["id"], statement="A clearer statement.", actor="human:test"
        )
        edited = engine.propositions()[0]
        assert edited["statement"] == "A clearer statement."
        assert first["statement"] in edited["edited_from"]


# -- ordering ----------------------------------------------------------------


class TestOrdering:
    def test_propositions_cannot_be_drafted_before_the_inventory_exists(
        self, tmp_path
    ):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "bare", extract=True)
        try:
            engine = engine_for(project)
            with pytest.raises(FrameworkError) as caught:
                engine.draft_propositions(actor="human:test")
            assert "graded" in str(caught.value).casefold()
        finally:
            project.close()

    def test_figures_cannot_be_drawn_before_themes_exist(self, graded):
        engine = engine_for(graded)
        with pytest.raises(FrameworkError):
            engine.draw_evidence_maps(actor="human:test")


class TestIntegrity:
    def test_the_audit_chain_survives_the_whole_stage(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        for theme in engine.themes():
            engine.decide_theme(theme["id"], approved=True, actor="human:test")
        engine.draft_propositions(actor="human:test")
        engine.draw_evidence_maps(actor="human:test")
        engine.draw_integrated_diagram(actor="human:test")
        assert graded.audit.verify() == []

    def test_every_figure_is_recorded_with_its_counts(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        for theme in engine.themes():
            engine.decide_theme(theme["id"], approved=True, actor="human:test")
        engine.draw_evidence_maps(actor="human:test")
        rows = graded.db.query("SELECT * FROM figure")
        assert rows
        for row in rows:
            assert row["n_links"] >= 0 and row["filename"]

    def test_the_figures_only_draw_graded_links(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        for theme in engine.themes():
            engine.decide_theme(theme["id"], approved=True, actor="human:test")
        engine.draw_evidence_maps(actor="human:test")
        drawn = sum(row["n_links"] for row in graded.db.query(
            "SELECT n_links FROM figure WHERE kind = 'evidence_map' "
            "AND format = 'svg'"
        ))
        graded_links = graded.db.scalar("SELECT COUNT(*) FROM graded_link")
        assert drawn <= graded_links


class TestTheOfflineDoubleAnswersFrameworkPrompts:
    """The demonstration project runs with no API key, so the offline double
    has to answer this stage too -- and its propositions must obey the same
    grounding rule as a real model's.
    """

    def test_it_proposes_themes(self, graded):
        outcome = engine_for(graded).propose_themes(actor="human:test")
        assert outcome.counts["n_themes"] >= 1

    def test_it_drafts_propositions_that_cite_real_links(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        outcome = engine.draft_propositions(actor="human:test")
        assert outcome.counts["n_kept"] >= 1
        assert outcome.counts["n_rejected"] == 0

    def test_each_proposition_it_drafts_has_a_falsifying_test(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        engine.draft_propositions(actor="human:test")
        for proposition in engine.propositions():
            assert proposition["falsifying_test"]
            assert proposition["mechanism"]


class TestNoLinkDisappears:
    """One figure per theme means a link between two themes is in none of
    them. That is tolerable; leaving the reviewer to notice is not.
    """

    def test_links_that_no_evidence_map_shows_are_reported(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        for theme in engine.themes():
            engine.decide_theme(theme["id"], approved=True, actor="human:test")
        outcome = engine.draw_evidence_maps(actor="human:test")

        drawn = outcome.counts["n_links"]
        total = graded.db.scalar("SELECT COUNT(*) FROM graded_link")
        assert outcome.counts["n_not_drawn"] == total - drawn
        if outcome.counts["n_not_drawn"]:
            assert any(
                "no evidence map" in message.casefold()
                for message in outcome.messages
            )

    def test_the_links_in_no_map_can_be_listed(self, graded):
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        for theme in engine.themes():
            engine.decide_theme(theme["id"], approved=True, actor="human:test")
        engine.draw_evidence_maps(actor="human:test")
        for link in engine.links_not_drawn():
            assert link["predictor_name"] and link["outcome_name"]

    def test_the_integrated_diagram_still_shows_them(self, graded):
        """Whatever the themes do, the whole framework is drawn once."""
        engine = engine_for(graded)
        engine.propose_themes(actor="human:test")
        outcome = engine.draw_integrated_diagram(actor="human:test")
        assert outcome.counts["n_evidenced"] >= 1
