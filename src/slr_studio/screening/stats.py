"""Agreement statistics.

Percentage agreement alone flatters any screening process: when 95% of
records are obvious excludes, two reviewers who both say "exclude" to
everything agree 95% of the time and have shown nothing. Cohen's κ corrects
for agreement expected by chance, and PABAK corrects κ's own weakness -- κ
collapses towards zero when one category dominates, which is exactly the
situation in a systematic review.

Reporting all three is the convention in review methodology, and all three
appear in the Methods draft.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "AgreementResult",
    "agreement",
    "cohens_kappa",
    "pabak",
    "percentage_agreement",
    "interpret_kappa",
    "confusion_matrix",
]


@dataclass
class AgreementResult:
    n: int
    n_agree: int
    percent_agreement: float
    kappa: float | None
    pabak: float | None
    p_observed: float
    p_expected: float
    categories: list[str]
    matrix: dict[str, dict[str, int]] = field(default_factory=dict)
    note: str = ""

    @property
    def interpretation(self) -> str:
        return interpret_kappa(self.kappa)

    def describe(self) -> str:
        if self.n == 0:
            return "No records to compare."
        kappa = "undefined" if self.kappa is None else f"{self.kappa:.3f}"
        pabak = "undefined" if self.pabak is None else f"{self.pabak:.3f}"
        return (
            f"{self.percent_agreement:.1%} agreement on {self.n} records "
            f"(κ = {kappa}, PABAK = {pabak})"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "n_agree": self.n_agree,
            "percent_agreement": round(self.percent_agreement, 4),
            "kappa": None if self.kappa is None else round(self.kappa, 4),
            "pabak": None if self.pabak is None else round(self.pabak, 4),
            "p_observed": round(self.p_observed, 4),
            "p_expected": round(self.p_expected, 4),
            "categories": self.categories,
            "matrix": self.matrix,
            "note": self.note,
        }


def _pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[str, str]]:
    if len(a) != len(b):
        raise ValueError(
            f"Both reviewers must have judged the same records "
            f"({len(a)} vs {len(b)} decisions)."
        )
    return list(zip(a, b))


def percentage_agreement(a: Sequence[str], b: Sequence[str]) -> float:
    pairs = _pairs(a, b)
    if not pairs:
        return 0.0
    return sum(1 for x, y in pairs if x == y) / len(pairs)


def confusion_matrix(
    a: Sequence[str], b: Sequence[str], categories: Sequence[str] | None = None
) -> dict[str, dict[str, int]]:
    pairs = _pairs(a, b)
    cats = list(categories) if categories else sorted({x for pair in pairs for x in pair})
    counts = Counter(pairs)
    return {x: {y: counts.get((x, y), 0) for y in cats} for x in cats}


def cohens_kappa(
    a: Sequence[str], b: Sequence[str], categories: Sequence[str] | None = None
) -> tuple[float | None, float, float]:
    """Return (kappa, observed agreement, expected agreement).

    Kappa is None when it is undefined rather than zero: if both reviewers
    used exactly one category, chance agreement is 1 and the formula divides
    by zero. Reporting 0 there would say "no better than chance" about a
    perfect run, which is the opposite of the truth.
    """
    pairs = _pairs(a, b)
    n = len(pairs)
    if n == 0:
        return None, 0.0, 0.0
    cats = list(categories) if categories else sorted({x for pair in pairs for x in pair})

    p_o = sum(1 for x, y in pairs if x == y) / n
    count_a = Counter(x for x, _ in pairs)
    count_b = Counter(y for _, y in pairs)
    p_e = sum((count_a.get(c, 0) / n) * (count_b.get(c, 0) / n) for c in cats)

    if abs(1.0 - p_e) < 1e-12:
        return None, p_o, p_e
    return (p_o - p_e) / (1.0 - p_e), p_o, p_e


def pabak(p_observed: float, n_categories: int = 2) -> float | None:
    """Prevalence-adjusted bias-adjusted kappa.

    For two categories this is the familiar ``2 * p_o - 1``. The general form
    below extends it to the three-way include / exclude / unclear comparison;
    with one category it is undefined.
    """
    if n_categories < 2:
        return None
    return (n_categories * p_observed - 1.0) / (n_categories - 1.0)


def agreement(
    a: Sequence[str],
    b: Sequence[str],
    *,
    categories: Sequence[str] | None = None,
) -> AgreementResult:
    """Every agreement figure for one pair of reviewers.

    ``categories`` fixes which labels count, so a run where nobody said
    "unclear" is still compared on the same three-category scale as one where
    somebody did. Without it the scale would shift between runs and the
    numbers would not be comparable.
    """
    pairs = _pairs(a, b)
    n = len(pairs)
    cats = list(categories) if categories else sorted({x for pair in pairs for x in pair})
    kappa, p_o, p_e = cohens_kappa(a, b, cats)
    note = ""
    if n == 0:
        note = "No records were judged by both reviewers."
    elif kappa is None:
        note = (
            "κ is undefined here: both reviewers used a single category, "
            "so agreement expected by chance is 100%."
        )
    return AgreementResult(
        n=n,
        n_agree=sum(1 for x, y in pairs if x == y),
        percent_agreement=p_o,
        kappa=kappa,
        pabak=pabak(p_o, max(len(cats), 2)) if n else None,
        p_observed=p_o,
        p_expected=p_e,
        categories=cats,
        matrix=confusion_matrix(a, b, cats) if n else {},
        note=note,
    )


#: The conventional reading of κ, due to Landis and Koch (1977). Offered as a
#: label in the interface, never as a pass mark: the threshold a review should
#: accept is the reviewer's judgement, not the software's.
_KAPPA_BANDS = (
    (0.81, "almost perfect"),
    (0.61, "substantial"),
    (0.41, "moderate"),
    (0.21, "fair"),
    (0.01, "slight"),
    (-1.01, "poor (worse than chance)"),
)


def interpret_kappa(value: float | None) -> str:
    if value is None:
        return "undefined"
    for threshold, label in _KAPPA_BANDS:
        if value >= threshold:
            return label
    return "poor"


def collapse_unclear(decisions: Iterable[str], treat_as: str = "include") -> list[str]:
    """Map 'unclear' onto a binary decision for a two-category comparison.

    Defaulting to 'include' matches what the pipeline does with an unresolved
    record: it is carried to full text rather than dropped, because a wrongly
    included record is caught later and a wrongly excluded one never is.
    """
    return [treat_as if d == "unclear" else d for d in decisions]
