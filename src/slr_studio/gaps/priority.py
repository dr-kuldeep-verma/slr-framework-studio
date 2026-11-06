"""Ranking the gaps on the four criteria the spec names.

Centrality, size, practical relevance and feasibility, each scored from 0 to
1, combined with weights the reviewer can change. The weights are normalised,
so a reviewer can type 3, 1, 1, 1 and mean it.

Every score can be overridden, and an override is kept beside the computed
value rather than replacing it, so the register shows both what the
application worked out and what the reviewer decided.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .detect import GapCandidate

__all__ = ["Scores", "Weights", "rank", "score"]

#: The four criteria, in the order the spec lists them.
CRITERIA = ("centrality", "size", "relevance", "feasibility")

#: Gap types a standard design could close without unusual access or cost.
#: An absent design or an empty context cell is a sampling problem; an
#: unresolved contradiction usually needs a study built to adjudicate it, and
#: a missing theoretical mechanism may need an instrument that does not exist.
_FEASIBLE_BY_DEFAULT = frozenset(
    {"evidence", "population_context", "methodological", "information"}
)


@dataclass(frozen=True)
class Weights:
    centrality: float = 1.0
    size: float = 1.0
    relevance: float = 1.0
    feasibility: float = 1.0

    def __post_init__(self) -> None:
        for name in CRITERIA:
            value = getattr(self, name)
            if value < 0:
                raise ValueError(
                    f"The weight for {name} is {value}; weights cannot be "
                    "negative."
                )
        if self.total() <= 0:
            raise ValueError(
                "At least one criterion must carry weight, or every gap "
                "scores the same and the ranking says nothing."
            )

    def total(self) -> float:
        return sum(getattr(self, name) for name in CRITERIA)

    def normalised(self) -> dict[str, float]:
        total = self.total()
        return {name: getattr(self, name) / total for name in CRITERIA}

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in CRITERIA}


@dataclass(frozen=True)
class Scores:
    centrality: float = 0.0
    size: float = 0.0
    relevance: float = 0.0
    feasibility: float = 0.0
    weights: Weights = field(default_factory=Weights)
    overrides: dict[str, float] = field(default_factory=dict)

    @property
    def priority(self) -> float:
        weights = self.weights.normalised()
        return round(
            sum(getattr(self, name) * weights[name] for name in CRITERIA), 6
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "centrality": self.centrality,
            "size": self.size,
            "relevance": self.relevance,
            "feasibility": self.feasibility,
            "priority": self.priority,
            "weights": self.weights.as_dict(),
            "overrides": dict(self.overrides),
        }

    def with_overrides(self, overrides: dict[str, float]) -> "Scores":
        """A copy with the reviewer's scores in place of the computed ones."""
        unknown = set(overrides) - set(CRITERIA)
        if unknown:
            raise ValueError(
                f"Unknown criteria: {sorted(unknown)}. The four are "
                + ", ".join(CRITERIA)
                + "."
            )
        for name, value in overrides.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"The override for {name} is {value}; scores run from 0 "
                    "to 1."
                )
        return replace(
            self,
            **overrides,
            overrides={**self.overrides, **overrides},
        )


def score(
    gap: GapCandidate,
    *,
    n_propositions: int = 0,
    n_propositions_total: int = 0,
    emptiness: float | None = None,
    in_author_gaps: bool = False,
    feasible: bool | None = None,
    weights: Weights | None = None,
) -> Scores:
    """Score one gap on the four criteria.

    `emptiness` is how empty the cells behind the gap are, from 0 (full) to 1
    (nothing at all). Where it is not given it is read from the counts, so a
    detector that reported "0 of 89" does not have to be asked twice.
    """
    weights = weights or Weights()

    centrality = (
        n_propositions / n_propositions_total if n_propositions_total else 0.0
    )

    if emptiness is None:
        emptiness = _emptiness_from(gap.counts)

    relevance = 1.0 if in_author_gaps else 0.0
    if gap.gap_type == "author_stated":
        # The authors named it themselves, which is what this criterion is
        # asking about.
        relevance = 1.0

    if feasible is None:
        feasible = gap.gap_type in _FEASIBLE_BY_DEFAULT

    return Scores(
        centrality=_clamp(centrality),
        size=_clamp(emptiness),
        relevance=_clamp(relevance),
        feasibility=1.0 if feasible else 0.25,
        weights=weights,
    )


def _emptiness_from(counts: dict[str, Any]) -> float:
    """How empty the gap's cells are, from the counts the detector recorded."""
    for found, total in (
        ("n_datasets", "n_datasets_total"),
        ("n_reports_with_design", "n_reports"),
        ("n_varying", "n_reports"),
        ("n_comparing", "n_reports"),
    ):
        if found in counts and counts.get(total):
            return 1.0 - (counts[found] / counts[total])
    if counts.get("n_datasets") == 0:
        return 1.0
    return 0.5


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def rank(
    scored: Iterable[tuple[GapCandidate, Scores]]
) -> list[tuple[GapCandidate, Scores]]:
    """Highest priority first, ties broken so the order is stable."""
    return sorted(
        scored,
        key=lambda pair: (-pair[1].priority, pair[0].gap_type,
                          pair[0].statement.body),
    )
