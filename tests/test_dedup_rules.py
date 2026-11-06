"""The three matching rules, tested in isolation from the database."""

from __future__ import annotations

import pytest

from slr_studio.dedup.rules import (
    CandidateRecord,
    DedupSettings,
    blocking_keys,
    candidate_pairs,
    choose_primary,
    cluster_pairs,
    compare,
    completeness_score,
    title_similarity,
)


def record(record_id: int, title: str, **kwargs) -> CandidateRecord:
    from slr_studio.importers.normalise import normalise_title, title_key

    kwargs.setdefault("year", 2020)
    return CandidateRecord(
        id=record_id,
        title_norm=normalise_title(title),
        title_key=title_key(title),
        **kwargs,
    )


class TestTitleSimilarity:
    def test_identical_titles_score_one(self):
        assert title_similarity("a study of trust", "a study of trust") == 1.0

    def test_empty_title_scores_zero(self):
        assert title_similarity("", "a study") == 0.0

    def test_hyphenation_difference_stays_very_close(self):
        score = title_similarity(
            "trust as a precondition for co management",
            "trust as a pre condition for co management",
        )
        assert score > 0.97

    def test_reordered_words_still_score_highly(self):
        assert title_similarity("trust and participation", "participation and trust") > 0.9

    def test_unrelated_titles_score_low(self):
        assert title_similarity("resident participation in tourism", "marine biology") < 0.5

    def test_added_subtitle_is_penalised_not_ignored(self):
        # Containment would call this a perfect match. It is a different paper.
        score = title_similarity(
            "resident participation in tourism",
            "resident participation in tourism planning in rural spain a panel study",
        )
        assert score < 0.92

    def test_similarity_is_symmetric(self):
        a, b = "a study of trust", "a study of trusts"
        assert title_similarity(a, b) == title_similarity(b, a)


class TestRuleOneDoi:
    def test_same_doi_matches_automatically(self):
        a = record(1, "One title", doi="10.1234/abc.2020")
        b = record(2, "A completely different title", doi="10.1234/abc.2020")
        verdict = compare(a, b)
        assert verdict.rule == "doi_exact"
        assert verdict.status == "auto_match"
        assert verdict.score == 1.0

    def test_doi_match_ignores_a_year_disagreement(self):
        # Online-first and issue versions carry different years, one DOI.
        a = record(1, "One title", doi="10.1234/abc.2020", year=2019)
        b = record(2, "One title", doi="10.1234/abc.2020", year=2021)
        assert compare(a, b).status == "auto_match"

    def test_pair_is_ordered_by_record_id(self):
        a = record(7, "One title", doi="10.1234/abc.2020")
        b = record(3, "One title", doi="10.1234/abc.2020")
        verdict = compare(a, b)
        assert (verdict.record_a_id, verdict.record_b_id) == (3, 7)


class TestRuleTwoTitleAndYear:
    def test_identical_title_and_same_year_matches(self):
        a = record(1, "Benefit sharing in ecotourism", year=2019)
        b = record(2, "Benefit Sharing in Ecotourism.", year=2019)
        verdict = compare(a, b)
        assert verdict.rule == "title_year"
        assert verdict.status == "auto_match"

    def test_one_year_apart_is_within_tolerance(self):
        a = record(1, "Benefit sharing in ecotourism", year=2019)
        b = record(2, "Benefit sharing in ecotourism", year=2020)
        assert compare(a, b).status == "auto_match"

    def test_missing_year_cannot_contradict(self):
        a = record(1, "Benefit sharing in ecotourism", year=None)
        b = record(2, "Benefit sharing in ecotourism", year=2019)
        assert compare(a, b).status == "auto_match"

    def test_same_title_years_far_apart_is_not_a_duplicate(self):
        a = record(1, "Women in homestay enterprises", year=2018)
        b = record(2, "Women in homestay enterprises", year=2024)
        verdict = compare(a, b)
        assert verdict.status == "auto_distinct"
        assert verdict.evidence["reason"] == "years too far apart"

    def test_conflicting_dois_send_an_identical_title_to_a_human(self):
        a = record(1, "Measuring empowerment", doi="10.5281/zenodo.2020201")
        b = record(2, "Measuring empowerment", doi="10.1016/j.atm.2020.07.004")
        verdict = compare(a, b)
        assert verdict.status == "uncertain"
        assert "conflicting DOIs" in verdict.evidence["reason"]


class TestRuleThreeFuzzyTitle:
    def test_near_identical_title_with_same_first_author_merges(self):
        a = record(1, "Trust as a precondition for co-management", first_author="larsen")
        b = record(2, "Trust as a pre-condition for co-management", first_author="larsen")
        verdict = compare(a, b)
        assert verdict.rule == "fuzzy_title"
        assert verdict.status == "auto_match"

    def test_near_identical_title_with_different_authors_needs_review(self):
        a = record(1, "Trust as a precondition for co-management", first_author="larsen")
        b = record(2, "Trust as a pre-condition for co-management", first_author="okonkwo")
        assert compare(a, b).status == "uncertain"

    def test_similarity_between_the_thresholds_needs_review(self):
        # One word differs in a long title: similar enough to propose, not
        # similar enough to merge unseen.
        a = record(
            1,
            "Resident participation in sustainable tourism planning: evidence "
            "from the coastal villages of Kerala",
        )
        b = record(
            2,
            "Resident participation in sustainable tourism planning: evidence "
            "from the coastal districts of Kerala",
        )
        verdict = compare(a, b)
        assert verdict.status == "uncertain"
        assert 0.92 <= verdict.score < 0.97

    def test_similarity_below_the_threshold_is_not_proposed(self):
        a = record(1, "Resident participation in tourism")
        b = record(2, "Marine biology of the North Sea")
        assert compare(a, b) is None

    def test_threshold_is_the_reviewers_to_change(self):
        a = record(1, "Resident attitudes to tourism in coastal towns")
        b = record(2, "Resident views on tourism in coastal towns")
        assert compare(a, b, DedupSettings(fuzzy_threshold=0.95)) is None
        loose = compare(a, b, DedupSettings(fuzzy_threshold=0.70))
        assert loose is not None and loose.status == "uncertain"

    def test_a_record_is_never_compared_with_itself(self):
        a = record(1, "One title", doi="10.1234/abc.2020")
        assert compare(a, a) is None


class TestBlocking:
    def test_records_sharing_a_doi_are_compared(self):
        a = record(1, "One title", doi="10.1234/abc.2020")
        b = record(2, "Another title entirely", doi="10.1234/abc.2020")
        assert blocking_keys(a) & blocking_keys(b)

    def test_records_sharing_a_title_opening_are_compared(self):
        a = record(1, "Resident participation in tourism planning")
        b = record(2, "Resident participation in tourism governance")
        assert blocking_keys(a) & blocking_keys(b)

    def test_unrelated_records_are_never_compared(self):
        records = [
            record(1, "Resident participation in tourism planning"),
            record(2, "Marine biology of the North Sea"),
        ]
        assert list(candidate_pairs(records)) == []

    def test_each_pair_is_yielded_once(self):
        records = [
            record(1, "Resident participation in tourism", doi="10.1234/a.1"),
            record(2, "Resident participation in tourism", doi="10.1234/a.1"),
            record(3, "Resident participation in tourism", doi="10.1234/a.1"),
        ]
        pairs = [(a.id, b.id) for a, b in candidate_pairs(records)]
        assert sorted(pairs) == [(1, 2), (1, 3), (2, 3)]

    def test_pairs_are_always_low_id_first(self):
        records = [record(i, "Resident participation in tourism") for i in (9, 2, 5)]
        for a, b in candidate_pairs(records):
            assert a.id < b.id

    def test_an_oversized_block_is_still_bounded(self):
        # 300 identically-titled conference abstracts must not become 45,000
        # comparisons when the block cap is 10.
        records = [record(i, "Abstracts of the annual meeting") for i in range(300)]
        pairs = list(candidate_pairs(records, DedupSettings(max_block_size=10)))
        assert 0 < len(pairs) < 300 * 11


class TestClustering:
    def test_transitive_matches_form_one_cluster(self):
        clusters = cluster_pairs([1, 2, 3, 4], [(1, 2), (2, 3)])
        sets = sorted(sorted(v) for v in clusters.values())
        assert sets == [[1, 2, 3], [4]]

    def test_unmatched_records_are_their_own_cluster(self):
        clusters = cluster_pairs([1, 2], [])
        assert sorted(sorted(v) for v in clusters.values()) == [[1], [2]]

    def test_cluster_root_is_the_lowest_id_for_stability(self):
        clusters = cluster_pairs([5, 9, 7], [(9, 7), (5, 9)])
        assert list(clusters.keys()) == [5]

    def test_edges_referring_to_unknown_records_are_ignored(self):
        clusters = cluster_pairs([1, 2], [(1, 99)])
        assert len(clusters) == 2


class TestChoosingWhatToKeep:
    def test_record_with_an_abstract_beats_one_without(self):
        bare = record(1, "A study", has_abstract=False)
        full = record(2, "A study", has_abstract=True, abstract_len=900)
        assert choose_primary([bare, full]).id == 2

    def test_record_with_a_doi_beats_one_without(self):
        without = record(1, "A study", has_abstract=True)
        with_doi = record(2, "A study", has_abstract=True, doi="10.1234/abc.2020")
        assert choose_primary([without, with_doi]).id == 2

    def test_source_priority_breaks_an_otherwise_equal_tie(self):
        wos = record(1, "A study", has_abstract=True, source_database="Web of Science")
        scopus = record(2, "A study", has_abstract=True, source_database="Scopus")
        assert choose_primary([wos, scopus]).id == 2

    def test_fully_equal_records_resolve_to_the_lowest_id(self):
        a = record(3, "A study", has_abstract=True, source_database="Scopus")
        b = record(8, "A study", has_abstract=True, source_database="Scopus")
        assert choose_primary([a, b]).id == 3

    def test_completeness_rises_with_more_fields(self):
        sparse = record(1, "A study")
        rich = record(
            2, "A study", has_abstract=True, doi="10.1234/a.1", n_authors=3,
            n_keywords=4, has_pages=True, has_language=True,
            journal_norm="journal of things",
        )
        assert completeness_score(rich) > completeness_score(sparse)
