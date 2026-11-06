"""The reviewer chooses how the framework is built, and the choice bites.

Two different reviews are being supported here, and they are not the same
method:

*Bottom-up* (inductive). Nobody says in advance what the constructs are.
They are derived from what the included studies turned out to measure. This
is what the application did, and only did, before this module existed.

*Top-down* (deductive). The reviewer commits to a set of constructs from
theory before the evidence is coded, and the studies are coded into them.
What makes that a priori rather than a story told afterwards is that the
commitment is dated: the constructs go into the protocol document, which is
hashed and locked, so moving them later is an amendment with a reason
attached and not a quiet edit.

So the tests below check three things, in that order: that the choice is
stored and survives, that it is part of the locked protocol, and that the
codebook step actually behaves differently -- because a dropdown that
changes nothing is worse than no dropdown at all.
"""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.core.project import Project, ProjectError
from slr_studio.synthesis import SynthesisEngine, SynthesisError, SynthesisSettings

from test_synthesis import add_relationship, add_report


def engine_for(project, provider=None, **settings):
    client = AIClient(
        project, provider or MockProvider(), stage="synthesise",
        sleep=lambda _: None,
    )
    return SynthesisEngine(project, client, SynthesisSettings(**settings))


@pytest.fixture
def corpus(project):
    """Three reports with extracted labels, enough for a codebook."""
    project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    for i in range(3):
        record = add_report(project, title=f"Study {i + 1}")
        add_relationship(
            project, record,
            predictor="community trust", outcome="resident participation",
        )
    project.protocol.lock(actor="human:test")
    return project


class TestTheSourceTheory:
    """Which theory the a priori constructs were deduced from.

    A deductive framework cannot be judged without it. "Four constructs
    were specified in advance" tells a reader nothing; "constructs drawn
    from Social Exchange Theory (Ap, 1992)" tells them what the review
    committed to and lets them disagree with it.
    """

    def test_it_round_trips(self, project):
        project.update_info(
            framework_approach="top_down",
            a_priori_constructs="Perceived benefit\nPerceived cost",
            a_priori_theory="Social Exchange Theory",
            a_priori_citation="Ap, J. (1992). Residents' perceptions on tourism "
            "impacts. Annals of Tourism Research, 19(4), 665-690.",
        )
        info = project.info()
        assert info["a_priori_theory"] == "Social Exchange Theory"
        assert "Annals of Tourism Research" in info["a_priori_citation"]

    def test_the_protocol_names_the_theory_and_its_citation(self, project):
        project.update_info(
            framework_approach="top_down",
            a_priori_constructs="Perceived benefit",
            a_priori_theory="Social Exchange Theory",
            a_priori_citation="Ap, J. (1992). Annals of Tourism Research.",
        )
        draft = project.protocol.draft()
        assert "Social Exchange Theory" in draft
        assert "Ap, J. (1992)" in draft

    def test_changing_the_theory_after_locking_needs_an_amendment(self, project):
        """Swapping the theory is a bigger change than editing a construct."""
        project.update_info(
            framework_approach="top_down",
            a_priori_constructs="Perceived benefit",
            a_priori_theory="Social Exchange Theory",
        )
        project.protocol.lock(actor="human:test")
        project.update_info(a_priori_theory="Theory of Planned Behaviour")
        assert project.protocol.summary()["has_unlocked_changes"] is True

    def test_a_bottom_up_review_does_not_claim_a_source_theory(self, project):
        """An inductive codebook was not deduced from anything."""
        project.update_info(
            framework_approach="bottom_up",
            a_priori_theory="Left over from a change of mind",
        )
        assert "Left over from a change of mind" not in project.protocol.draft()

    def test_the_methods_paragraph_names_the_theory(self, corpus):
        from slr_studio.exports.methods import _paragraphs

        corpus.update_info(
            framework_approach="top_down",
            a_priori_constructs="Perceived benefit\nPerceived cost",
            a_priori_theory="Social Exchange Theory",
            a_priori_citation="Ap, J. (1992). Annals of Tourism Research.",
        )
        corpus.protocol.lock(actor="human:test", reason="Theory recorded.")
        engine = engine_for(
            corpus, ScriptedProvider([_codebook_reply(["Perceived benefit"])])
        )
        engine.propose_codebook(actor="human:test")

        text = "\n".join(body for _, body in _paragraphs(corpus))
        assert "Social Exchange Theory" in text
        assert "Ap, J. (1992)" in text

    def test_the_methods_paragraph_copes_without_one(self, corpus):
        """Naming a theory is not compulsory; inventing one would be worse."""
        from slr_studio.exports.methods import _paragraphs

        declare(corpus, "top_down", "Perceived benefit")
        engine = engine_for(
            corpus, ScriptedProvider([_codebook_reply(["Perceived benefit"])])
        )
        engine.propose_codebook(actor="human:test")

        text = "\n".join(body for _, body in _paragraphs(corpus))
        assert "deductively" in text
        assert "None" not in text and "none" not in text.split("Construct")[-1][:200]


def declare(project, approach, constructs=""):
    """Choose the approach and lock it into the protocol, as the page does.

    Done through the protocol rather than only the database because the
    engine refuses to build a top-down codebook from constructs the locked
    protocol does not contain. That refusal is the subject of one of the
    tests below; here it is just the normal way round.
    """
    project.update_info(
        framework_approach=approach, a_priori_constructs=constructs
    )
    if project.protocol.pending_diff():
        project.protocol.lock(
            actor="human:test", reason="Framework development approach chosen."
        )


# -- 1. the choice is stored -------------------------------------------------


class TestStoringTheChoice:
    def test_a_new_review_is_bottom_up(self, project):
        """The default is the method that needs nothing declared first.

        A reviewer who has not thought about this yet has not committed to
        a set of constructs, and pretending they have would be the wrong
        way round.
        """
        assert project.info()["framework_approach"] == "bottom_up"
        assert project.info()["a_priori_constructs"] == ""

    def test_the_choice_round_trips(self, project):
        project.update_info(
            framework_approach="top_down",
            a_priori_constructs="Trust\nPerceived fairness\nParticipation",
        )
        info = project.info()
        assert info["framework_approach"] == "top_down"
        assert "Perceived fairness" in info["a_priori_constructs"]

    def test_a_meaningless_approach_is_refused(self, project):
        """Not a free-text field. Two methods are supported, not any word."""
        with pytest.raises(Exception):
            project.update_info(framework_approach="sideways")

    def test_an_older_project_opens_and_gets_the_default(self, tmp_path):
        """A review started before this release must still open.

        Someone's half-screened corpus is in one of these files. The
        migration adds the column with the inductive default, which is what
        they were in fact doing, because it was the only thing on offer.
        """
        import sqlite3

        from slr_studio import SCHEMA_VERSION

        folder = tmp_path / "old"
        project = Project.create(folder, "Older review", actor="human:test")
        path = project.db.path
        project.close()

        # Put the database back to the version before this feature: the
        # columns gone, and the stored version saying so.
        connection = sqlite3.connect(path)
        with connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("ALTER TABLE project DROP COLUMN framework_approach")
            connection.execute("ALTER TABLE project DROP COLUMN a_priori_constructs")
            connection.execute("UPDATE meta SET value = '9' WHERE key = 'schema_version'")
        connection.close()

        reopened = Project.open(path, actor="human:test")
        try:
            assert reopened.db.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert 10 in reopened.db.applied_migrations
            assert reopened.info()["framework_approach"] == "bottom_up"
            assert reopened.info()["a_priori_constructs"] == ""
        finally:
            reopened.close()


# -- 2. it is part of the protocol ------------------------------------------


class TestItIsInTheProtocol:
    def test_the_draft_states_the_approach(self, project):
        project.update_info(framework_approach="bottom_up")
        draft = project.protocol.draft()
        assert "bottom-up" in draft.lower()

    def test_a_priori_constructs_are_written_into_the_protocol(self, project):
        """Named in the document, so the commitment is dated and hashed."""
        project.update_info(
            framework_approach="top_down",
            a_priori_constructs="Place attachment\nPerceived benefit",
        )
        draft = project.protocol.draft()
        assert "Place attachment" in draft
        assert "Perceived benefit" in draft

    def test_bottom_up_does_not_list_constructs_it_has_not_got(self, project):
        """An inductive review must not look like it declared constructs."""
        project.update_info(
            framework_approach="bottom_up",
            a_priori_constructs="Left over from a change of mind",
        )
        draft = project.protocol.draft()
        assert "Left over from a change of mind" not in draft

    def test_changing_the_constructs_after_locking_needs_an_amendment(self, project):
        """The point of the whole arrangement.

        If a reviewer can revise their a priori constructs after seeing the
        evidence without that being recorded, "a priori" means nothing.
        """
        project.update_info(
            framework_approach="top_down", a_priori_constructs="Trust"
        )
        project.protocol.lock(actor="human:test")
        assert project.protocol.summary()["has_unlocked_changes"] is False

        project.update_info(a_priori_constructs="Trust\nSomething the data suggested")
        assert project.protocol.summary()["has_unlocked_changes"] is True
        assert "Something the data suggested" in project.protocol.pending_diff()


# -- 3. the codebook step behaves differently -------------------------------


def _codebook_reply(names):
    """What a model returns: a definition for each construct it was given."""
    return json.dumps(
        {
            "constructs": [
                {
                    "name": name,
                    "definition": f"What the studies measured as {name}.",
                    "includes": [f"measures of {name}"],
                    "excludes": ["anything else"],
                }
                for name in names
            ],
            "note": "",
        }
    )


class TestTopDownCodebook:
    def test_top_down_without_constructs_refuses_to_guess(self, corpus):
        """It must not quietly fall back to deriving them from the data.

        That would be an inductive codebook in a review whose protocol says
        deductive, which is the one outcome worse than an error message.
        """
        declare(corpus, "top_down", "")
        engine = engine_for(corpus)
        with pytest.raises(SynthesisError, match="no constructs"):
            engine.propose_codebook(actor="human:test")

    def test_top_down_uses_the_reviewer_s_constructs(self, corpus):
        declare(corpus, "top_down", "Trust\nPerceived fairness")
        provider = ScriptedProvider([_codebook_reply(["Trust", "Perceived fairness"])])
        engine = engine_for(corpus, provider)
        engine.propose_codebook(actor="human:test")

        names = {c["name"] for c in engine.constructs(status="proposed")}
        assert names == {"Trust", "Perceived fairness"}

    def test_the_reviewer_s_constructs_are_recorded_as_theirs(self, corpus):
        """Provenance. The names are the human's; the definitions are not.

        Recording the whole row as 'ai' would overstate what the model did,
        and recording it as 'human' without the call id would hide that a
        model wrote the definition a reviewer is about to approve.
        """
        declare(corpus, "top_down", "Trust")
        provider = ScriptedProvider([_codebook_reply(["Trust"])])
        engine = engine_for(corpus, provider)
        engine.propose_codebook(actor="human:test")

        row = next(
            c for c in engine.constructs(status="proposed") if c["name"] == "Trust"
        )
        assert row["source"] == "human"
        assert row["ai_call_id"] is not None

    def test_a_construct_the_model_invented_is_discarded_and_reported(self, corpus):
        """In a deductive review the model does not get to add constructs.

        The prompt tells it not to. This is what happens when it does
        anyway, which is the only version of that instruction that counts.
        """
        declare(corpus, "top_down", "Trust")
        provider = ScriptedProvider(
            [_codebook_reply(["Trust", "Something Nobody Asked For"])]
        )
        engine = engine_for(corpus, provider)
        outcome = engine.propose_codebook(actor="human:test")

        names = {c["name"] for c in engine.constructs(status="proposed")}
        assert names == {"Trust"}
        assert outcome.counts["n_discarded"] == 1
        assert any("Something Nobody Asked For" in m for m in outcome.messages)

    def test_a_construct_the_model_skipped_is_still_created(self, corpus):
        """The reviewer declared it, so it is in the codebook either way.

        Dropping it because the model did not mention it would let the
        model silently narrow a protocol.
        """
        declare(corpus, "top_down", "Trust\nPerceived fairness")
        provider = ScriptedProvider([_codebook_reply(["Trust"])])
        engine = engine_for(corpus, provider)
        outcome = engine.propose_codebook(actor="human:test")

        names = {c["name"] for c in engine.constructs(status="proposed")}
        assert names == {"Trust", "Perceived fairness"}
        assert any("definition" in m.lower() for m in outcome.messages)

    def test_the_reviewer_s_own_definition_is_kept(self, corpus):
        """`Name — definition` lets a reviewer write the definition too."""
        declare(
            corpus,
            "top_down",
            "Trust — confidence that the tourism authority will act fairly",
        )
        provider = ScriptedProvider([_codebook_reply(["Trust"])])
        engine = engine_for(corpus, provider)
        engine.propose_codebook(actor="human:test")

        row = next(c for c in engine.constructs(status="proposed"))
        assert "confidence that the tourism authority" in row["definition"]

    def test_the_prompt_carries_the_constructs_and_forbids_additions(self, corpus):
        declare(corpus, "top_down", "Place attachment\nPerceived benefit")
        provider = ScriptedProvider(
            [_codebook_reply(["Place attachment", "Perceived benefit"])]
        )
        engine = engine_for(corpus, provider)
        engine.propose_codebook(actor="human:test")

        assert len(provider.calls) == 1
        sent = provider.calls[0]["system"] + provider.calls[0]["user"]
        assert "Place attachment" in sent
        assert "Perceived benefit" in sent
        # The model is told, in terms, that the list is closed.
        assert "do not add" in sent.lower()

    def test_the_bottom_up_prompt_does_not_name_any_construct(self, corpus):
        """Otherwise an inductive proposal is being led by the hand."""
        declare(corpus, "bottom_up", "Place attachment\nPerceived benefit")
        provider = ScriptedProvider([_codebook_reply(["Whatever the data said"])])
        engine = engine_for(corpus, provider)
        engine.propose_codebook(actor="human:test")

        sent = provider.calls[0]["system"] + provider.calls[0]["user"]
        assert "Place attachment" not in sent

    def test_constructs_not_in_the_locked_protocol_are_refused(self, corpus):
        """A priori means in the locked protocol, not in the form.

        Otherwise the claim is empty: a reviewer could read the evidence,
        type a different set of constructs into the box, and get a codebook
        that the protocol document never mentioned. The refusal names the
        remedy, which is to record the amendment and its reason.
        """
        declare(corpus, "top_down", "Trust")
        corpus.update_info(a_priori_constructs="Trust\nWhatever the data showed")

        engine = engine_for(corpus, ScriptedProvider([_codebook_reply(["Trust"])]))
        with pytest.raises(SynthesisError, match="amend"):
            engine.propose_codebook(actor="human:test")

    def test_an_unrelated_unlocked_edit_does_not_block_it(self, corpus):
        """Only a change to the constructs themselves blocks the step.

        A reviewer who has retyped the contact email has not revised their
        theory, and refusing to proceed over that would teach them to lock
        amendments without reading them.
        """
        declare(corpus, "top_down", "Trust")
        corpus.update_info(contact_email="someone@example.edu")

        engine = engine_for(corpus, ScriptedProvider([_codebook_reply(["Trust"])]))
        outcome = engine.propose_codebook(actor="human:test")
        assert outcome.counts["n_proposed"] == 1

    def test_a_proposal_still_needs_a_human(self, corpus):
        """Deductive or not, nothing is approved by the model."""
        declare(corpus, "top_down", "Trust")
        provider = ScriptedProvider([_codebook_reply(["Trust"])])
        engine = engine_for(corpus, provider)
        engine.propose_codebook(actor="human:test")
        assert engine.codebook_approved() is False


class TestTheMethodsSectionSaysWhich:
    """A reader cannot interpret a framework without knowing which it was.

    The Methods draft is generated from the database and cannot be switched
    off, so this is the sentence a journal actually receives.
    """

    def _methods(self, project):
        from slr_studio.exports.methods import _paragraphs

        return "\n".join(body for _, body in _paragraphs(project))

    def test_a_bottom_up_review_says_the_codebook_was_derived(self, corpus):
        declare(corpus, "bottom_up")
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        text = self._methods(corpus)
        assert "inductively" in text
        assert "no constructs were specified in advance" in text

    def test_a_top_down_review_names_the_protocol_version(self, corpus):
        """"A priori" is a claim about a date, so the date is cited."""
        declare(corpus, "top_down", "Trust\nPerceived fairness")
        engine = engine_for(corpus, ScriptedProvider([_codebook_reply(["Trust"])]))
        engine.propose_codebook(actor="human:test")

        text = self._methods(corpus)
        version = corpus.protocol.current()
        assert "deductively" in text
        assert f"version {version.version}" in text
        assert version.short_hash in text
        assert "2 construct(s)" in text


class TestBottomUpIsUnchanged:
    def test_bottom_up_still_derives_constructs_from_the_labels(self, corpus):
        engine = engine_for(corpus)
        outcome = engine.propose_codebook(actor="human:test")
        assert outcome.counts["n_proposed"] > 0
        assert outcome.counts["n_labels"] > 0

    def test_bottom_up_ignores_constructs_left_in_the_field(self, corpus):
        """Switching back to inductive must not keep using the old list."""
        declare(corpus, "bottom_up", "Trust\nPerceived fairness")
        engine = engine_for(corpus)
        outcome = engine.propose_codebook(actor="human:test")
        assert outcome.counts.get("n_discarded", 0) == 0
        assert outcome.counts["n_proposed"] > 0
