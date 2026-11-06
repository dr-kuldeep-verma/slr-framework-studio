"""The quick scan's arithmetic: vectors, clusters, trends, co-occurrence.

All of it is pure and offline. The spec asks for "embeddings and clustering";
these are term-frequency vectors and agglomerative clustering, computed here
rather than by a service, because the one standing constraint on this
application is that nothing leaves the computer except calls to the language
model the reviewer chose, and a quick scan of titles is not worth breaking
that for.

The scan is a scoping view and not a finding, which is what the tests about
determinism and labelling are protecting: a reviewer who re-runs it must see
the same topics, or they will believe the corpus changed.
"""

from __future__ import annotations

import math

import pytest

from slr_studio.gaps.scan import (
    Document,
    cluster,
    cosine,
    co_occurrence,
    sparse_cells,
    tokenise,
    topic_label,
    trend,
    vectorise,
)


def doc(ident, title, *, abstract="", keywords=(), year=2020, **extra):
    return Document(
        record_id=ident,
        title=title,
        abstract=abstract,
        keywords=list(keywords),
        year=year,
        attributes=dict(extra),
    )


class TestTokenise:
    def test_it_lowercases_and_splits_on_punctuation(self):
        assert tokenise("Trust, Participation; and PLANNING!") == [
            "trust", "participation", "planning"
        ]

    def test_stop_words_are_dropped(self):
        """"The" and "of" are in every record and separate nothing."""
        tokens = tokenise("The effect of trust on the participation of residents")
        assert "the" not in tokens and "of" not in tokens
        assert "trust" in tokens

    def test_very_short_tokens_are_dropped(self):
        assert "a" not in tokenise("a trust study")

    def test_numbers_alone_are_dropped(self):
        assert tokenise("study 2019 of trust") == ["study", "trust"]

    def test_hyphenated_words_become_two_tokens(self):
        assert tokenise("co-management") == ["co", "management"]

    def test_it_is_empty_for_empty_text(self):
        assert tokenise("") == []
        assert tokenise(None) == []


class TestVectorise:
    def test_a_term_in_every_record_carries_no_weight(self):
        """Inverse document frequency: a word everywhere distinguishes nothing."""
        docs = [doc(i, "tourism study of trust") for i in range(1, 5)]
        vectors = vectorise(docs)
        assert vectors[1].get("tourism", 0.0) == pytest.approx(0.0)

    def test_a_rare_term_carries_weight(self):
        docs = [doc(1, "tourism and ecotourism")] + [
            doc(i, "tourism and planning") for i in range(2, 6)
        ]
        vectors = vectorise(docs)
        assert vectors[1]["ecotourism"] > 0.0

    def test_keywords_count_as_well_as_the_title(self):
        docs = [
            doc(1, "a study", keywords=["benefit sharing"]),
            doc(2, "a study", keywords=["planning"]),
            doc(3, "a study", keywords=["planning"]),
        ]
        vectors = vectorise(docs)
        assert "benefit" in vectors[1]

    def test_a_record_with_no_text_gets_an_empty_vector(self):
        docs = [doc(1, ""), doc(2, "trust and planning"), doc(3, "trust")]
        assert vectorise(docs)[1] == {}

    def test_vectors_are_unit_length_so_cosine_is_a_dot_product(self):
        docs = [
            doc(1, "trust and fairness"),
            doc(2, "planning and participation"),
            doc(3, "trust and planning"),
        ]
        for vector in vectorise(docs).values():
            if vector:
                assert math.sqrt(sum(v * v for v in vector.values())) == (
                    pytest.approx(1.0)
                )


class TestCosine:
    def test_identical_vectors_are_one(self):
        assert cosine({"a": 1.0}, {"a": 1.0}) == pytest.approx(1.0)

    def test_disjoint_vectors_are_zero(self):
        assert cosine({"a": 1.0}, {"b": 1.0}) == pytest.approx(0.0)

    def test_an_empty_vector_is_zero_not_an_error(self):
        assert cosine({}, {"a": 1.0}) == 0.0


class TestCluster:
    def test_records_about_the_same_thing_land_together(self):
        docs = [
            doc(1, "trust and participation in tourism planning"),
            doc(2, "participation and trust in planning for tourism"),
            doc(3, "coral reef bleaching and sea temperature"),
            doc(4, "sea temperature and coral bleaching on reefs"),
        ]
        topics = cluster(docs, vectorise(docs))
        grouped = {frozenset(t.record_ids) for t in topics}
        assert frozenset({1, 2}) in grouped
        assert frozenset({3, 4}) in grouped

    def test_every_record_lands_in_exactly_one_topic(self):
        docs = [doc(i, f"study {i} of trust and planning") for i in range(1, 9)]
        topics = cluster(docs, vectorise(docs))
        placed = [r for topic in topics for r in topic.record_ids]
        assert sorted(placed) == list(range(1, 9))

    def test_a_record_with_no_text_is_still_placed(self):
        """Dropping it would make the corpus look smaller than it is."""
        docs = [doc(1, ""), doc(2, "trust"), doc(3, "trust and planning")]
        topics = cluster(docs, vectorise(docs))
        assert 1 in [r for topic in topics for r in topic.record_ids]

    def test_the_same_corpus_clusters_the_same_way_twice(self):
        docs = [
            doc(1, "trust and participation"),
            doc(2, "coral reefs and bleaching"),
            doc(3, "participation and trust"),
            doc(4, "bleaching of coral"),
        ]
        vectors = vectorise(docs)
        first = [sorted(t.record_ids) for t in cluster(docs, vectors)]
        second = [sorted(t.record_ids) for t in cluster(docs, vectors)]
        assert first == second

    def test_the_order_records_arrive_in_does_not_change_the_topics(self):
        """A reviewer who re-imports in a different order sees the same scan."""
        docs = [
            doc(1, "trust and participation in planning"),
            doc(2, "coral reefs and bleaching events"),
            doc(3, "participation and trust in planning"),
            doc(4, "bleaching of coral reefs"),
        ]
        forward = {
            frozenset(t.record_ids) for t in cluster(docs, vectorise(docs))
        }
        backward_docs = list(reversed(docs))
        backward = {
            frozenset(t.record_ids)
            for t in cluster(backward_docs, vectorise(backward_docs))
        }
        assert forward == backward

    def test_one_record_is_one_topic(self):
        docs = [doc(1, "trust")]
        assert len(cluster(docs, vectorise(docs))) == 1

    def test_no_records_is_no_topics_rather_than_an_error(self):
        assert cluster([], {}) == []

    def test_the_number_of_topics_can_be_asked_for(self):
        docs = [doc(i, f"study {i} about trust planning reefs") for i in range(1, 13)]
        assert len(cluster(docs, vectorise(docs), n_topics=3)) <= 3


class TestTopicLabel:
    def test_a_topic_is_named_for_its_commonest_distinctive_terms(self):
        docs = [
            doc(1, "community participation in tourism planning"),
            doc(2, "participation of the community in planning"),
        ]
        label = topic_label(docs, vectorise(docs), [1, 2])
        assert "participation" in label or "community" in label

    def test_a_topic_with_no_text_is_named_as_such_not_blank(self):
        docs = [doc(1, "")]
        assert topic_label(docs, vectorise(docs), [1]).strip()


class TestTrend:
    def test_it_counts_records_per_year(self):
        docs = [
            doc(1, "a", year=2019), doc(2, "b", year=2020),
            doc(3, "c", year=2020), doc(4, "d", year=2021),
        ]
        assert trend(docs, [1, 2, 3, 4]) == {2019: 1, 2020: 2, 2021: 1}

    def test_a_year_with_none_is_shown_as_zero_not_skipped(self):
        """A gap in the middle of a trend is the interesting part."""
        docs = [doc(1, "a", year=2019), doc(2, "b", year=2022)]
        assert trend(docs, [1, 2]) == {2019: 1, 2020: 0, 2021: 0, 2022: 1}

    def test_records_without_a_year_are_counted_separately(self):
        docs = [doc(1, "a", year=None), doc(2, "b", year=2020)]
        counts = trend(docs, [1, 2])
        assert counts[2020] == 1
        assert None not in counts        # undated records are not a year

    def test_no_records_is_an_empty_trend(self):
        assert trend([], []) == {}


class TestCoOccurrence:
    def test_two_keywords_on_one_record_co_occur(self):
        docs = [doc(1, "a", keywords=["trust", "planning"])]
        assert co_occurrence(docs)[("planning", "trust")] == 1

    def test_the_pair_is_ordered_so_it_is_counted_once(self):
        docs = [
            doc(1, "a", keywords=["trust", "planning"]),
            doc(2, "b", keywords=["planning", "trust"]),
        ]
        counts = co_occurrence(docs)
        assert counts[("planning", "trust")] == 2
        assert ("trust", "planning") not in counts

    def test_a_keyword_does_not_co_occur_with_itself(self):
        docs = [doc(1, "a", keywords=["trust", "trust"])]
        assert co_occurrence(docs) == {}

    def test_spellings_of_one_keyword_are_counted_as_one(self):
        docs = [
            doc(1, "a", keywords=["Community Participation", "trust"]),
            doc(2, "b", keywords=["community participation", "trust"]),
        ]
        counts = co_occurrence(docs)
        assert len(counts) == 1
        assert list(counts.values()) == [2]


class TestSparseCells:
    def test_an_empty_intersection_is_reported(self):
        docs = [
            doc(1, "a", topic="trust", region="Asia"),
            doc(2, "b", topic="trust", region="Asia"),
            doc(3, "c", topic="fairness", region="Africa"),
        ]
        cells = sparse_cells(docs, "topic", "region", threshold=1)
        empty = {(c.row, c.column): c.n_records for c in cells if c.n_records == 0}
        assert ("trust", "Africa") in empty
        assert ("fairness", "Asia") in empty

    def test_a_thin_cell_is_reported_with_its_count(self):
        docs = [
            doc(1, "a", topic="trust", region="Asia"),
            doc(2, "b", topic="trust", region="Asia"),
            doc(3, "c", topic="trust", region="Africa"),
        ]
        cells = sparse_cells(docs, "topic", "region", threshold=1)
        thin = {(c.row, c.column): c.n_records for c in cells}
        assert thin[("trust", "Africa")] == 1

    def test_a_well_populated_cell_is_not_reported(self):
        docs = [doc(i, "a", topic="trust", region="Asia") for i in range(1, 6)]
        assert sparse_cells(docs, "topic", "region", threshold=1) == []

    def test_a_missing_attribute_is_its_own_category_not_a_silent_drop(self):
        docs = [
            doc(1, "a", topic="trust"),
            doc(2, "b", topic="trust", region="Asia"),
            doc(3, "c", topic="trust", region="Asia"),
        ]
        cells = sparse_cells(docs, "topic", "region", threshold=1)
        assert any(c.column in ("", "not reported") for c in cells)

    def test_the_cells_carry_the_records_behind_them(self):
        docs = [
            doc(1, "a", topic="trust", region="Asia"),
            doc(2, "b", topic="fairness", region="Asia"),
            doc(3, "c", topic="fairness", region="Asia"),
        ]
        cells = sparse_cells(docs, "topic", "region", threshold=1)
        for cell in cells:
            assert len(cell.record_ids) == cell.n_records


class TestItScalesToARealCorpus:
    """The quick scan runs on the imported records, before any screening.

    That is the largest set in the whole review -- thousands of records, not
    the dozens a graded inventory holds. The first implementation was
    average-link agglomerative clustering, which took four seconds at two
    hundred records and would have taken hours at the size this actually
    runs on.
    """

    def _corpus(self, n):
        import random

        rng = random.Random(1)
        vocab = (
            "trust fairness participation planning tourism reef coral bleaching "
            "governance committee benefit sharing ecotourism heritage livelihood"
        ).split()
        return [
            doc(
                i,
                " ".join(rng.sample(vocab, 5)),
                # Real records carry abstracts. The first version of this
                # fixture used bare titles, which made the scan look eight
                # times faster than it is on anything a reviewer imports.
                abstract=" ".join(rng.choices(vocab, k=60)),
                keywords=rng.sample(vocab, 2),
                year=2015 + i % 8,
            )
            for i in range(1, n + 1)
        ]

    def test_two_thousand_records_cluster_in_reasonable_time(self):
        import time

        docs = self._corpus(2000)
        vectors = vectorise(docs)
        started = time.monotonic()
        topics = cluster(docs, vectors)
        elapsed = time.monotonic() - started
        # A scoping scan is something a reviewer runs while deciding what
        # their review is about. Half a minute of staring at a progress bar
        # is the difference between a tool they use and one they skip.
        assert elapsed < 10.0, f"took {elapsed:.1f}s"
        placed = sorted(r for t in topics for r in t.record_ids)
        assert placed == [d.record_id for d in docs]

    def test_it_is_still_deterministic_at_that_size(self):
        docs = self._corpus(400)
        vectors = vectorise(docs)
        first = [sorted(t.record_ids) for t in cluster(docs, vectors)]
        second = [sorted(t.record_ids) for t in cluster(docs, vectors)]
        assert first == second
