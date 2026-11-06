"""The matching rules, as pure functions over lightweight records.

Nothing here touches the database, which keeps every rule directly testable
and keeps the thresholds auditable: a reviewer can be told exactly why two
records were treated as the same study.

Rule order matters. A DOI match is decisive; a normalised-title match with a
compatible year is nearly as good; fuzzy similarity is a suggestion that a
human confirms unless it is close to identical.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Iterator

__all__ = [
    "CandidateRecord",
    "DedupSettings",
    "PairVerdict",
    "title_similarity",
    "compare",
    "candidate_pairs",
    "completeness_score",
    "cluster_pairs",
]

#: Which database to prefer as the surviving record, best first. Scopus and
#: Web of Science carry the fullest metadata in most exports.
DEFAULT_SOURCE_PRIORITY = (
    "Scopus",
    "Web of Science",
    "PubMed",
    "EBSCO",
    "ProQuest",
)


@dataclass
class DedupSettings:
    """Thresholds the reviewer can change, recorded with every run."""

    #: Below this similarity a pair is not even proposed.
    fuzzy_threshold: float = 0.92
    #: At or above this similarity a fuzzy pair merges without being asked.
    auto_threshold: float = 0.97
    #: Years this far apart still count as the same study (online-first vs issue).
    year_tolerance: int = 1
    #: Largest blocking group to compare exhaustively; bigger groups are split.
    max_block_size: int = 400
    source_priority: tuple[str, ...] = DEFAULT_SOURCE_PRIORITY

    def as_dict(self) -> dict[str, Any]:
        return {
            "fuzzy_threshold": self.fuzzy_threshold,
            "auto_threshold": self.auto_threshold,
            "year_tolerance": self.year_tolerance,
            "max_block_size": self.max_block_size,
            "source_priority": list(self.source_priority),
        }


@dataclass
class CandidateRecord:
    """Just enough of a record to decide whether two are the same study."""

    id: int
    title_norm: str = ""
    title_key: str = ""
    year: int | None = None
    doi: str | None = None
    first_author: str = ""
    journal_norm: str = ""
    source_database: str = ""
    has_abstract: bool = False
    abstract_len: int = 0
    n_authors: int = 0
    n_keywords: int = 0
    has_pages: bool = False
    has_language: bool = False

    @classmethod
    def from_row(cls, row: Any) -> CandidateRecord:
        import json

        def get(key: str, default: Any = "") -> Any:
            try:
                value = row[key]
            except (KeyError, IndexError):
                return default
            return default if value is None else value

        try:
            authors = json.loads(get("authors", "[]") or "[]")
        except (TypeError, ValueError):
            authors = []
        try:
            keywords = json.loads(get("keywords", "[]") or "[]")
        except (TypeError, ValueError):
            keywords = []
        from ..importers.normalise import normalise_title

        return cls(
            id=int(row["id"]),
            title_norm=get("title_norm") or normalise_title(get("title")),
            title_key=get("title_key"),
            year=row["year"] if row["year"] is not None else None,
            doi=get("doi", None) or None,
            first_author=get("first_author_norm"),
            journal_norm=normalise_title(get("journal")),
            source_database=get("source_database"),
            has_abstract=bool(get("has_abstract", 0)),
            abstract_len=len(str(get("abstract", ""))),
            n_authors=len(authors),
            n_keywords=len(keywords),
            has_pages=bool(str(get("pages", "")).strip()),
            has_language=bool(str(get("language", "")).strip()),
        )


@dataclass
class PairVerdict:
    """Why two records were, or were not, judged the same study."""

    record_a_id: int
    record_b_id: int
    rule: str              # doi_exact | title_year | fuzzy_title
    score: float
    status: str            # auto_match | uncertain | auto_distinct
    evidence: dict[str, Any] = field(default_factory=dict)


# -- similarity ------------------------------------------------------------


def title_similarity(a: str, b: str) -> float:
    """Similarity of two already-normalised titles, in [0, 1].

    Two measures are taken and the larger wins:

    * character-level ratio, which copes with typos and transposed letters;
    * token Dice coefficient, which copes with reordering.

    Dice is used rather than containment on purpose. Containment scores 1.0
    when one title is a prefix of the other, and `Resident participation in
    tourism` is a genuinely different paper from `Resident participation in
    tourism planning in rural Spain`.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    tokens_a, tokens_b = set(a.split()), set(b.split())
    if tokens_a and tokens_b:
        shared = len(tokens_a & tokens_b)
        dice = (2 * shared) / (len(tokens_a) + len(tokens_b))
    else:
        dice = 0.0
    return max(ratio, dice)


def years_compatible(a: int | None, b: int | None, tolerance: int) -> bool:
    """A missing year cannot contradict anything, so it never blocks a match."""
    if a is None or b is None:
        return True
    return abs(a - b) <= tolerance


# -- the three rules -------------------------------------------------------


def compare(
    a: CandidateRecord, b: CandidateRecord, settings: DedupSettings | None = None
) -> PairVerdict | None:
    """Judge one pair. Returns None when the pair is not worth recording."""
    settings = settings or DedupSettings()
    if a.id == b.id:
        return None
    first, second = (a, b) if a.id < b.id else (b, a)

    doi_conflict = bool(a.doi and b.doi and a.doi != b.doi)

    # Rule 1: exact DOI. A shared DOI is the same work, full stop.
    if a.doi and b.doi and a.doi == b.doi:
        return PairVerdict(
            first.id, second.id, "doi_exact", 1.0, "auto_match",
            {"doi": a.doi,
             "title_similarity": round(title_similarity(a.title_norm, b.title_norm), 4)},
        )

    same_title = bool(a.title_norm) and a.title_norm == b.title_norm
    compatible = years_compatible(a.year, b.year, settings.year_tolerance)
    similarity = title_similarity(a.title_norm, b.title_norm)

    # Rule 2: identical normalised title and a year within tolerance.
    if same_title and compatible:
        if doi_conflict:
            # Same title, different DOIs. Usually a correction, a reprint or a
            # conference paper later published as an article -- all of which a
            # human must look at rather than have silently merged.
            return PairVerdict(
                first.id, second.id, "title_year", 1.0, "uncertain",
                {"reason": "identical title but conflicting DOIs",
                 "doi_a": a.doi, "doi_b": b.doi,
                 "year_a": a.year, "year_b": b.year},
            )
        return PairVerdict(
            first.id, second.id, "title_year", 1.0, "auto_match",
            {"title": a.title_norm, "year_a": a.year, "year_b": b.year,
             "first_author_a": a.first_author, "first_author_b": b.first_author},
        )

    # Rule 3: fuzzy title similarity above the reviewer's threshold.
    if similarity >= settings.fuzzy_threshold:
        if not compatible:
            return PairVerdict(
                first.id, second.id, "fuzzy_title", round(similarity, 4), "auto_distinct",
                {"reason": "years too far apart", "year_a": a.year, "year_b": b.year},
            )
        evidence = {
            "similarity": round(similarity, 4),
            "year_a": a.year,
            "year_b": b.year,
            "first_author_a": a.first_author,
            "first_author_b": b.first_author,
            "same_first_author": bool(a.first_author)
            and a.first_author == b.first_author,
            "same_journal": bool(a.journal_norm) and a.journal_norm == b.journal_norm,
        }
        if doi_conflict:
            evidence["reason"] = "conflicting DOIs"
            evidence["doi_a"], evidence["doi_b"] = a.doi, b.doi
            return PairVerdict(
                first.id, second.id, "fuzzy_title", round(similarity, 4),
                "uncertain", evidence,
            )
        status = (
            "auto_match"
            if similarity >= settings.auto_threshold and evidence["same_first_author"]
            else "uncertain"
        )
        return PairVerdict(
            first.id, second.id, "fuzzy_title", round(similarity, 4), status, evidence
        )

    return None


# -- blocking --------------------------------------------------------------


def blocking_keys(record: CandidateRecord) -> set[str]:
    """Cheap keys that two records must share to be worth comparing.

    Comparing every pair in a 20,000-record import is 200 million
    comparisons. Blocking cuts that to the pairs that could plausibly match,
    and the keys overlap so that a record missing one field is still reachable
    through another.
    """
    keys: set[str] = set()
    if record.doi:
        keys.add(f"doi:{record.doi}")
    if record.title_key:
        keys.add(f"tk:{record.title_key}")
    if record.title_norm:
        tokens = record.title_norm.split()
        keys.add(f"tp:{record.title_norm[:12]}")
        if len(tokens) >= 2:
            # Last two words catch titles whose opening was truncated.
            keys.add(f"ts:{' '.join(tokens[-2:])}")
    if record.first_author and record.year:
        keys.add(f"ay:{record.first_author}:{record.year}")
    return keys


def candidate_pairs(
    records: Iterable[CandidateRecord], settings: DedupSettings | None = None
) -> Iterator[tuple[CandidateRecord, CandidateRecord]]:
    """Yield each plausible pair once, in a deterministic order."""
    settings = settings or DedupSettings()
    records = list(records)
    blocks: dict[str, list[CandidateRecord]] = defaultdict(list)
    for record in records:
        for key in blocking_keys(record):
            blocks[key].append(record)

    seen: set[tuple[int, int]] = set()
    for key in sorted(blocks):
        members = blocks[key]
        if len(members) < 2:
            continue
        if len(members) > settings.max_block_size:
            # An over-full block means a generic key (a long series of
            # identically-titled conference abstracts, say). Sort it so the
            # comparison window is at least stable, and only compare within it.
            members = sorted(members, key=lambda r: (r.title_norm, r.id))
            for index, left in enumerate(members):
                for right in members[index + 1 : index + 1 + settings.max_block_size]:
                    pair = (min(left.id, right.id), max(left.id, right.id))
                    if pair not in seen:
                        seen.add(pair)
                        yield (left, right) if left.id < right.id else (right, left)
            continue
        members = sorted(members, key=lambda r: r.id)
        for index, left in enumerate(members):
            for right in members[index + 1 :]:
                pair = (left.id, right.id)
                if pair not in seen:
                    seen.add(pair)
                    yield left, right


# -- choosing the record to keep -------------------------------------------


def completeness_score(
    record: CandidateRecord, settings: DedupSettings | None = None
) -> float:
    """How much a record is worth keeping when duplicates are merged.

    Weighted towards the fields the later stages actually need: an abstract to
    screen on, a DOI to retrieve with.
    """
    settings = settings or DedupSettings()
    score = 0.0
    if record.has_abstract:
        score += 3.0
    if record.doi:
        score += 2.0
    if record.n_authors:
        score += 1.0
    if record.year is not None:
        score += 1.0
    if record.journal_norm:
        score += 1.0
    if record.n_keywords:
        score += 1.0
    if record.has_pages:
        score += 0.5
    if record.has_language:
        score += 0.5
    priority = settings.source_priority
    if record.source_database in priority:
        score += 0.25 * (len(priority) - priority.index(record.source_database))
    # A longer abstract breaks ties between otherwise equal records.
    score += min(record.abstract_len, 4000) / 100_000
    return score


def choose_primary(
    records: list[CandidateRecord], settings: DedupSettings | None = None
) -> CandidateRecord:
    """Pick the record that survives a merge; ties break on lowest id."""
    settings = settings or DedupSettings()
    return max(records, key=lambda r: (completeness_score(r, settings), -r.id))


# -- clustering ------------------------------------------------------------


def cluster_pairs(
    record_ids: Iterable[int], matched_pairs: Iterable[tuple[int, int]]
) -> dict[int, list[int]]:
    """Group records into duplicate sets by transitive closure (union-find).

    If A matches B and B matches C, all three are one study even when A and C
    were never compared directly -- which happens whenever three databases
    spell a title three ways.
    """
    parent: dict[int, int] = {rid: rid for rid in record_ids}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(x: int, y: int) -> None:
        root_x, root_y = find(x), find(y)
        if root_x != root_y:
            # Keep the lower id as the root so clusters are stable across runs.
            if root_x < root_y:
                parent[root_y] = root_x
            else:
                parent[root_x] = root_y

    for a, b in matched_pairs:
        if a in parent and b in parent:
            union(a, b)

    clusters: dict[int, list[int]] = defaultdict(list)
    for rid in parent:
        clusters[find(rid)].append(rid)
    return {root: sorted(members) for root, members in clusters.items()}
