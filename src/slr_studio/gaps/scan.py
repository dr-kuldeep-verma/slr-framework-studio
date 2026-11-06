"""The quick scan's arithmetic: vectors, clusters, trends, co-occurrence.

The spec asks for "embeddings and clustering". These are term-frequency
vectors weighted by inverse document frequency, and agglomerative clustering
over cosine similarity -- computed here rather than by a service, because
nothing may leave the computer except calls to the language model the reviewer
chose, and a scoping view of some titles is not worth an exception to that.

They are also enough. The scan exists to show a reviewer roughly what their
corpus is about and where it is thin, before the review proper. It is
explicitly not a finding, and a better vector space would not change that.

Everything here is a pure function of its input, and the clustering is
deterministic and independent of the order the records arrived in: a reviewer
who re-runs the scan and sees different topics will believe the corpus
changed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "Document",
    "SparseCell",
    "Topic",
    "cluster",
    "co_occurrence",
    "cosine",
    "sparse_cells",
    "tokenise",
    "topic_label",
    "trend",
    "vectorise",
]

Vector = dict[str, float]

#: Function words only. The temptation is to add the boilerplate of academic
#: writing -- study, research, findings, method -- but the inverse document
#: frequency already drives those to nothing when they are everywhere, and it
#: does so per corpus. A fixed list would also strip them from the one review
#: where they are the subject, such as a methods review about mixed methods.
_STOP_WORDS = frozenset(
    """
    a an and are as at be been but by for from had has have how i if in into is
    it its of on or our so than that the their then there these they this to
    was we were what when where which who why will with would
    about above after again against all also among any because before being
    below between both during each few further here more most no nor not only
    other over own same some such through too under until up very
    """.split()
)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

#: Two documents are near enough to join below this cosine distance. Chosen so
#: that papers sharing most of their distinctive vocabulary group, and papers
#: sharing only a field-wide word do not.
_JOIN_BELOW = 0.72

#: Terms in a topic's label.
_LABEL_TERMS = 3


@dataclass(frozen=True)
class Document:
    """One record as the scan sees it."""

    record_id: int
    title: str = ""
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    year: int | None = None
    #: Anything else the sparse-cell cross-tabulation may be asked for:
    #: region, country, design, setting.
    attributes: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return " ".join(
            [self.title or "", self.abstract or "", " ".join(self.keywords or [])]
        )


@dataclass(frozen=True)
class Topic:
    label: str
    record_ids: tuple[int, ...]
    terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class SparseCell:
    row: str
    column: str
    n_records: int
    record_ids: tuple[int, ...]


# -- text -------------------------------------------------------------------


def tokenise(text: str | None) -> list[str]:
    """Content words, lowercased.

    Numbers go: a year or a sample size in a title says nothing about what
    the paper is about, and leaving them in made "2019" one of the strongest
    shared terms in a corpus.
    """
    if not text:
        return []
    return [
        word
        for word in (match.group(0).casefold() for match in _WORD.finditer(text))
        if len(word) > 1 and word not in _STOP_WORDS
    ]


def vectorise(documents: Sequence[Document]) -> dict[int, Vector]:
    """A unit-length tf-idf vector per record.

    Unit length so that cosine similarity is a plain dot product, and so that
    a long abstract does not count for more than a short one -- length is a
    house style, not a signal about topic.
    """
    tokens = {d.record_id: tokenise(d.text()) for d in documents}
    n_documents = len(documents)
    if not n_documents:
        return {}

    document_frequency: dict[str, int] = {}
    for words in tokens.values():
        for word in set(words):
            document_frequency[word] = document_frequency.get(word, 0) + 1

    vectors: dict[int, Vector] = {}
    for record_id, words in tokens.items():
        counts: dict[str, int] = {}
        for word in words:
            counts[word] = counts.get(word, 0) + 1
        vector: Vector = {}
        for word, count in counts.items():
            # A term in every record has an idf of zero and drops out, which
            # is the point: it distinguishes nothing.
            idf = math.log(n_documents / document_frequency[word])
            if idf <= 0:
                continue
            vector[word] = (1.0 + math.log(count)) * idf
        norm = math.sqrt(sum(value * value for value in vector.values()))
        vectors[record_id] = (
            {word: value / norm for word, value in vector.items()} if norm else {}
        )
    return vectors


def cosine(first: Vector, second: Vector) -> float:
    if not first or not second:
        return 0.0
    if len(second) < len(first):
        first, second = second, first
    return sum(value * second.get(word, 0.0) for word, value in first.items())


# -- clustering ---------------------------------------------------------------


def _suggest_k(n_records: int) -> int:
    """How many topics to look for in a corpus of this size.

    Roughly the square root of half the corpus, which gives three or four
    topics for fifty records and about twenty for a thousand -- enough to be
    informative without producing a list nobody will read.
    """
    return max(2, min(25, round(math.sqrt(n_records / 2)) or 2))


def _centroid(vectors: dict[int, Vector], record_ids: Sequence[int]) -> Vector:
    """The unit-length mean of a group's vectors."""
    total: Vector = {}
    for record_id in record_ids:
        for word, value in vectors.get(record_id, {}).items():
            total[word] = total.get(word, 0.0) + value
    norm = math.sqrt(sum(value * value for value in total.values()))
    if not norm:
        return {}
    return {word: value / norm for word, value in total.items()}


def cluster(
    documents: Sequence[Document],
    vectors: dict[int, Vector],
    *,
    n_topics: int | None = None,
    join_below: float = _JOIN_BELOW,
) -> list[Topic]:
    """Group the records into topics: spherical k-means over cosine.

    The first version of this was average-link agglomerative clustering,
    which is the better algorithm and was the wrong choice. The quick scan
    runs on the imported records -- before screening, so the largest set in
    the review -- and agglomerative clustering took four seconds at two
    hundred records and did not finish at two thousand. This is linear in the
    corpus and finishes a few thousand records in seconds.

    It is deterministic, which k-means usually is not: the starting centroids
    are chosen by the furthest-point rule from the lowest record id rather
    than at random, and records are visited in id order. A reviewer who
    re-runs the scan sees the same topics, and one who re-imports in a
    different order sees them too.
    """
    if not documents:
        return []

    ids = sorted(d.record_id for d in documents)
    if len(ids) == 1:
        return [
            Topic(
                label=topic_label(documents, vectors, ids),
                record_ids=tuple(ids),
                terms=tuple(_top_terms(vectors, ids)),
            )
        ]

    k = min(len(ids), max(1, n_topics) if n_topics else _suggest_k(len(ids)))

    # Furthest-point seeding: start from the lowest record id, then
    # repeatedly take the record least like anything chosen so far. Both
    # steps are deterministic and depend on record ids, not arrival order.
    seeds: list[int] = [ids[0]]
    while len(seeds) < k:
        best_id, best_score = None, 2.0
        for record_id in ids:
            if record_id in seeds:
                continue
            closest = max(
                (cosine(vectors.get(record_id, {}), vectors.get(seed, {}))
                 for seed in seeds),
                default=0.0,
            )
            if closest < best_score or (
                closest == best_score and best_id is not None
                and record_id < best_id
            ):
                best_id, best_score = record_id, closest
        if best_id is None:
            break
        seeds.append(best_id)

    centroids = [dict(vectors.get(seed, {})) for seed in seeds]
    assignment: dict[int, int] = {}
    for _ in range(12):
        groups: list[list[int]] = [[] for _ in centroids]
        changed = False
        for record_id in ids:
            vector = vectors.get(record_id, {})
            # Ties go to the earliest centroid, so a record with no text
            # lands somewhere stable rather than wherever rounding sends it.
            best, best_score = 0, -1.0
            for index, centroid in enumerate(centroids):
                score = cosine(vector, centroid)
                if score > best_score:
                    best, best_score = index, score
            groups[best].append(record_id)
            if assignment.get(record_id) != best:
                changed = True
            assignment[record_id] = best
        centroids = [
            _centroid(vectors, group) if group else centroids[index]
            for index, group in enumerate(groups)
        ]
        if not changed:
            break

    kept = [sorted(group) for group in groups if group]
    kept.sort(key=lambda g: (-len(g), g[0]))
    return [
        Topic(
            label=topic_label(documents, vectors, group),
            record_ids=tuple(group),
            terms=tuple(_top_terms(vectors, group)),
        )
        for group in kept
    ]


def _top_terms(
    vectors: dict[int, Vector], record_ids: Sequence[int], how_many: int = _LABEL_TERMS
) -> list[str]:
    """The terms that weigh most across a group, strongest first."""
    totals: dict[str, float] = {}
    for record_id in record_ids:
        for word, value in vectors.get(record_id, {}).items():
            totals[word] = totals.get(word, 0.0) + value
    return [
        word
        for word, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[
            :how_many
        ]
    ]


def topic_label(
    documents: Sequence[Document],
    vectors: dict[int, Vector],
    record_ids: Sequence[int],
) -> str:
    """A name for a topic: the terms its members share and others do not.

    Computed from the token counts rather than from the vectors, because the
    vectors are built for clustering and a term every record in the corpus
    shares is zeroed there on purpose. A topic of two papers that both say
    "community participation" should be labelled for it even when the corpus
    is those two papers -- otherwise every small scan ends up named after
    whichever word happened to appear in only half of each group.
    """
    inside = set(record_ids)
    members = [d for d in documents if d.record_id in inside]
    others = [d for d in documents if d.record_id not in inside]
    if not members:
        return "empty topic"

    counts: dict[str, int] = {}
    for document in members:
        for word in tokenise(document.text()):
            counts[word] = counts.get(word, 0) + 1
    if not counts:
        # Every record in the group had no usable text: say so rather than
        # showing a blank row, which reads like a fault.
        return f"{len(record_ids)} record(s) with no distinctive text"

    # How many documents each side contains each candidate term, counted
    # once. The first version asked that question inside the sort's
    # comparator, which re-tokenised every document in the corpus for every
    # candidate term: 1.3 million calls to `tokenise` for a corpus of 2,500,
    # and nine tenths of the scan's running time.
    in_members: dict[str, int] = {}
    for document in members:
        for word in set(tokenise(document.text())):
            if word in counts:
                in_members[word] = in_members.get(word, 0) + 1
    in_others: dict[str, int] = {}
    for document in others:
        for word in set(tokenise(document.text())):
            if word in counts:
                in_others[word] = in_others.get(word, 0) + 1

    n_members = len(members)
    n_others = len(others)

    def distinctiveness(term: str) -> float:
        here = in_members.get(term, 0) / n_members
        there = in_others.get(term, 0) / n_others if n_others else 0.0
        return here - there

    scored = sorted(
        counts,
        key=lambda term: (-distinctiveness(term), -counts[term], term),
    )
    return ", ".join(scored[:_LABEL_TERMS])


# -- trends and co-occurrence ---------------------------------------------------


def trend(
    documents: Sequence[Document], record_ids: Iterable[int]
) -> dict[int, int]:
    """Records per year, with the quiet years shown as zero.

    A missing year in the middle of a run is the interesting part of a trend,
    so the range is filled rather than only the years that occur. Undated
    records are left out of the series entirely: putting them anywhere would
    invent a date.
    """
    wanted = set(record_ids)
    years = [
        d.year
        for d in documents
        if d.record_id in wanted and d.year is not None
    ]
    if not years:
        return {}
    return {
        year: years.count(year) for year in range(min(years), max(years) + 1)
    }


def co_occurrence(
    documents: Sequence[Document], *, minimum: int = 1
) -> dict[tuple[str, str], int]:
    """How often two keywords appear on the same record.

    Pairs are stored in one order only, so a pair is counted once rather than
    twice. Spellings are folded, for the same reason the keyword index folds
    them: two databases writing one term differently would otherwise look
    like two terms that happen to co-occur everywhere.
    """
    from ..importers.normalise import normalise_title

    counts: dict[tuple[str, str], int] = {}
    for document in documents:
        folded = sorted(
            {
                normalise_title(keyword)
                for keyword in (document.keywords or [])
                if normalise_title(keyword)
            }
        )
        for index, first in enumerate(folded):
            for second in folded[index + 1 :]:
                pair = (first, second)
                counts[pair] = counts.get(pair, 0) + 1
    return {pair: n for pair, n in counts.items() if n >= minimum}


def sparse_cells(
    documents: Sequence[Document],
    row_attribute: str,
    column_attribute: str,
    *,
    threshold: int = 2,
) -> list[SparseCell]:
    """Intersections with few or no records.

    Every combination of the values present is examined, not only the
    combinations that occur, because the empty ones are the whole point. A
    record that does not report an attribute forms its own category rather
    than being dropped: "not reported" is itself a finding about the corpus,
    and dropping those records would make the populated cells look fuller
    than they are.
    """
    def value(document: Document, name: str) -> str:
        raw = document.attributes.get(name)
        text = "" if raw is None else str(raw).strip()
        return text or "not reported"

    rows = sorted({value(d, row_attribute) for d in documents})
    columns = sorted({value(d, column_attribute) for d in documents})
    members: dict[tuple[str, str], list[int]] = {}
    for document in documents:
        key = (value(document, row_attribute), value(document, column_attribute))
        members.setdefault(key, []).append(document.record_id)

    cells = []
    for row in rows:
        for column in columns:
            record_ids = sorted(members.get((row, column), []))
            if len(record_ids) <= threshold:
                cells.append(
                    SparseCell(
                        row=row,
                        column=column,
                        n_records=len(record_ids),
                        record_ids=tuple(record_ids),
                    )
                )
    return cells
