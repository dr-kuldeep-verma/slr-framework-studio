"""Scoring a screening run against a human-labelled gold standard.

This module exists so that the claim "the AI screens well enough" can be
checked rather than asserted. Until it existed, the application had 1,400
tests proving it behaved as designed and no evidence whatever about whether
the designed behaviour finds the right studies.

Two things shape every definition here.

**There are two recalls, and only one of them is the honest headline.**
The workflow is an AI first pass that a human confirms, so a record the AI
marked `unclear` is not lost -- it goes to the human queue and can still be
included. A record the AI marked `exclude` *is* lost: nothing downstream
ever sees it again. So `workflow_recall` (include + unclear) is what a
reviewer actually risks, and `strict_recall` (include only) is what the
model would achieve unsupervised. Reporting only the first would flatter
the tool; reporting only the second would describe a workflow nobody uses.

**Missing a relevant study is not symmetric with reading an irrelevant
one.** One costs a review its completeness and cannot be detected
afterwards; the other costs a reviewer a minute. So the loss count is
reported as a count with the records named, not folded into an F-score
where a good specificity can hide it.

Every proportion carries a Wilson interval, because these benchmarks are
small and a recall of 1.0 over 12 relevant records is not evidence of a
perfect screener.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from ..synthesis.stats import Interval, wilson_interval

__all__ = ["ScreeningScore", "score_screening", "DECISIONS"]

#: What the screening engine can return for a record.
DECISIONS = ("include", "exclude", "unclear")

#: Decisions that keep a record in the review. `unclear` is here because it
#: reaches the human queue rather than being discarded.
_SURVIVES = ("include", "unclear")


@dataclass(frozen=True)
class ScreeningScore:
    """What a screening run achieved against known-correct labels."""

    n_records: int
    n_relevant: int
    n_to_human: int
    n_lost: int
    #: Positions in the input of relevant records the AI excluded outright.
    #: Kept so the report can name them: "87% recall" and "it threw away
    #: these three papers" are the same fact, and only one of them is read.
    lost_indices: list[int] = field(default_factory=list)

    strict_recall: float | None = None
    workflow_recall: float | None = None
    specificity: float | None = None
    precision: float | None = None
    burden: float | None = None
    number_needed_to_read: float | None = None

    strict_recall_interval: tuple[float, float] = (0.0, 1.0)
    workflow_recall_interval: tuple[float, float] = (0.0, 1.0)
    specificity_interval: tuple[float, float] = (0.0, 1.0)

    #: The raw confusion counts, so a reader can recompute anything here.
    counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_records": self.n_records,
            "n_relevant": self.n_relevant,
            "n_to_human": self.n_to_human,
            "n_lost": self.n_lost,
            "strict_recall": self.strict_recall,
            "workflow_recall": self.workflow_recall,
            "specificity": self.specificity,
            "precision": self.precision,
            "burden": self.burden,
            "number_needed_to_read": self.number_needed_to_read,
            "strict_recall_interval": list(self.strict_recall_interval),
            "workflow_recall_interval": list(self.workflow_recall_interval),
            "specificity_interval": list(self.specificity_interval),
            "counts": dict(self.counts),
        }


def _interval(successes: int, n: int) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    result: Interval = wilson_interval(successes, n)
    return (result.low, result.high)


def score_screening(
    *,
    gold: Sequence[int],
    decisions: Sequence[str],
) -> ScreeningScore:
    """Score `decisions` against `gold`, aligned by position.

    *gold* is 1 for a record the human reviewers kept at title/abstract
    screening and 0 for one they rejected. *decisions* holds this
    application's decision for the same record.

    Raises rather than guessing on anything it does not understand: a
    benchmark that quietly buckets an unexpected decision produces a number
    that looks like evidence and is not.
    """
    if len(gold) != len(decisions):
        raise ValueError(
            f"{len(gold)} labels and {len(decisions)} decisions: a benchmark "
            "can only score records it can line up, and they must be the "
            "same length and in the same order."
        )
    unknown = sorted({d for d in decisions if d not in DECISIONS})
    if unknown:
        raise ValueError(
            f"Decisions this scorer does not recognise: {', '.join(unknown)}. "
            f"Expected one of {', '.join(DECISIONS)}. Nothing is bucketed by "
            "guesswork here, because the resulting figure would be wrong in "
            "a way nobody could see."
        )

    counts = {f"{truth}_{d}": 0 for truth in ("relevant", "irrelevant") for d in DECISIONS}
    lost: list[int] = []
    for index, (truth, decision) in enumerate(zip(gold, decisions)):
        bucket = "relevant" if truth else "irrelevant"
        counts[f"{bucket}_{decision}"] += 1
        if truth and decision == "exclude":
            lost.append(index)

    n_records = len(gold)
    n_relevant = sum(1 for g in gold if g)
    n_irrelevant = n_records - n_relevant

    relevant_included = counts["relevant_include"]
    relevant_survived = sum(counts[f"relevant_{d}"] for d in _SURVIVES)
    irrelevant_survived = sum(counts[f"irrelevant_{d}"] for d in _SURVIVES)
    irrelevant_excluded = counts["irrelevant_exclude"]
    n_to_human = relevant_survived + irrelevant_survived

    # None, not zero, where the denominator is empty: "no relevant records
    # in this sample" and "found none of them" are opposite facts and must
    # not print the same.
    strict_recall = relevant_included / n_relevant if n_relevant else None
    workflow_recall = relevant_survived / n_relevant if n_relevant else None
    specificity = irrelevant_excluded / n_irrelevant if n_irrelevant else None
    precision = relevant_survived / n_to_human if n_to_human else None
    burden = n_to_human / n_records if n_records else None
    nnr = n_to_human / relevant_survived if relevant_survived else None

    return ScreeningScore(
        n_records=n_records,
        n_relevant=n_relevant,
        n_to_human=n_to_human,
        n_lost=len(lost),
        lost_indices=lost,
        strict_recall=strict_recall,
        workflow_recall=workflow_recall,
        specificity=specificity,
        precision=precision,
        burden=burden,
        number_needed_to_read=nnr,
        strict_recall_interval=_interval(relevant_included, n_relevant),
        workflow_recall_interval=_interval(relevant_survived, n_relevant),
        specificity_interval=_interval(irrelevant_excluded, n_irrelevant),
        counts=counts,
    )
