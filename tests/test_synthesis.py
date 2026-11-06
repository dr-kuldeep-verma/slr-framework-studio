"""The synthesis engine, from codebook to graded inventory.

Most of these run against a synthetic corpus built straight into the
database, with counts chosen so the right answer is known in advance: a link
six datasets agree on, a link three datasets disagree about, a pair of
reports sharing one sample, a review paper that must not vote. The real
two-report pipeline is exercised once at the end, for the wiring.
"""

from __future__ import annotations

import json

import pytest

from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.core.db import utc_now
from slr_studio.dedup import DedupEngine
from slr_studio.importers.normalise import normalise_title
from slr_studio.synthesis import (
    SynthesisEngine,
    SynthesisError,
    SynthesisSettings,
    link_key,
)
from slr_studio.synthesis.linkage import author_surnames, score_shared_dataset


# -- a corpus with known answers -------------------------------------------


def add_report(
    project,
    *,
    title: str,
    authors: str = "Rivera, A; Osei, B",
    year: int = 2021,
    country: str = "Fiji",
    design_family: str = "cross_sectional",
    sample_size: int | None = 300,
    setting: str = "coastal villages",
    concern: str = "low",
    time_horizon: str = "2019 fieldwork",
) -> int:
    """One included report with an extraction attached."""
    file_id = project.db.scalar("SELECT id FROM import_file LIMIT 1")
    if file_id is None:
        file_id = project.db.insert(
            "import_file",
            {
                "filename": "synthetic.ris", "original_path": "synthetic.ris",
                "file_sha256": "synthetic", "format": "ris", "dialect": "generic",
                "source_database": "Synthetic", "imported_at": utc_now(),
            },
        )
    position = (project.db.scalar("SELECT COUNT(*) FROM record") or 0) + 1
    record_id = project.db.insert(
        "record",
        {
            "import_file_id": file_id, "position_in_file": position,
            "raw": "synthetic", "raw_format": "ris",
            "record_hash": f"hash-{title}-{position}",
            "created_at": utc_now(), "title": title, "authors": authors,
            "year": year, "source_database": "Synthetic", "keywords": "[]",
        },
    )
    project.db.insert(
        "report_extraction",
        {
            "record_id": record_id, "coder_role": "ai", "coder": "mock",
            "country": country, "setting": setting, "population": "residents",
            "design": design_family, "design_family": design_family,
            "sample_size": sample_size, "time_horizon": time_horizon,
            "appraisal_concern": concern, "created_at": utc_now(),
        },
    )
    return record_id


def add_relationship(
    project,
    record_id: int,
    *,
    predictor: str,
    outcome: str,
    direction: str = "positive",
    basis: str = "statistical",
    position: int = 0,
) -> int:
    return project.db.insert(
        "relationship",
        {
            "record_id": record_id, "coder_role": "ai", "coder": "mock",
            "position": position, "predictor_label": predictor,
            "outcome_label": outcome, "direction": direction,
            "evidence_basis": basis, "created_at": utc_now(),
        },
    )


@pytest.fixture
def corpus(project):
    """Eight reports whose counts are known.

    - trust -> participation: six datasets agree positive, two countries
    - fairness -> participation: four positive, three negative (contested)
    - a review paper also claims trust -> participation, and must not vote
    """
    project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    project.protocol.lock(actor="human:test")

    countries = ["Fiji", "Nepal", "Fiji", "Nepal", "Peru", "Peru"]
    for i, country in enumerate(countries):
        record = add_report(
            project,
            title=f"Trust study {i + 1}",
            authors=f"Author{i}, X; Common, Y",
            country=country,
            sample_size=200 + i,
        )
        add_relationship(
            project, record,
            predictor="community trust", outcome="resident participation",
        )

    for i, direction in enumerate(["positive"] * 4 + ["negative"] * 3):
        record = add_report(
            project,
            title=f"Fairness study {i + 1}",
            authors=f"Fair{i}, Z",
            country=["Fiji", "Nepal", "Peru", "Kenya"][i % 4],
            sample_size=400 + i,
        )
        add_relationship(
            project, record,
            predictor="perceived fairness", outcome="resident participation",
            direction=direction,
        )

    review = add_report(
        project, title="A review of trust and participation",
        design_family="review", sample_size=None, authors="Scribe, R",
    )
    add_relationship(
        project, review,
        predictor="community trust", outcome="resident participation",
        basis="asserted",
    )
    return project


def engine_for(project, provider=None, **settings):
    client = AIClient(
        project, provider or MockProvider(), stage="synthesise",
        sleep=lambda _: None,
    )
    return SynthesisEngine(project, client, SynthesisSettings(**settings))


def approved_codebook(engine, names=("trust", "fairness", "participation")):
    """Skip the model: put an approved codebook in directly."""
    for name in names:
        engine.add_construct(
            name, definition=f"What studies measured as {name}.",
            includes=[name], excludes=["anything else"],
        )
    engine.approve_codebook(actor="human:test")
    return {c["name"]: c["id"] for c in engine.constructs(status="approved")}


def map_by_hand(engine, pairs, *, orientations=True):
    """Map labels straight onto constructs, as a reviewer would after edits."""
    constructs = {c["name"]: c for c in engine.constructs(status="approved")}
    from slr_studio.importers.normalise import normalise_title

    for label, role, construct_name in pairs:
        engine.db.insert(
            "label_mapping",
            {
                "label": label, "label_norm": normalise_title(label), "role": role,
                "construct_id": constructs[construct_name]["id"],
                "measured_as": f"as {construct_name}", "status": "accepted",
                "confidence": 1.0, "created_at": utc_now(),
            },
        )
    if orientations:
        for name, row in constructs.items():
            if not row["is_residual"]:
                engine.set_orientation(row["id"], f"higher {name}", actor="human:test")
    return constructs


@pytest.fixture
def mapped(corpus):
    """The corpus with an approved codebook, mappings and orientations."""
    engine = engine_for(corpus)
    approved_codebook(engine)
    map_by_hand(
        engine,
        [
            ("community trust", "predictor", "trust"),
            ("perceived fairness", "predictor", "fairness"),
            ("resident participation", "outcome", "participation"),
        ],
    )
    return corpus, engine


# -- 1. the codebook --------------------------------------------------------


class TestCodebook:
    def test_it_proposes_constructs_from_the_extracted_labels(self, corpus):
        engine = engine_for(corpus)
        outcome = engine.propose_codebook(actor="human:test")
        assert outcome.counts["n_proposed"] > 0
        assert all(c["status"] == "proposed" for c in engine.constructs(status="proposed"))

    def test_a_proposal_is_not_an_approval(self, corpus):
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        assert engine.codebook_approved() is False

    def test_mapping_refuses_until_the_codebook_is_approved(self, corpus):
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        with pytest.raises(SynthesisError, match="has not been approved"):
            engine.map_labels(actor="human:test")

    def test_approval_records_who_and_what(self, corpus):
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        engine.approve_codebook(actor="human:ana")
        row = corpus.db.query_one("SELECT * FROM codebook_approval")
        assert row["approved_by"] == "human:ana"
        assert len(row["construct_hash"]) == 64

    def test_an_edit_before_approval_is_logged_as_an_override(self, corpus):
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        construct = engine.constructs(status="proposed")[0]
        engine.edit_construct(
            construct["id"], definition="A definition the reviewer wrote.",
            actor="human:ana",
        )
        overrides = corpus.db.query(
            "SELECT * FROM human_override WHERE entity = 'construct'"
        )
        assert overrides and overrides[0]["field"] == "definition"

    def test_editing_after_approval_is_detectable(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine)
        assert engine.codebook_changed_since_approval() is False
        construct = engine.constructs(status="approved")[0]
        engine.edit_construct(construct["id"], definition="changed")
        assert engine.codebook_changed_since_approval() is True

    def test_the_residual_code_always_exists_and_is_approved(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine)
        residual = corpus.db.query_one("SELECT * FROM construct WHERE is_residual = 1")
        assert residual["name"] == "other" and residual["status"] == "approved"

    def test_a_codebook_of_only_the_residual_is_refused(self, corpus):
        engine = engine_for(corpus)
        engine._ensure_residual()
        with pytest.raises(SynthesisError):
            engine.approve_codebook(actor="human:test")

    def test_two_constructs_cannot_share_a_name(self, corpus):
        engine = engine_for(corpus)
        engine.add_construct("trust", definition="d", includes=["x"], excludes=["y"])
        with pytest.raises(SynthesisError, match="already a construct"):
            engine.add_construct("Trust", definition="d", includes=["x"], excludes=["y"])


# -- 2. mapping -------------------------------------------------------------


class TestMapping:
    def test_one_label_written_two_ways_is_asked_about_once(self, corpus):
        """Authors capitalise as they please; it is one measured thing.

        The mapping table allows one row per normalised label and role, so
        treating "Participation" and "participation" as two labels both
        asks the model the same question twice and fails on insert.
        """
        record = add_report(corpus, title="A study that shouts its labels")
        add_relationship(
            corpus, record, predictor="Community Trust", outcome="Resident Participation",
            position=9,
        )
        engine = engine_for(corpus)
        predictors = [
            row["label"] for row in engine.labels() if row["role"] == "predictor"
        ]
        folded = {normalise_title(label) for label in predictors}
        assert len(predictors) == len(folded)

    def test_the_uses_of_both_spellings_are_counted_together(self, corpus):
        record = add_report(corpus, title="A study that shouts its labels")
        add_relationship(
            corpus, record, predictor="Community Trust", outcome="Resident Participation",
            position=9,
        )
        engine = engine_for(corpus)
        entry = next(
            row for row in engine.labels()
            if normalise_title(row["label"]) == "community trust"
            and row["role"] == "predictor"
        )
        assert entry["n_uses"] == 8

    def test_mapping_survives_a_label_written_two_ways(self, corpus):
        record = add_report(corpus, title="A study that shouts its labels")
        add_relationship(
            corpus, record, predictor="Community Trust", outcome="Resident Participation",
            position=9,
        )
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        engine.approve_codebook(actor="human:test")
        outcome = engine.map_labels(actor="human:test")
        assert outcome.counts["n_mapped"] == outcome.counts["n_labels"]

    def test_every_label_is_mapped(self, corpus):
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        engine.approve_codebook(actor="human:test")
        outcome = engine.map_labels(actor="human:test")
        assert outcome.counts["n_mapped"] == outcome.counts["n_labels"]
        assert outcome.counts["n_labels"] == len(engine.labels())

    def test_a_construct_outside_the_codebook_is_refused_not_created(self, corpus):
        """The approval gate would be pointless if mapping could route round it."""
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust",))
        before = len(engine.constructs(status=None))
        provider = ScriptedProvider(
            default=json.dumps(
                {
                    "mappings": [
                        {
                            "label": "community trust",
                            "construct": "a construct nobody approved",
                            "measured_as": "trust in officials",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        )
        engine = engine_for(corpus, provider)
        engine.map_labels(actor="human:test")
        assert len(engine.constructs(status=None)) == before
        mapping = corpus.db.query_one(
            "SELECT m.*, c.is_residual FROM label_mapping m "
            "JOIN construct c ON c.id = m.construct_id WHERE m.label = 'community trust'"
        )
        assert mapping["is_residual"] == 1
        assert mapping["contestable"] == 1
        assert "not in the approved codebook" in mapping["contest_reason"]

    def test_low_confidence_makes_a_mapping_contestable(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust",))
        provider = ScriptedProvider(
            default=json.dumps(
                {
                    "mappings": [
                        {
                            "label": "community trust", "construct": "trust",
                            "measured_as": "trust in officials", "confidence": 0.3,
                        }
                    ]
                }
            )
        )
        engine = engine_for(corpus, provider)
        engine.map_labels(actor="human:test")
        row = corpus.db.query_one(
            "SELECT * FROM label_mapping WHERE label = 'community trust'"
        )
        assert row["contestable"] == 1 and "confidence" in row["contest_reason"]

    def test_contestable_mappings_reach_the_review_queue(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust",))
        engine.map_labels(actor="human:test")
        queued = corpus.db.query(
            "SELECT * FROM review_queue WHERE stage = 'synthesise' "
            "AND resolved_at IS NULL"
        )
        assert queued

    def test_confirming_a_mapping_clears_it_from_the_queue(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust",))
        engine.map_labels(actor="human:test")
        pending = engine.contestable_mappings()
        assert pending
        engine.confirm_mapping(pending[0]["id"], actor="human:test")
        assert corpus.db.scalar(
            "SELECT COUNT(*) FROM review_queue WHERE stage = 'synthesise' "
            "AND resolved_at IS NULL AND detail = ?",
            (f"{pending[0]['role']}: {pending[0]['label']}",),
        ) == 0

    def test_moving_a_label_is_logged_as_an_override(self, mapped):
        corpus, engine = mapped
        constructs = {c["name"]: c["id"] for c in engine.constructs(status="approved")}
        mapping = corpus.db.query_one(
            "SELECT * FROM label_mapping WHERE label = 'community trust'"
        )
        engine.confirm_mapping(
            mapping["id"], construct_id=constructs["fairness"],
            reason="it measures fairness, not trust", actor="human:ana",
        )
        override = corpus.db.query_one(
            "SELECT * FROM human_override WHERE entity = 'label_mapping'"
        )
        assert override is not None
        assert override["human_value"] == str(constructs["fairness"])

    def test_a_label_cannot_be_moved_to_an_unapproved_construct(self, mapped):
        corpus, engine = mapped
        rogue = engine.add_construct(
            "rogue", definition="d", includes=["x"], excludes=["y"]
        )
        mapping = corpus.db.query_one("SELECT * FROM label_mapping LIMIT 1")
        with pytest.raises(SynthesisError, match="not in the approved codebook"):
            engine.confirm_mapping(mapping["id"], construct_id=rogue)

    def test_mapping_twice_does_not_duplicate(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine)
        engine.map_labels(actor="human:test")
        first = corpus.db.scalar("SELECT COUNT(*) FROM label_mapping")
        engine.map_labels(actor="human:test")
        assert corpus.db.scalar("SELECT COUNT(*) FROM label_mapping") == first


# -- 3 and 4. orientation and harmonisation ---------------------------------


class TestHarmonisation:
    def test_harmonising_refuses_without_orientations(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine)
        map_by_hand(
            engine,
            [("community trust", "predictor", "trust"),
             ("resident participation", "outcome", "participation")],
            orientations=False,
        )
        with pytest.raises(SynthesisError, match="reference orientation"):
            engine.harmonise(actor="human:test")

    def test_the_orientation_is_the_humans_and_is_logged(self, corpus):
        engine = engine_for(corpus)
        constructs = approved_codebook(engine)
        engine.set_orientation(constructs["trust"], "higher trust", actor="human:ana")
        row = corpus.db.query_one(
            "SELECT * FROM construct WHERE id = ?", (constructs["trust"],)
        )
        assert row["orientation"] == "higher trust"
        assert row["orientation_set_by"] == "human:ana"
        assert corpus.db.query_one(
            "SELECT * FROM audit_log WHERE action = 'orientation_set'"
        )

    def test_an_empty_orientation_is_refused(self, corpus):
        engine = engine_for(corpus)
        constructs = approved_codebook(engine)
        with pytest.raises(SynthesisError, match="which direction counts"):
            engine.set_orientation(constructs["trust"], "   ", actor="human:test")

    def test_a_reversed_label_has_its_sign_flipped_and_recorded(self, corpus):
        engine = engine_for(corpus)
        constructs = approved_codebook(engine, names=("trust", "participation"))
        record = add_report(corpus, title="Distrust study")
        add_relationship(
            corpus, record,
            predictor="community distrust", outcome="resident participation",
            direction="positive",
        )
        map_by_hand(
            engine,
            [("community trust", "predictor", "trust"),
             ("community distrust", "predictor", "trust"),
             ("resident participation", "outcome", "participation")],
        )
        engine.harmonise(actor="human:test")
        flips = engine.sign_flips()
        assert len(flips) == 1
        assert flips[0]["original_direction"] == "positive"
        assert flips[0]["direction"] == "negative"
        assert "distrust" in flips[0]["flip_reason"]

    def test_two_reversals_cancel(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust", "participation"))
        record = add_report(corpus, title="Both reversed")
        add_relationship(
            corpus, record,
            predictor="community distrust", outcome="resident non-participation",
            direction="positive",
        )
        map_by_hand(
            engine,
            [("community distrust", "predictor", "trust"),
             ("resident non-participation", "outcome", "participation")],
        )
        engine.harmonise(actor="human:test")
        row = corpus.db.query_one(
            "SELECT * FROM harmonised_relationship WHERE record_id = ?", (record,)
        )
        assert row["flipped"] == 0
        assert row["direction"] == "positive"
        assert "cancel" in row["flip_reason"]

    def test_a_label_oriented_on_the_reverse_is_not_flipped(self, corpus):
        """'distrust' against an orientation of 'higher distrust' is not a flip."""
        engine = engine_for(corpus)
        constructs = approved_codebook(engine, names=("distrust", "participation"))
        record = add_report(corpus, title="Distrust oriented")
        add_relationship(
            corpus, record,
            predictor="community distrust", outcome="resident participation",
        )
        map_by_hand(
            engine,
            [("community distrust", "predictor", "distrust"),
             ("resident participation", "outcome", "participation")],
        )
        engine.harmonise(actor="human:test")
        row = corpus.db.query_one(
            "SELECT * FROM harmonised_relationship WHERE record_id = ?", (record,)
        )
        assert row["flipped"] == 0

    def test_a_review_paper_does_not_vote(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        uncounted = engine.uncounted_relationships()
        reasons = " ".join(r["excluded_reason"] for r in uncounted)
        assert "review" in reasons

    def test_an_unmapped_end_is_excluded_with_its_reason(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust", "participation"))
        map_by_hand(
            engine,
            [("community trust", "predictor", "trust"),
             ("resident participation", "outcome", "participation")],
        )
        engine.harmonise(actor="human:test")
        uncounted = engine.uncounted_relationships()
        assert any("not mapped" in r["excluded_reason"] for r in uncounted)

    def test_nothing_is_silently_dropped(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        total = corpus.db.scalar(
            "SELECT COUNT(*) FROM relationship WHERE coder_role = 'ai'"
        )
        assert corpus.db.scalar("SELECT COUNT(*) FROM harmonised_relationship") == total

    def test_rerunning_harmonisation_is_stable(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        first = corpus.db.scalar("SELECT COUNT(*) FROM harmonised_relationship")
        engine.harmonise(actor="human:test")
        assert corpus.db.scalar("SELECT COUNT(*) FROM harmonised_relationship") == first


# -- 5. dataset linkage -----------------------------------------------------


class TestLinkageScoring:
    def test_surnames_are_pulled_from_either_format(self):
        assert author_surnames("Rivera, A; Osei, B") == {"rivera", "osei"}
        assert author_surnames('["Ana Rivera", "B Osei"]') == {"rivera", "osei"}

    def test_the_same_team_site_and_sample_scores_high(self):
        a = {"authors": "Rivera, A; Osei, B", "country": "Fiji",
             "setting": "coastal villages of Viti Levu", "sample_size": 312,
             "time_horizon": "2019 fieldwork"}
        score, signals = score_shared_dataset(a, dict(a))
        assert score > 0.9 and "rivera" in signals["authors"]

    def test_unrelated_studies_score_low(self):
        a = {"authors": "Rivera, A", "country": "Fiji", "setting": "coastal",
             "sample_size": 312}
        b = {"authors": "Tanaka, K", "country": "Japan", "setting": "alpine",
             "sample_size": 90}
        assert score_shared_dataset(a, b)[0] < 0.2

    def test_shared_authors_alone_do_not_carry_a_pair(self):
        """Teams run many studies; the same names are not the same fieldwork."""
        a = {"authors": "Rivera, A; Osei, B", "country": "Fiji",
             "setting": "coastal", "sample_size": 312}
        b = {"authors": "Rivera, A; Osei, B", "country": "Peru",
             "setting": "andean highlands", "sample_size": 88}
        assert score_shared_dataset(a, b)[0] < 0.55

    def test_a_pair_without_shared_authors_is_discounted(self):
        a = {"authors": "Rivera, A", "country": "Fiji", "setting": "coastal",
             "sample_size": 312}
        b = {"authors": "Tanaka, K", "country": "Fiji", "setting": "coastal",
             "sample_size": 312}
        score, signals = score_shared_dataset(a, b)
        assert signals["no_shared_authors"] is True
        assert score < 0.55


class TestDatasets:
    def test_every_report_lands_in_exactly_one_dataset(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        engine.build_datasets(actor="human:test")
        n_reports = corpus.db.scalar(
            "SELECT COUNT(*) FROM report_extraction WHERE coder_role = 'ai'"
        )
        assert corpus.db.scalar("SELECT COUNT(*) FROM dataset_member") == n_reports
        assert corpus.db.scalar(
            "SELECT COUNT(DISTINCT record_id) FROM dataset_member"
        ) == n_reports

    def test_without_confirmed_links_each_report_is_its_own_dataset(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        outcome = engine.build_datasets(actor="human:test")
        assert outcome.counts["n_datasets"] == outcome.counts["n_reports"]

    def test_a_confirmed_link_collapses_two_reports_into_one_dataset(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        a, b = [r["id"] for r in corpus.db.query(
            "SELECT id FROM record ORDER BY id LIMIT 2")]
        link_id = corpus.db.insert(
            "dataset_link",
            {"record_a": a, "record_b": b, "score": 0.9, "created_at": utc_now()},
        )
        engine.decide_link(link_id, confirmed=True, actor="human:test")
        outcome = engine.build_datasets(actor="human:test")
        assert outcome.counts["n_datasets"] == outcome.counts["n_reports"] - 1
        assert outcome.counts["n_shared"] == 1

    def test_a_rejected_link_changes_nothing(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        a, b = [r["id"] for r in corpus.db.query(
            "SELECT id FROM record ORDER BY id LIMIT 2")]
        link_id = corpus.db.insert(
            "dataset_link",
            {"record_a": a, "record_b": b, "score": 0.9, "created_at": utc_now()},
        )
        engine.decide_link(link_id, confirmed=False, actor="human:test")
        outcome = engine.build_datasets(actor="human:test")
        assert outcome.counts["n_shared"] == 0

    def test_links_are_transitive(self, mapped):
        """A shares with B, B with C: one body of fieldwork, not two."""
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        a, b, c = [r["id"] for r in corpus.db.query(
            "SELECT id FROM record ORDER BY id LIMIT 3")]
        for pair in ((a, b), (b, c)):
            link_id = corpus.db.insert(
                "dataset_link",
                {"record_a": pair[0], "record_b": pair[1], "score": 0.9,
                 "created_at": utc_now()},
            )
            engine.decide_link(link_id, confirmed=True, actor="human:test")
        outcome = engine.build_datasets(actor="human:test")
        assert outcome.counts["n_datasets"] == outcome.counts["n_reports"] - 2
        members = corpus.db.scalar(
            "SELECT COUNT(*) FROM dataset_member WHERE dataset_id = "
            "(SELECT dataset_id FROM dataset_member WHERE record_id = ?)", (a,)
        )
        assert members == 3

    def test_detection_proposes_and_never_merges(self, mapped):
        corpus, engine = mapped
        engine.find_dataset_links(actor="human:test")
        assert corpus.db.scalar(
            "SELECT COUNT(*) FROM dataset_link WHERE status = 'confirmed'"
        ) == 0

    def test_a_decision_is_logged(self, mapped):
        corpus, engine = mapped
        a, b = [r["id"] for r in corpus.db.query(
            "SELECT id FROM record ORDER BY id LIMIT 2")]
        link_id = corpus.db.insert(
            "dataset_link",
            {"record_a": a, "record_b": b, "score": 0.9, "created_at": utc_now()},
        )
        engine.decide_link(link_id, confirmed=True, reason="same survey",
                           actor="human:ana")
        assert corpus.db.query_one(
            "SELECT * FROM audit_log WHERE action = 'dataset_link_confirmed'"
        )


# -- 6. grading -------------------------------------------------------------


@pytest.fixture
def graded(mapped):
    corpus, engine = mapped
    engine.harmonise(actor="human:test")
    engine.build_datasets(actor="human:test")
    outcome = engine.grade(actor="human:test")
    return corpus, engine, outcome


class TestGrading:
    def test_grading_refuses_before_harmonisation(self, mapped):
        corpus, engine = mapped
        with pytest.raises(SynthesisError, match="harmonised"):
            engine.grade(actor="human:test")

    def test_grading_refuses_before_datasets_are_built(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        with pytest.raises(SynthesisError, match="datasets have not been built"):
            engine.grade(actor="human:test")

    def test_the_known_strong_link_is_strong(self, graded):
        corpus, engine, _ = graded
        row = corpus.db.query_one(
            "SELECT * FROM v_graded_inventory WHERE predictor = 'trust'"
        )
        assert row["n_datasets"] == 6      # the review paper does not vote
        assert row["n_agree"] == 6
        assert row["grade"] == "strong"
        assert row["n_countries"] == 3

    def test_the_known_contested_link_is_contested(self, graded):
        corpus, engine, _ = graded
        row = corpus.db.query_one(
            "SELECT * FROM v_graded_inventory WHERE predictor = 'fairness'"
        )
        assert (row["n_agree"], row["n_oppose"]) == (4, 3)
        assert row["grade"] == "contested"

    def test_every_link_carries_an_interval(self, graded):
        corpus, engine, _ = graded
        for row in engine.graded_inventory():
            assert row["ci_low"] is not None and row["ci_high"] is not None
            assert 0.0 <= row["ci_low"] <= row["ci_high"] <= 1.0

    def test_every_grade_explains_itself(self, graded):
        corpus, engine, _ = graded
        assert all(r["grade_reason"] for r in engine.graded_inventory())

    def test_one_vote_per_dataset(self, graded):
        corpus, engine, _ = graded
        duplicates = corpus.db.query(
            "SELECT predictor_construct, outcome_construct, dataset_id, COUNT(*) n "
            "FROM link_vote GROUP BY 1, 2, 3 HAVING n > 1"
        )
        assert duplicates == []

    def test_linked_reports_vote_once(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        trust_records = [
            r["id"] for r in corpus.db.query(
                "SELECT id FROM record WHERE title LIKE 'Trust study%' ORDER BY id"
            )
        ]
        link_id = corpus.db.insert(
            "dataset_link",
            {"record_a": trust_records[0], "record_b": trust_records[1],
             "score": 0.9, "created_at": utc_now()},
        )
        engine.decide_link(link_id, confirmed=True, actor="human:test")
        engine.build_datasets(actor="human:test")
        engine.grade(actor="human:test")
        row = corpus.db.query_one(
            "SELECT * FROM v_graded_inventory WHERE predictor = 'trust'"
        )
        assert row["n_datasets"] == 5 and row["n_reports"] == 6

    def test_a_dataset_whose_reports_disagree_is_flagged(self, mapped):
        corpus, engine = mapped
        engine.harmonise(actor="human:test")
        records = [
            r["id"] for r in corpus.db.query(
                "SELECT id FROM record WHERE title LIKE 'Fairness study%' "
                "ORDER BY id LIMIT 7"
            )
        ]
        # A positive report and a negative one, declared the same fieldwork.
        link_id = corpus.db.insert(
            "dataset_link",
            {"record_a": records[0], "record_b": records[6], "score": 0.9,
             "created_at": utc_now()},
        )
        engine.decide_link(link_id, confirmed=True, actor="human:test")
        engine.build_datasets(actor="human:test")
        engine.grade(actor="human:test")
        assert corpus.db.scalar(
            "SELECT COUNT(*) FROM link_vote WHERE within_dataset_conflict = 1"
        ) == 1

    def test_the_grade_can_be_overridden_with_a_reason(self, graded):
        corpus, engine, _ = graded
        link = corpus.db.query_one(
            "SELECT * FROM graded_link WHERE grade = 'strong'"
        )
        engine.override_grade(
            link["id"], "moderate",
            reason="two of the six share a sampling frame", actor="human:ana",
        )
        after = corpus.db.query_one("SELECT * FROM graded_link WHERE id = ?", (link["id"],))
        assert after["grade"] == "moderate"
        assert after["original_grade"] == "strong"
        assert after["grade_overridden"] == 1
        assert corpus.db.query_one(
            "SELECT * FROM human_override WHERE entity = 'graded_link'"
        )

    def test_an_override_without_a_reason_is_refused(self, graded):
        corpus, engine, _ = graded
        link = corpus.db.query_one("SELECT * FROM graded_link LIMIT 1")
        with pytest.raises(SynthesisError, match="needs a reason"):
            engine.override_grade(link["id"], "strong", reason="  ")

    def test_the_run_warns_about_the_unreachable_minimum(self, graded):
        corpus, engine, outcome = graded
        assert any("effective minimum" in m for m in outcome.messages)

    def test_regrading_replaces_rather_than_accumulates(self, graded):
        corpus, engine, _ = graded
        first = corpus.db.scalar("SELECT COUNT(*) FROM graded_link")
        engine.grade(actor="human:test")
        assert corpus.db.scalar("SELECT COUNT(*) FROM graded_link") == first

    def test_link_keys_are_readable(self, graded):
        corpus, engine, _ = graded
        assert "trust -> participation" in {
            r["link_key"] for r in engine.graded_inventory()
        }


# -- 7a. sensitivity --------------------------------------------------------


class TestSensitivity:
    def test_all_five_analyses_run(self, graded):
        corpus, engine, _ = graded
        outcome = engine.run_sensitivity(actor="human:test")
        assert outcome.counts["n_analyses"] == 5
        assert set(engine.SENSITIVITY) == {
            "alternative_strong_rule", "exclude_high_concern",
            "quantitative_only", "experiments_only", "observational_only",
        }

    def test_the_stricter_rule_demotes_the_strong_link(self, graded):
        """Six datasets and three countries pass; seven and three do not."""
        corpus, engine, _ = graded
        engine.run_sensitivity(actor="human:test")
        row = corpus.db.query_one(
            "SELECT * FROM sensitivity_result WHERE analysis = "
            "'alternative_strong_rule' AND link_key LIKE 'trust%'"
        )
        assert row["base_grade"] == "strong" and row["new_grade"] != "strong"
        assert row["changed_grade"] == 1

    def test_experiments_only_empties_a_cross_sectional_corpus(self, graded):
        corpus, engine, _ = graded
        engine.run_sensitivity(actor="human:test")
        rows = corpus.db.query(
            "SELECT * FROM sensitivity_result WHERE analysis = 'experiments_only'"
        )
        assert rows and all(r["new_grade"] == "none" for r in rows)

    def test_an_analysis_that_changes_nothing_reports_nothing(self, graded):
        corpus, engine, _ = graded
        engine.run_sensitivity(actor="human:test")
        # Every study in the corpus is low-concern, so removing high-concern
        # studies cannot change a grade.
        assert corpus.db.scalar(
            "SELECT COUNT(*) FROM sensitivity_result "
            "WHERE analysis = 'exclude_high_concern'"
        ) == 0

    def test_results_are_replaced_on_a_rerun(self, graded):
        corpus, engine, _ = graded
        engine.run_sensitivity(actor="human:test")
        first = corpus.db.scalar("SELECT COUNT(*) FROM sensitivity_result")
        engine.run_sensitivity(actor="human:test")
        assert corpus.db.scalar("SELECT COUNT(*) FROM sensitivity_result") == first

    def test_it_refuses_before_grading(self, mapped):
        corpus, engine = mapped
        with pytest.raises(SynthesisError, match="graded"):
            engine.run_sensitivity(actor="human:test")


# -- 7b. contradictions -----------------------------------------------------


class TestContradictions:
    def test_each_contested_link_gets_an_explanation(self, graded):
        corpus, engine, _ = graded
        outcome = engine.explain_contradictions(actor="human:test")
        assert outcome.counts["n_contested"] == 1
        assert outcome.counts["n_explained"] == 1
        row = corpus.db.query_one("SELECT * FROM contradiction")
        assert row["explanation"] and row["explanation_type"] in (
            "context", "measurement", "method", "sample", "unclear"
        )

    def test_an_uncontested_corpus_produces_no_table(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust", "participation"))
        map_by_hand(
            engine,
            [("community trust", "predictor", "trust"),
             ("resident participation", "outcome", "participation")],
        )
        engine.harmonise(actor="human:test")
        engine.build_datasets(actor="human:test")
        engine.grade(actor="human:test")
        outcome = engine.explain_contradictions(actor="human:test")
        assert outcome.counts == {"n_contested": 0, "n_explained": 0}

    def test_the_counts_behind_the_disagreement_are_stored(self, graded):
        corpus, engine, _ = graded
        engine.explain_contradictions(actor="human:test")
        row = corpus.db.query_one("SELECT * FROM contradiction")
        supporting = json.loads(row["supporting"])
        assert supporting["positive"] == 4 and supporting["negative"] == 3

    def test_a_suggestion_is_proposed_not_applied(self, graded):
        corpus, engine, _ = graded
        engine.explain_contradictions(actor="human:test")
        row = corpus.db.query_one("SELECT * FROM contradiction")
        assert row["status"] == "proposed"

    def test_a_decision_is_recorded(self, graded):
        corpus, engine, _ = graded
        engine.explain_contradictions(actor="human:test")
        row = corpus.db.query_one("SELECT * FROM contradiction")
        engine.decide_contradiction(
            row["id"], accepted=True, reason="country does look like the moderator",
            actor="human:ana",
        )
        after = corpus.db.query_one("SELECT * FROM contradiction WHERE id = ?", (row["id"],))
        assert after["status"] == "accepted" and after["decided_by"] == "human:ana"


# -- traceability and cost --------------------------------------------------


class TestIntegrity:
    def test_every_graded_link_traces_to_its_datasets(self, graded):
        corpus, engine, _ = graded
        for link in corpus.db.query("SELECT * FROM graded_link"):
            votes = corpus.db.query(
                "SELECT * FROM link_vote WHERE predictor_construct = ? "
                "AND outcome_construct = ?",
                (link["predictor_construct"], link["outcome_construct"]),
            )
            assert len(votes) == link["n_datasets"]

    def test_every_vote_traces_to_a_real_relationship(self, graded):
        corpus, engine, _ = graded
        orphans = corpus.db.query(
            "SELECT * FROM harmonised_relationship h "
            "LEFT JOIN relationship r ON r.id = h.relationship_id "
            "WHERE r.id IS NULL"
        )
        assert orphans == []

    def test_every_ai_step_is_logged_with_its_model(self, corpus):
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        call = corpus.db.query_one(
            "SELECT * FROM ai_call WHERE task = 'synthesise:codebook'"
        )
        assert call["model"] and call["prompt_hash"] and call["created_at"]

    def test_each_step_writes_a_run_row(self, graded):
        corpus, engine, _ = graded
        kinds = {r["kind"] for r in corpus.db.query("SELECT kind FROM synthesis_run")}
        assert {"harmonise", "linkage", "grade"} <= kinds

    def test_a_cost_estimate_is_available_before_a_bulk_run(self, corpus):
        engine = engine_for(corpus)
        estimate = engine.estimate("codebook")
        assert estimate.n_calls == 1
        approved_codebook(engine)
        assert engine.estimate("mapping").n_calls >= 1

    def test_the_synthesis_never_reads_a_report(self, graded):
        """No step here opens a PDF: everything rests on stored extractions."""
        corpus, engine, _ = graded
        import inspect

        from slr_studio.synthesis import engine as module

        source = inspect.getsource(module)
        assert "report_page" not in source
        assert "PdfPage" not in source


class TestStepOutcome:
    """What a step reports back, for a terminal rather than a screen."""

    def test_it_names_the_step_and_what_it_did(self, corpus):
        engine = engine_for(corpus)
        outcome = engine.propose_codebook(actor="human:test")
        text = outcome.describe()
        assert "codebook" in text.casefold()
        assert str(outcome.counts["n_proposed"]) in text

    def test_it_reports_the_cost_so_a_bulk_run_is_accountable(self, corpus):
        engine = engine_for(corpus)
        outcome = engine.propose_codebook(actor="human:test")
        assert "call" in outcome.describe().casefold()

    def test_messages_are_carried_through_not_swallowed(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine, names=("trust",))
        engine.map_labels(actor="human:test")
        outcome = engine.map_labels(actor="human:test")
        assert outcome.messages
        for message in outcome.messages:
            assert message in outcome.describe()

    def test_each_step_names_its_own_units(self, graded):
        """`n_proposed` is constructs in one step and shared samples in
        another, so a phrase chosen by count name alone misreports one of
        them. Every step is run here and its sentence read back.
        """
        _, engine, _ = graded
        said = {
            "codebook": ("construct", engine.propose_codebook),
            "linkage": ("shared sample", engine.find_dataset_links),
            "grade": ("graded link", engine.grade),
        }
        for step, (unit, run) in said.items():
            text = run(actor="human:test").describe().casefold()
            assert unit in text, f"{step} did not mention {unit}: {text!r}"

    def test_a_step_that_did_something_does_not_claim_it_did_nothing(self, graded):
        _, engine, _ = graded
        outcome = engine.grade(actor="human:test")
        assert outcome.counts["n_links"] > 0
        assert "nothing to do" not in outcome.describe().casefold()

    def test_a_paused_step_says_so_rather_than_looking_finished(self, corpus):
        engine = engine_for(corpus)
        approved_codebook(engine)
        outcome = engine.map_labels(
            actor="human:test", should_stop=lambda: True
        )
        assert outcome.status == "paused"
        assert "paused" in outcome.describe().casefold()


class TestProgressView:
    def test_it_reports_what_has_been_done(self, graded):
        corpus, engine, _ = graded
        counts = engine.counts()
        assert counts["n_constructs"] >= 3
        assert counts["n_graded_links"] == 2
        assert counts["n_strong"] == 1
        assert counts["n_contested"] == 1
        assert counts["n_datasets"] > 0

    def test_the_label_count_matches_the_labels_shown(self, corpus):
        """The screen says "N labels"; N must be the number it then lists."""
        record = add_report(corpus, title="A study that shouts its labels")
        add_relationship(
            corpus, record, predictor="Community Trust",
            outcome="Resident Participation", position=9,
        )
        engine = engine_for(corpus)
        assert engine.counts()["n_labels"] == len(engine.labels())


# -- end to end through the real pipeline -----------------------------------


class TestEndToEnd:
    def test_the_whole_stage_runs_on_the_bundled_corpus(self, corpus):
        """Every step in order, with no hand-written mappings."""
        engine = engine_for(corpus)
        engine.propose_codebook(actor="human:test")
        engine.approve_codebook(actor="human:test")
        engine.map_labels(actor="human:test")
        for construct in engine.constructs_without_orientation():
            engine.set_orientation(
                construct["id"], f"higher {construct['name']}", actor="human:test"
            )
        engine.harmonise(actor="human:test")
        engine.find_dataset_links(actor="human:test")
        engine.build_datasets(actor="human:test")
        outcome = engine.grade(actor="human:test")
        engine.run_sensitivity(actor="human:test")
        engine.explain_contradictions(actor="human:test")

        inventory = engine.graded_inventory()
        assert inventory
        assert all(r["grade"] in
                   ("asserted", "contested", "strong", "moderate",
                    "emerging", "tentative") for r in inventory)
        assert outcome.status == "finished"
