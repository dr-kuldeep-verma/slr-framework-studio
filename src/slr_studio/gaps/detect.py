"""The seven kinds of gap, found from a snapshot of the coded evidence.

These are pure functions over plain dataclasses, with no database in sight.
That is deliberate: the spec's acceptance test is a synthetic corpus with
known holes in which every planted gap must be found and no false gap
reported, and that is only checkable if a corpus can be written out in full
and handed to the detectors directly.

Two rules hold across all seven.

**A gap is an absence within this corpus.** Not an absence in the literature.
Every candidate carries `searched_to`, and the statement built from it says so;
nothing here ever reports an absence unqualified.

**A gap is reported only for categories the corpus contains.** The detectors
cross-tabulate the values that are actually present. They do not hold a list
of the world's countries and report the ones nobody studied -- that would be
a claim about the world rather than about the corpus, and there would be no
end to it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..core.frameworks import BUILT_IN

__all__ = [
    "Corpus",
    "Dataset",
    "GapCandidate",
    "GapStatement",
    "Link",
    "Report",
    "Statement",
    "detect_all",
    "find_author_stated_gaps",
    "find_contradiction_gaps",
    "find_evidence_gaps",
    "find_information_gaps",
    "find_methodological_gaps",
    "find_population_gaps",
    "find_theoretical_gaps",
]

#: The framework stages each review type runs through, in order. A transition
#: between consecutive stages with no graded link behind it is a theoretical
#: gap: the framework claims a path the evidence does not cover.
#: The built-in frameworks, re-exported from the catalogue that now owns
#: them. A reviewer-defined framework is not in here -- it is in the
#: project's database -- which is why a corpus carries its own stage order
#: and this is only the fallback for a caller that did not supply one.
FRAMEWORK_STAGES = BUILT_IN

#: Designs a reviewer will want to know are missing for a construct. Named
#: rather than derived, because "no experiments on this construct" is a
#: finding and "no cross-sectional surveys" usually is not.
WANTED_DESIGNS = ("experiment", "longitudinal", "observed behaviour")

#: How a design family answers for one of those. A report's design_family is
#: the comparable form the synthesis grouped by.
_DESIGN_SYNONYMS = {
    "experiment": ("experiment", "rct", "quasi_experiment", "quasi-experiment"),
    "longitudinal": ("longitudinal", "panel", "cohort", "repeated_measures"),
    "observed behaviour": ("observation", "observed", "behavioural", "field_experiment"),
}

#: Below this many datasets a cell counts as thin rather than populated.
THIN_CELL = 2

#: The context dimensions the spec asks to cross constructs against.
CONTEXT_DIMENSIONS = (
    "country", "region", "income_group", "setting", "population", "activity",
)

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


# -- the snapshot -----------------------------------------------------------


@dataclass(frozen=True)
class Report:
    """One included report, as the detectors need it."""

    record_id: int
    title: str = ""
    country: str = ""
    region: str = ""
    income_group: str = ""
    setting: str = ""
    population: str = ""
    activity: str = ""
    design_family: str = ""
    sample_size: int | None = None
    measures: tuple[str, ...] = ()
    theories: tuple[str, ...] = ()
    framework_stages: tuple[str, ...] = ()
    stimulus_attributes: tuple[str, ...] = ()
    compares_intention_with_behaviour: bool = False


@dataclass(frozen=True)
class Dataset:
    """A sample that votes once, with the context it was drawn from."""

    dataset_id: int
    record_ids: tuple[int, ...] = ()
    country: str = ""
    region: str = ""
    income_group: str = ""
    setting: str = ""
    population: str = ""
    activity: str = ""
    design_family: str = ""


@dataclass(frozen=True)
class Link:
    """A graded link from the inventory."""

    link_id: int
    predictor: str
    outcome: str
    grade: str = "tentative"
    direction: str = "positive"
    n_datasets: int = 0
    record_ids: tuple[int, ...] = ()
    design_families: tuple[str, ...] = ()
    #: Whether the contradiction analysis settled it.
    contested_resolved: bool = False


@dataclass(frozen=True)
class Statement:
    """An author's own statement about what remains to be done."""

    record_id: int
    statement: str
    page: int | None = None
    kind: str = "future_research"


@dataclass(frozen=True)
class Corpus:
    """Everything the detectors read, and nothing else."""

    searched_to: str = ""
    constructs: tuple[str, ...] = ()
    links: tuple[Link, ...] = ()
    datasets: tuple[Dataset, ...] = ()
    reports: tuple[Report, ...] = ()
    #: (predictor, outcome) pairs a proposition claims.
    propositions: tuple[tuple[str, str], ...] = ()
    untested_pathways: tuple[tuple[str, str], ...] = ()
    theories: tuple[str, ...] = ()
    stimulus_attributes: tuple[str, ...] = ()
    future_research: tuple[Statement, ...] = ()
    framework_type: str = "PCC"
    #: The framework's stages, in order. Carried on the corpus rather than
    #: looked up by name, because a reviewer-defined framework lives in the
    #: project's database and the detectors read nothing but this object.
    #: Left empty, a built-in name still resolves; an unknown one gives no
    #: stages, and the engine says so rather than reporting no gaps.
    framework_stage_order: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Fixtures and callers pass generators freely; freeze them once so a
        # detector reading the corpus twice sees the same thing.
        for name in (
            "constructs", "links", "datasets", "reports", "propositions",
            "untested_pathways", "theories", "stimulus_attributes",
            "future_research", "framework_stage_order",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class GapStatement:
    """The prose of a gap, kept apart from its qualifier.

    `body` is the claim; the corpus qualifier is added when the statement is
    written out, so that no code path can produce the claim without it.
    """

    body: str


@dataclass(frozen=True)
class GapCandidate:
    gap_type: str
    statement: GapStatement
    counts: dict[str, Any] = field(default_factory=dict)
    record_ids: tuple[int, ...] = ()
    link_ids: tuple[int, ...] = ()
    statement_ids: tuple[int, ...] = ()
    cell_key: str = ""


def _candidate(gap_type: str, body: str, counts: dict[str, Any], **extra):
    return GapCandidate(
        gap_type=gap_type,
        statement=GapStatement(body=body),
        counts=counts,
        **extra,
    )


def _words(text: str) -> set[str]:
    return {m.group(0).casefold() for m in _WORD.finditer(text or "")}


# -- 1. evidence gaps -------------------------------------------------------


def find_evidence_gaps(corpus: Corpus) -> list[GapCandidate]:
    """Construct pairs with no study, or with only one dataset."""
    by_pair = {(link.predictor, link.outcome): link for link in corpus.links}
    total = len(corpus.datasets) or sum(
        1 for _ in {d.dataset_id for d in corpus.datasets}
    )
    total = total or len(corpus.reports)

    found: list[GapCandidate] = []
    for predictor in corpus.constructs:
        for outcome in corpus.constructs:
            if predictor == outcome:
                continue
            link = by_pair.get((predictor, outcome))
            n_datasets = link.n_datasets if link else 0
            if n_datasets > 1:
                continue
            if n_datasets == 0:
                body = (
                    f"No dataset measures {predictor} together with {outcome} "
                    f"(0 of {total} dataset(s))."
                )
            else:
                body = (
                    f"Only one dataset links {predictor} to {outcome}, so the "
                    f"link is graded {link.grade} and cannot be relied on "
                    f"(1 of {total} dataset(s))."
                )
            found.append(
                _candidate(
                    "evidence",
                    body,
                    {
                        "kind": "pair_unevidenced",
                        "predictor": predictor,
                        "outcome": outcome,
                        "n_datasets": n_datasets,
                        "n_datasets_total": total,
                    },
                    record_ids=tuple(link.record_ids) if link else (),
                    link_ids=(link.link_id,) if link else (),
                    cell_key=f"{predictor} x {outcome}",
                )
            )
    return found


# -- 2. theoretical gaps ----------------------------------------------------


def find_theoretical_gaps(corpus: Corpus) -> list[GapCandidate]:
    """Framework transitions, unmeasured mechanisms, untested pathways."""
    found: list[GapCandidate] = []
    # The corpus's own stages win; a built-in name still resolves without
    # them, so a caller holding only a framework name keeps working.
    stages = corpus.framework_stage_order or BUILT_IN.get(corpus.framework_type, ())
    covered = {
        stage.casefold()
        for report in corpus.reports
        for stage in report.framework_stages
    }
    graded = {
        (link.predictor, link.outcome)
        for link in corpus.links
        if link.grade not in ("asserted",)
    }

    for first, second in zip(stages, stages[1:]):
        if first in covered and second in covered:
            continue
        missing = [
            stage for stage in (first, second) if stage not in covered
        ]
        found.append(
            _candidate(
                "theoretical",
                f"The {corpus.framework_type} transition {first} → "
                f"{second} has no graded link: "
                + ", ".join(f"no report codes the {s} stage" for s in missing)
                + f" (0 of {len(graded)} graded link(s) cover it).",
                {
                    "kind": "stage_transition",
                    "from_stage": first,
                    "to_stage": second,
                    "n_graded_links": len(graded),
                    "stages_not_coded": missing,
                },
                cell_key=f"{first} -> {second}",
            )
        )

    measured = {c.casefold() for c in corpus.constructs}
    for mechanism in _theory_mechanisms(corpus.theories):
        if mechanism.casefold() in measured:
            continue
        found.append(
            _candidate(
                "theoretical",
                f"The mechanism {mechanism!r}, named by the theories this "
                f"review works from, is measured by no construct in the "
                f"codebook (0 of {len(corpus.constructs)} construct(s)).",
                {
                    "kind": "mechanism_not_measured",
                    "mechanism": mechanism,
                    "n_constructs": len(corpus.constructs),
                },
                cell_key=f"mechanism:{mechanism}",
            )
        )

    for predictor, outcome in corpus.untested_pathways:
        found.append(
            _candidate(
                "theoretical",
                f"The pathway {predictor} → {outcome} is proposed in the "
                f"framework but rests on no graded link "
                f"(0 of {len(graded)} graded link(s)).",
                {
                    "kind": "untested_pathway",
                    "predictor": predictor,
                    "outcome": outcome,
                    "n_graded_links": len(graded),
                },
                cell_key=f"{predictor} -> {outcome}",
            )
        )
    return found


def _theory_mechanisms(theories: Sequence[str]) -> list[str]:
    """The mechanisms a reviewer named, from "theory: a, b, c" entries.

    A reviewer writes "protection motivation theory: efficacy, threat". The
    part before the colon is the theory's name and is not a mechanism; the
    parts after it are.
    """
    mechanisms: list[str] = []
    for entry in theories:
        if ":" not in entry:
            continue
        _, listed = entry.split(":", 1)
        for mechanism in re.split(r"[,;]", listed):
            name = mechanism.strip()
            if name and name not in mechanisms:
                mechanisms.append(name)
    return mechanisms


# -- 3. contradiction gaps --------------------------------------------------


def find_contradiction_gaps(corpus: Corpus) -> list[GapCandidate]:
    """Contested links the contradiction analysis did not settle."""
    return [
        _candidate(
            "contradiction",
            f"The link {link.predictor} → {link.outcome} is contested "
            f"across {link.n_datasets} dataset(s) and the contradiction "
            f"analysis did not resolve it.",
            {
                "kind": "unresolved_contradiction",
                "predictor": link.predictor,
                "outcome": link.outcome,
                "n_datasets": link.n_datasets,
            },
            record_ids=tuple(link.record_ids),
            link_ids=(link.link_id,),
            cell_key=f"{link.predictor} x {link.outcome}",
        )
        for link in corpus.links
        if link.grade == "contested" and not link.contested_resolved
    ]


# -- 4. methodological gaps -------------------------------------------------


def _design_matches(family: str, wanted: str) -> bool:
    family = (family or "").casefold().replace(" ", "_")
    return any(
        synonym.replace(" ", "_") in family
        for synonym in _DESIGN_SYNONYMS[wanted]
    )


def find_methodological_gaps(corpus: Corpus) -> list[GapCandidate]:
    """Designs absent for a construct, measures used once, intention vs behaviour."""
    found: list[GapCandidate] = []

    families_by_construct: dict[str, set[str]] = {}
    for link in corpus.links:
        for construct in (link.predictor, link.outcome):
            families_by_construct.setdefault(construct, set()).update(
                link.design_families
            )
    all_families = {r.design_family for r in corpus.reports if r.design_family}

    for construct in corpus.constructs:
        families = families_by_construct.get(construct, set())
        if not families and not all_families:
            continue
        for wanted in WANTED_DESIGNS:
            if any(_design_matches(family, wanted) for family in families):
                continue
            n_matching = sum(
                1 for r in corpus.reports if _design_matches(r.design_family, wanted)
            )
            found.append(
                _candidate(
                    "methodological",
                    f"No study of {construct} uses {'an' if wanted[0] in 'aeiou' else 'a'} "
                    f"{wanted} design ({n_matching} of "
                    f"{len(corpus.reports)} report(s) use one at all).",
                    {
                        "kind": "design_absent",
                        "construct": construct,
                        "design": wanted,
                        "n_reports_with_design": n_matching,
                        "n_reports": len(corpus.reports),
                    },
                    cell_key=f"{construct} x {wanted}",
                )
            )

    uses: dict[str, list[int]] = {}
    for report in corpus.reports:
        for measure in report.measures:
            uses.setdefault(measure.strip(), []).append(report.record_id)
    for measure, record_ids in sorted(uses.items()):
        if len(record_ids) != 1 or not measure:
            continue
        found.append(
            _candidate(
                "methodological",
                f"The measure {measure!r} is used by one report only, so "
                f"nothing in the corpus replicates it "
                f"(1 of {len(corpus.reports)} report(s)).",
                {
                    "kind": "measure_used_once",
                    "measure": measure,
                    "n_reports_using": 1,
                    "n_reports": len(corpus.reports),
                },
                record_ids=tuple(record_ids),
                cell_key=f"measure:{measure}",
            )
        )

    if corpus.reports:
        comparing = [
            r.record_id for r in corpus.reports if r.compares_intention_with_behaviour
        ]
        if not comparing:
            found.append(
                _candidate(
                    "methodological",
                    "No report compares a stated intention with an observed "
                    f"behaviour (0 of {len(corpus.reports)} report(s)), so the "
                    "corpus cannot say whether what people say predicts what "
                    "they do.",
                    {
                        "kind": "intention_behaviour",
                        "n_comparing": 0,
                        "n_reports": len(corpus.reports),
                    },
                    cell_key="intention vs behaviour",
                )
            )
    return found


# -- 5. population and context gaps -----------------------------------------


def find_population_gaps(corpus: Corpus) -> list[GapCandidate]:
    """Constructs crossed against where and whom the studies covered."""
    if not corpus.datasets:
        return []

    by_record: dict[int, list[Dataset]] = {}
    for dataset in corpus.datasets:
        for record_id in dataset.record_ids:
            by_record.setdefault(record_id, []).append(dataset)

    constructs_by_record: dict[int, set[str]] = {}
    for link in corpus.links:
        for record_id in link.record_ids:
            constructs_by_record.setdefault(record_id, set()).update(
                {link.predictor, link.outcome}
            )

    found: list[GapCandidate] = []
    for dimension in CONTEXT_DIMENSIONS:
        # The categories are those the corpus contains, from the reports as
        # well as the datasets: a country that appears in an included report
        # but contributes no dataset is exactly the empty cell worth
        # reporting. The counting unit stays the dataset, because that is
        # what votes.
        values = sorted(
            {
                (getattr(dataset, dimension) or "not reported")
                for dataset in corpus.datasets
            }
            | {
                (getattr(report, dimension, "") or "not reported")
                for report in corpus.reports
            }
        )
        if not values:
            continue
        for construct in corpus.constructs:
            for value in values:
                datasets = [
                    dataset
                    for dataset in corpus.datasets
                    if (getattr(dataset, dimension) or "not reported") == value
                    and any(
                        construct in constructs_by_record.get(record_id, set())
                        for record_id in dataset.record_ids
                    )
                ]
                if len(datasets) > THIN_CELL:
                    continue
                n = len(datasets)
                found.append(
                    _candidate(
                        "population_context",
                        (
                            f"No dataset studies {construct} in "
                            f"{dimension.replace('_', ' ')} {value!r} "
                            f"(0 of {len(corpus.datasets)} dataset(s))."
                            if n == 0
                            else
                            f"Only {n} dataset(s) study {construct} in "
                            f"{dimension.replace('_', ' ')} {value!r} "
                            f"({n} of {len(corpus.datasets)} dataset(s))."
                        ),
                        {
                            "kind": "empty_cell" if n == 0 else "thin_cell",
                            "dimension": dimension,
                            "row": construct,
                            "column": value,
                            "n_datasets": n,
                            "n_datasets_total": len(corpus.datasets),
                        },
                        record_ids=tuple(
                            sorted(
                                {r for d in datasets for r in d.record_ids}
                            )
                        ),
                        cell_key=f"{construct} x {dimension}:{value}",
                    )
                )
    return found


# -- 6. information gaps ----------------------------------------------------


def find_information_gaps(corpus: Corpus) -> list[GapCandidate]:
    """Stimulus attributes the reviewer named that nobody varied."""
    if not corpus.stimulus_attributes:
        return []
    varied: set[str] = set()
    for report in corpus.reports:
        varied.update(a.strip().casefold() for a in report.stimulus_attributes)

    found: list[GapCandidate] = []
    for attribute in corpus.stimulus_attributes:
        if attribute.strip().casefold() in varied:
            continue
        found.append(
            _candidate(
                "information",
                f"No study varies the stimulus attribute {attribute!r} "
                f"(0 of {len(corpus.reports)} report(s)), so the corpus "
                f"cannot say what difference it makes.",
                {
                    "kind": "attribute_never_varied",
                    "attribute": attribute,
                    "n_varying": 0,
                    "n_reports": len(corpus.reports),
                },
                cell_key=f"attribute:{attribute}",
            )
        )
    return found


# -- 7. author-stated gaps --------------------------------------------------


def find_author_stated_gaps(
    corpus: Corpus, *, similarity: float = 0.34
) -> list[GapCandidate]:
    """The authors' own statements, grouped into themes and counted.

    Grouped by shared vocabulary rather than by a model: two papers both
    asking for longitudinal work say so in much the same words, and a
    reviewer can see why two statements were grouped when the reason is the
    words they share.
    """
    statements = list(corpus.future_research)
    if not statements:
        return []

    vectors = [_words(s.statement) for s in statements]
    groups: list[list[int]] = []
    for index in range(len(statements)):
        for group in groups:
            overlaps = [
                _overlap(vectors[index], vectors[other]) for other in group
            ]
            if overlaps and sum(overlaps) / len(overlaps) >= similarity:
                group.append(index)
                break
        else:
            groups.append([index])

    groups.sort(key=lambda g: (-len(g), g[0]))
    found: list[GapCandidate] = []
    for group in groups:
        members = [statements[i] for i in group]
        shared = set.intersection(*(vectors[i] for i in group)) if group else set()
        theme = ", ".join(sorted(shared)[:4]) or members[0].statement[:48]
        found.append(
            _candidate(
                "author_stated",
                f"{len(members)} included report(s) say in their own words that "
                f"work is still needed on: {theme}. For example: "
                f"{members[0].statement!r}.",
                {
                    "kind": "author_stated",
                    "theme": theme,
                    "n_statements": len(members),
                    "n_reports": len({m.record_id for m in members}),
                    # Source and page for every statement, which is what makes
                    # the count checkable rather than asserted.
                    "pages": [[m.record_id, m.page] for m in members],
                },
                record_ids=tuple(sorted({m.record_id for m in members})),
                cell_key=f"author:{theme}",
            )
        )
    return found


def _overlap(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


# -- everything -------------------------------------------------------------


def detect_all(corpus: Corpus) -> list[GapCandidate]:
    """Every detector, in the order the spec lists the types."""
    found: list[GapCandidate] = []
    for detector in (
        find_evidence_gaps,
        find_theoretical_gaps,
        find_contradiction_gaps,
        find_methodological_gaps,
        find_population_gaps,
        find_information_gaps,
        find_author_stated_gaps,
    ):
        found.extend(detector(corpus))
    return found
