"""Writing a gap down, and ranking it.

Two rules from the spec, both absolute:

- every claim of absence is qualified "within the included corpus, searched to
  [date]";
- no gap statement may be produced without the counts and records that
  support it.

The second is the one that needs teeth. A statement is the thing a reviewer
pastes into a manuscript, so a code path that can produce one without its
evidence is a code path that can put an unsupported claim in a thesis.
"""

from __future__ import annotations

import pytest

from slr_studio.gaps.detect import GapCandidate, GapStatement
from slr_studio.gaps.priority import Weights, rank, score
from slr_studio.gaps.statements import UnsupportedGap, write_statement


def candidate(**kwargs) -> GapCandidate:
    base = dict(
        gap_type="evidence",
        statement=GapStatement(
            body="No dataset measures efficacy together with climate concern "
                 "(0 of 89 datasets)."
        ),
        counts={"n_datasets": 0, "n_datasets_total": 89},
        record_ids=(),
        link_ids=(),
    )
    base.update(kwargs)
    return GapCandidate(**base)


class TestTheCorpusQualifier:
    def test_the_statement_says_which_corpus_and_which_date(self):
        text = write_statement(candidate(), searched_to="2026-03-01")
        assert "within the included corpus" in text.casefold()
        assert "2026-03-01" in text

    def test_the_claim_itself_is_kept(self):
        text = write_statement(candidate(), searched_to="2026-03-01")
        assert "0 of 89 datasets" in text

    def test_a_missing_search_date_is_refused_rather_than_left_vague(self):
        """Unqualified, the claim is about the literature, not the corpus."""
        with pytest.raises(UnsupportedGap) as caught:
            write_statement(candidate(), searched_to="")
        assert "search" in str(caught.value).casefold()

    def test_the_qualifier_is_not_repeated_if_already_present(self):
        body = GapStatement(
            body="Within the included corpus, searched to 2026-03-01, nothing "
                 "measures X (0 of 3 datasets)."
        )
        text = write_statement(
            candidate(statement=body), searched_to="2026-03-01"
        )
        assert text.casefold().count("within the included corpus") == 1


class TestNoStatementWithoutEvidence:
    def test_a_gap_with_no_counts_is_refused(self):
        with pytest.raises(UnsupportedGap):
            write_statement(candidate(counts={}), searched_to="2026-03-01")

    def test_the_refusal_names_the_gap_so_it_can_be_found(self):
        with pytest.raises(UnsupportedGap) as caught:
            write_statement(
                candidate(counts={}, gap_type="methodological"),
                searched_to="2026-03-01",
            )
        assert "methodological" in str(caught.value)

    def test_counts_without_any_number_are_not_counts(self):
        """A dict of labels is not evidence; a count has to count something."""
        with pytest.raises(UnsupportedGap):
            write_statement(
                candidate(counts={"kind": "pair_unevidenced",
                                  "predictor": "a", "outcome": "b"}),
                searched_to="2026-03-01",
            )

    def test_a_gap_about_present_studies_must_name_them(self):
        """A contradiction is about studies that exist, so they must be cited."""
        with pytest.raises(UnsupportedGap):
            write_statement(
                candidate(
                    gap_type="contradiction",
                    counts={"n_datasets": 7},
                    record_ids=(),
                ),
                searched_to="2026-03-01",
            )

    def test_an_absence_need_not_cite_records_because_there_are_none(self):
        text = write_statement(
            candidate(counts={"n_datasets": 0, "n_datasets_total": 89}),
            searched_to="2026-03-01",
        )
        assert text


class TestScoring:
    def test_a_gap_many_propositions_depend_on_scores_higher_on_centrality(self):
        central = score(
            candidate(), n_propositions=4, n_propositions_total=5,
            emptiness=0.5, in_author_gaps=False, feasible=True,
        )
        peripheral = score(
            candidate(), n_propositions=0, n_propositions_total=5,
            emptiness=0.5, in_author_gaps=False, feasible=True,
        )
        assert central.centrality > peripheral.centrality

    def test_an_emptier_cell_scores_higher_on_size(self):
        empty = score(candidate(), emptiness=1.0)
        thin = score(candidate(), emptiness=0.25)
        assert empty.size > thin.size

    def test_a_gap_the_authors_themselves_name_scores_on_relevance(self):
        named = score(candidate(), in_author_gaps=True)
        unnamed = score(candidate(), in_author_gaps=False)
        assert named.relevance > unnamed.relevance

    def test_a_gap_a_standard_design_could_close_scores_on_feasibility(self):
        assert score(candidate(), feasible=True).feasibility > (
            score(candidate(), feasible=False).feasibility
        )

    def test_every_score_is_between_zero_and_one(self):
        for emptiness in (0.0, 0.5, 1.0):
            result = score(
                candidate(), n_propositions=2, n_propositions_total=4,
                emptiness=emptiness, in_author_gaps=True, feasible=True,
            )
            for value in (
                result.centrality, result.size, result.relevance,
                result.feasibility, result.priority,
            ):
                assert 0.0 <= value <= 1.0


class TestWeights:
    def test_the_weights_can_be_changed(self):
        default = score(candidate(), emptiness=1.0, feasible=False)
        reweighted = score(
            candidate(), emptiness=1.0, feasible=False,
            weights=Weights(centrality=0.0, size=1.0, relevance=0.0,
                            feasibility=0.0),
        )
        assert reweighted.priority >= default.priority

    def test_weights_that_are_all_zero_are_refused(self):
        with pytest.raises(ValueError):
            Weights(centrality=0.0, size=0.0, relevance=0.0, feasibility=0.0)

    def test_a_negative_weight_is_refused(self):
        with pytest.raises(ValueError):
            Weights(centrality=-1.0)

    def test_the_weights_need_not_sum_to_one(self):
        """They are normalised, so a reviewer can type 3, 1, 1, 1."""
        result = score(
            candidate(), emptiness=1.0,
            weights=Weights(centrality=3.0, size=1.0, relevance=1.0,
                            feasibility=1.0),
        )
        assert 0.0 <= result.priority <= 1.0


class TestRanking:
    def test_gaps_come_back_in_priority_order(self):
        scored = [
            (candidate(), score(candidate(), emptiness=0.1)),
            (candidate(), score(candidate(), emptiness=1.0)),
            (candidate(), score(candidate(), emptiness=0.5)),
        ]
        ordered = rank(scored)
        priorities = [s.priority for _, s in ordered]
        assert priorities == sorted(priorities, reverse=True)

    def test_a_reviewer_override_changes_the_ranking(self):
        base = score(candidate(), emptiness=0.1)
        overridden = base.with_overrides({"size": 1.0})
        assert overridden.size == 1.0
        assert overridden.priority > base.priority

    def test_an_override_records_which_scores_were_changed(self):
        base = score(candidate(), emptiness=0.1)
        overridden = base.with_overrides({"size": 1.0})
        assert overridden.overrides == {"size": 1.0}

    def test_an_override_outside_the_range_is_refused(self):
        base = score(candidate())
        with pytest.raises(ValueError):
            base.with_overrides({"size": 1.5})

    def test_an_override_of_an_unknown_criterion_is_refused(self):
        base = score(candidate())
        with pytest.raises(ValueError):
            base.with_overrides({"vibes": 1.0})
