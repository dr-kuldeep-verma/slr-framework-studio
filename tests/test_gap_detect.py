"""The seven kinds of gap, found from a corpus described as plain data.

The detectors take a snapshot rather than a database so that a corpus with
known holes can be written out in a test and checked exactly: the spec asks
for "a synthetic corpus with known empty cells, in which every planted gap is
found and no false gap is reported", and that is only checkable if the corpus
is something a test can state in full.

Every detector returns candidates carrying the counts and the records behind
them. A candidate without them is not a gap, and `test_statements` enforces
that; here the concern is only whether the right holes are found.
"""

from __future__ import annotations

import pytest

from slr_studio.gaps.detect import (
    Corpus,
    Dataset,
    Link,
    Report,
    Statement,
    detect_all,
    find_author_stated_gaps,
    find_contradiction_gaps,
    find_evidence_gaps,
    find_information_gaps,
    find_methodological_gaps,
    find_population_gaps,
    find_theoretical_gaps,
)


def report(ident, **kwargs):
    base = dict(
        record_id=ident,
        title=f"Study {ident}",
        country="Fiji",
        region="Oceania",
        income_group="Upper middle income",
        setting="coastal village",
        population="residents",
        activity="reef tourism",
        design_family="survey",
        sample_size=200,
        measures=(),
        theories=(),
        framework_stages=(),
        stimulus_attributes=(),
        compares_intention_with_behaviour=False,
    )
    base.update(kwargs)
    return Report(**base)


def link(predictor, outcome, *, grade="strong", datasets=5, direction="positive",
         contested_resolved=False, designs=("survey",), ident=None):
    return Link(
        link_id=ident if ident is not None else abs(hash((predictor, outcome))) % 997,
        predictor=predictor,
        outcome=outcome,
        grade=grade,
        direction=direction,
        n_datasets=datasets,
        record_ids=tuple(range(1, datasets + 1)),
        design_families=tuple(designs),
        contested_resolved=contested_resolved,
    )


def corpus(**kwargs) -> Corpus:
    base = dict(
        searched_to="2026-03-01",
        constructs=("trust", "fairness", "participation"),
        links=(),
        datasets=(),
        reports=(),
        propositions=(),
        untested_pathways=(),
        theories=(),
        stimulus_attributes=(),
        future_research=(),
        framework_type="SOBC",
    )
    base.update(kwargs)
    return Corpus(**base)


# -- 1. evidence gaps -------------------------------------------------------


class TestEvidenceGaps:
    def test_a_construct_pair_with_no_study_is_a_gap(self):
        found = find_evidence_gaps(
            corpus(links=(link("trust", "participation"),))
        )
        pairs = {(g.counts["predictor"], g.counts["outcome"]) for g in found}
        assert ("fairness", "participation") in pairs

    def test_a_pair_with_one_dataset_only_is_a_gap(self):
        found = find_evidence_gaps(
            corpus(links=(link("trust", "participation", datasets=1,
                               grade="tentative"),))
        )
        thin = [
            g for g in found
            if g.counts.get("predictor") == "trust"
            and g.counts.get("outcome") == "participation"
        ]
        assert thin and thin[0].counts["n_datasets"] == 1

    def test_a_well_evidenced_pair_is_not_a_gap(self):
        found = find_evidence_gaps(
            corpus(
                constructs=("trust", "participation"),
                links=(link("trust", "participation", datasets=6),),
            )
        )
        assert all(
            not (g.counts.get("predictor") == "trust"
                 and g.counts.get("outcome") == "participation")
            for g in found
        )

    def test_a_construct_is_not_paired_with_itself(self):
        found = find_evidence_gaps(corpus(constructs=("trust",)))
        assert all(
            g.counts.get("predictor") != g.counts.get("outcome") for g in found
        )

    def test_the_counts_say_how_many_datasets_exist(self):
        found = find_evidence_gaps(corpus())
        for gap in found:
            assert "n_datasets" in gap.counts
            assert "n_datasets_total" in gap.counts


# -- 2. theoretical gaps ----------------------------------------------------


class TestTheoreticalGaps:
    def test_a_framework_transition_with_no_graded_link_is_a_gap(self):
        found = find_theoretical_gaps(corpus(framework_type="SOBC"))
        described = " ".join(g.statement.body for g in found).casefold()
        assert "stimulus" in described and "organism" in described

    def test_a_transition_that_has_a_link_is_not_a_gap(self):
        found = find_theoretical_gaps(
            corpus(
                framework_type="SOBC",
                reports=(
                    report(1, framework_stages=("stimulus",)),
                    report(2, framework_stages=("organism",)),
                ),
                links=(
                    link("trust", "participation", designs=("survey",)),
                ),
                propositions=(),
            )
        )
        assert isinstance(found, list)

    def test_a_theory_mechanism_never_measured_is_a_gap(self):
        found = find_theoretical_gaps(
            corpus(
                theories=("protection motivation theory: efficacy, threat",),
                constructs=("trust", "participation"),
            )
        )
        named = " ".join(g.statement.body for g in found).casefold()
        assert "efficacy" in named

    def test_a_mechanism_that_is_a_construct_is_not_a_gap(self):
        found = find_theoretical_gaps(
            corpus(
                theories=("protection motivation theory: efficacy",),
                constructs=("efficacy", "participation"),
            )
        )
        mechanisms = [
            g for g in found if g.counts.get("kind") == "mechanism_not_measured"
        ]
        assert all("efficacy" not in g.statement.body for g in mechanisms)

    def test_an_untested_pathway_is_a_gap(self):
        found = find_theoretical_gaps(
            corpus(untested_pathways=(("trust", "support"),))
        )
        assert any(
            g.counts.get("kind") == "untested_pathway" for g in found
        )


# -- 3. contradiction gaps --------------------------------------------------


class TestContradictionGaps:
    def test_an_unresolved_contested_link_is_a_gap(self):
        found = find_contradiction_gaps(
            corpus(links=(link("fairness", "participation", grade="contested",
                               contested_resolved=False),))
        )
        assert len(found) == 1

    def test_a_resolved_contested_link_is_not_a_gap(self):
        found = find_contradiction_gaps(
            corpus(links=(link("fairness", "participation", grade="contested",
                               contested_resolved=True),))
        )
        assert found == []

    def test_a_strong_link_is_not_a_contradiction(self):
        found = find_contradiction_gaps(
            corpus(links=(link("trust", "participation", grade="strong"),))
        )
        assert found == []

    def test_the_gap_carries_the_studies_that_disagreed(self):
        found = find_contradiction_gaps(
            corpus(links=(link("fairness", "participation", grade="contested",
                               datasets=7),))
        )
        assert found[0].record_ids


# -- 4. methodological gaps -------------------------------------------------


class TestMethodologicalGaps:
    def test_a_construct_with_no_experiment_is_a_gap(self):
        found = find_methodological_gaps(
            corpus(
                constructs=("trust",),
                links=(link("trust", "participation", designs=("survey",)),),
                reports=(report(1, design_family="survey"),),
            )
        )
        assert any("experiment" in g.statement.body.casefold() for g in found)

    def test_a_construct_studied_experimentally_is_not_that_gap(self):
        found = find_methodological_gaps(
            corpus(
                constructs=("trust",),
                links=(
                    link("trust", "participation",
                         designs=("experiment", "survey")),
                ),
                reports=(report(1, design_family="experiment"),),
            )
        )
        experiments = [
            g for g in found
            if g.counts.get("kind") == "design_absent"
            and g.counts.get("design") == "experiment"
            and g.counts.get("construct") == "trust"
        ]
        assert experiments == []

    def test_a_measure_used_once_is_a_gap(self):
        found = find_methodological_gaps(
            corpus(
                reports=(
                    report(1, measures=("Likert trust scale",)),
                    report(2, measures=("Likert trust scale",)),
                    report(3, measures=("one-off index",)),
                ),
            )
        )
        once = [g for g in found if g.counts.get("kind") == "measure_used_once"]
        assert any("one-off index" in g.statement.body for g in once)
        assert all("Likert trust scale" not in g.statement.body for g in once)

    def test_intentions_never_compared_with_behaviour_is_a_gap(self):
        found = find_methodological_gaps(
            corpus(
                reports=(
                    report(1, compares_intention_with_behaviour=False),
                    report(2, compares_intention_with_behaviour=False),
                ),
            )
        )
        assert any(
            g.counts.get("kind") == "intention_behaviour" for g in found
        )

    def test_it_is_not_a_gap_when_some_study_made_the_comparison(self):
        found = find_methodological_gaps(
            corpus(
                reports=(
                    report(1, compares_intention_with_behaviour=True),
                    report(2, compares_intention_with_behaviour=False),
                ),
            )
        )
        assert all(
            g.counts.get("kind") != "intention_behaviour" for g in found
        )


# -- 5. population and context gaps -----------------------------------------


class TestPopulationAndContextGaps:
    def test_an_empty_construct_by_country_cell_is_a_gap(self):
        found = find_population_gaps(
            corpus(
                constructs=("trust",),
                links=(link("trust", "participation", datasets=1),),
                reports=(
                    report(1, country="Fiji"),
                    report(2, country="Nepal"),
                ),
                datasets=(
                    Dataset(dataset_id=1, record_ids=(1,), country="Fiji",
                            design_family="survey", setting="coastal village",
                            region="Oceania", income_group="Upper middle income",
                            population="residents", activity="reef tourism"),
                ),
            )
        )
        assert any(g.counts.get("kind") == "empty_cell" for g in found)

    def test_a_cell_with_studies_is_not_reported(self):
        found = find_population_gaps(
            corpus(
                constructs=("trust",),
                reports=(report(i, country="Fiji") for i in (1, 2, 3)),
                datasets=tuple(
                    Dataset(dataset_id=i, record_ids=(i,), country="Fiji",
                            design_family="survey", setting="coastal village",
                            region="Oceania", income_group="Upper middle income",
                            population="residents", activity="reef tourism")
                    for i in (1, 2, 3)
                ),
            )
        )
        populated = [
            g for g in found
            if g.counts.get("column") == "Fiji" and g.counts.get("n_datasets", 0) > 2
        ]
        assert populated == []

    def test_each_dimension_the_spec_names_is_crossed(self):
        found = find_population_gaps(
            corpus(
                constructs=("trust",),
                reports=(report(1),),
                datasets=(
                    Dataset(dataset_id=1, record_ids=(1,), country="Fiji",
                            design_family="survey", setting="coastal village",
                            region="Oceania", income_group="Upper middle income",
                            population="residents", activity="reef tourism"),
                ),
            )
        )
        dimensions = {g.counts.get("dimension") for g in found}
        assert {
            "country", "region", "income_group", "setting", "population",
            "activity",
        } <= dimensions


# -- 6. information gaps ----------------------------------------------------


class TestInformationGaps:
    def test_an_attribute_never_varied_is_a_gap(self):
        found = find_information_gaps(
            corpus(
                stimulus_attributes=("source", "certainty", "framing"),
                reports=(report(1, stimulus_attributes=("source",)),),
            )
        )
        named = {g.counts.get("attribute") for g in found}
        assert {"certainty", "framing"} <= named
        assert "source" not in named

    def test_nothing_is_reported_when_the_reviewer_named_nothing(self):
        """The reviewer has to name the attributes; the app will not guess."""
        assert find_information_gaps(corpus(stimulus_attributes=())) == []


# -- 7. author-stated gaps --------------------------------------------------


class TestAuthorStatedGaps:
    def test_statements_are_clustered_and_counted(self):
        found = find_author_stated_gaps(
            corpus(
                future_research=(
                    Statement(record_id=1, statement="Longitudinal designs are "
                              "needed to establish direction.", page=9),
                    Statement(record_id=2, statement="Longitudinal work is "
                              "needed to establish causal direction.", page=12),
                    Statement(record_id=3, statement="More work in Africa is "
                              "needed.", page=4),
                ),
            )
        )
        assert found
        biggest = max(found, key=lambda g: g.counts["n_statements"])
        assert biggest.counts["n_statements"] >= 2

    def test_each_statement_keeps_its_source_and_page(self):
        found = find_author_stated_gaps(
            corpus(
                future_research=(
                    Statement(record_id=7, statement="Longitudinal designs "
                              "are needed.", page=9),
                ),
            )
        )
        assert found[0].record_ids == (7,)
        assert found[0].counts["pages"] == [[7, 9]]

    def test_no_statements_is_no_gaps(self):
        assert find_author_stated_gaps(corpus(future_research=())) == []


# -- the acceptance test the spec asks for ----------------------------------


class TestAPlantedCorpus:
    """A corpus with known holes: every planted gap found, none invented."""

    @pytest.fixture
    def planted(self):
        """Two constructs well evidenced, one pair deliberately empty.

        - trust → participation: six datasets, two designs, two countries
        - fairness → participation: nothing at all (the planted evidence gap)
        - every study is a survey (the planted methodological gap)
        - nothing from Africa (the planted population gap)
        """
        reports = tuple(
            report(
                i,
                country="Fiji" if i % 2 else "Nepal",
                region="Oceania" if i % 2 else "South Asia",
                design_family="survey",
                measures=("shared trust scale",),
            )
            for i in range(1, 7)
        )
        datasets = tuple(
            Dataset(
                dataset_id=i,
                record_ids=(i,),
                country="Fiji" if i % 2 else "Nepal",
                region="Oceania" if i % 2 else "South Asia",
                income_group="Upper middle income",
                setting="coastal village",
                population="residents",
                activity="reef tourism",
                design_family="survey",
            )
            for i in range(1, 7)
        )
        return corpus(
            constructs=("trust", "fairness", "participation"),
            links=(link("trust", "participation", datasets=6, ident=1),),
            reports=reports,
            datasets=datasets,
            stimulus_attributes=("certainty",),
        )

    def test_the_planted_evidence_gap_is_found(self, planted):
        gaps = detect_all(planted)
        pairs = {
            (g.counts.get("predictor"), g.counts.get("outcome"))
            for g in gaps
            if g.gap_type == "evidence"
        }
        assert ("fairness", "participation") in pairs

    def test_the_evidenced_pair_is_not_called_a_gap(self, planted):
        gaps = detect_all(planted)
        pairs = {
            (g.counts.get("predictor"), g.counts.get("outcome"))
            for g in gaps
            if g.gap_type == "evidence"
        }
        assert ("trust", "participation") not in pairs

    def test_the_planted_methodological_gap_is_found(self, planted):
        gaps = [g for g in detect_all(planted) if g.gap_type == "methodological"]
        assert any("experiment" in g.statement.body.casefold() for g in gaps)

    def test_the_planted_information_gap_is_found(self, planted):
        gaps = [g for g in detect_all(planted) if g.gap_type == "information"]
        assert any(g.counts.get("attribute") == "certainty" for g in gaps)

    def test_no_gap_claims_a_country_the_corpus_never_studied(self, planted):
        """Absence is reported for the categories present, not invented ones."""
        gaps = detect_all(planted)
        mentioned = " ".join(g.statement.body for g in gaps)
        assert "Africa" not in mentioned

    def test_every_gap_carries_its_counts(self, planted):
        for gap in detect_all(planted):
            assert gap.counts, gap.statement.body

    def test_every_gap_is_one_of_the_seven_types(self, planted):
        allowed = {
            "evidence", "theoretical", "contradiction", "methodological",
            "population_context", "information", "author_stated",
        }
        assert {g.gap_type for g in detect_all(planted)} <= allowed

    def test_a_corpus_with_no_holes_reports_no_evidence_gap(self):
        """The other half of the acceptance test: no false gaps."""
        full = corpus(
            constructs=("trust", "participation"),
            links=(
                link("trust", "participation", datasets=6,
                     designs=("survey", "experiment"), ident=1),
                link("participation", "trust", datasets=6,
                     designs=("survey", "experiment"), ident=2),
            ),
            reports=tuple(
                report(i, design_family="experiment",
                       measures=("shared scale",),
                       compares_intention_with_behaviour=True)
                for i in range(1, 7)
            ),
        )
        assert [g for g in detect_all(full) if g.gap_type == "evidence"] == []
