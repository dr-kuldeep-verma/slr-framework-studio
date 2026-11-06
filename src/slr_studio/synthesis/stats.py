"""The two statistics the grading rules are built on.

**The exact sign test** asks whether a split of votes is further from even
than chance would readily produce. It is exact rather than normal-approximate
because the counts here are small -- a link supported by seven datasets is a
well-evidenced link in a systematic review, and the normal approximation is
unreliable at that size.

**The Wilson score interval** gives the uncertainty around the proportion
agreeing. The textbook Wald interval is wrong in exactly the cases this
review will meet most often: at 5 of 5 agreeing it reports an interval of
zero width, claiming certainty from five observations. Wilson does not.

Both are written out rather than taken from SciPy: the application ships as a
Windows executable, and the two functions below are a few lines each against
a dependency of some tens of megabytes. Both are tested against worked
examples with known answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import comb, isclose, sqrt

__all__ = [
    "Interval",
    "SignTest",
    "binomial_pmf",
    "sign_test",
    "wilson_interval",
]

#: The two-sided normal deviate for a 95% interval. Written out for the same
#: reason as the functions themselves.
_Z = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}


@dataclass(frozen=True)
class Interval:
    """A confidence interval around a proportion."""

    proportion: float
    low: float
    high: float
    level: float = 0.95
    n: int = 0

    def describe(self) -> str:
        return (
            f"{self.proportion:.0%} "
            f"({self.level:.0%} CI {self.low:.0%} to {self.high:.0%})"
        )

    def as_dict(self) -> dict[str, float | int]:
        return {
            "proportion": round(self.proportion, 6),
            "low": round(self.low, 6),
            "high": round(self.high, 6),
            "level": self.level,
            "n": self.n,
        }


@dataclass(frozen=True)
class SignTest:
    """The outcome of a two-sided exact sign test."""

    n_positive: int
    n_negative: int
    n_used: int
    n_tied: int
    p_value: float | None
    note: str = ""

    @property
    def significant(self) -> bool:
        """At the conventional 5%. Undefined p is not significant."""
        return self.p_value is not None and self.p_value < 0.05

    def describe(self) -> str:
        if self.p_value is None:
            return f"No sign test: {self.note}"
        return (
            f"{self.n_positive} positive against {self.n_negative} negative "
            f"(two-sided exact p = {self.p_value:.4f})"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "n_positive": self.n_positive,
            "n_negative": self.n_negative,
            "n_used": self.n_used,
            "n_tied": self.n_tied,
            "p_value": None if self.p_value is None else round(self.p_value, 8),
            "significant": self.significant,
            "note": self.note,
        }


def binomial_pmf(k: int, n: int, p: float = 0.5) -> float:
    """P(exactly k successes in n trials)."""
    if k < 0 or k > n:
        return 0.0
    return comb(n, k) * (p**k) * ((1.0 - p) ** (n - k))


def sign_test(
    n_positive: int, n_negative: int, *, n_tied: int = 0
) -> SignTest:
    """Two-sided exact sign test on a split of directions.

    Ties are excluded from the test, which is what the sign test does with
    them: a dataset that found no relationship is evidence about the size of
    an effect, not about its direction, so it cannot speak for either side.
    It is still counted and reported, because a link where most datasets
    found nothing should not read as strong.

    The two-sided p-value is the total probability of every outcome no more
    likely than the one observed, under a fair coin. For a symmetric null
    this equals twice the smaller tail, except that doubling can exceed 1 at
    small n -- hence the explicit sum rather than the shortcut.
    """
    if n_positive < 0 or n_negative < 0 or n_tied < 0:
        raise ValueError("Vote counts cannot be negative.")
    n = n_positive + n_negative
    if n == 0:
        return SignTest(
            n_positive, n_negative, 0, n_tied, None,
            "every dataset was tied, so there is no direction to test",
        )

    observed = binomial_pmf(n_positive, n)
    # A floating-point nudge: outcomes that are equally likely in exact
    # arithmetic can differ in the last bit, and dropping one would halve the
    # p-value of a perfectly symmetric split.
    p_value = sum(
        pmf
        for k in range(n + 1)
        if (pmf := binomial_pmf(k, n)) <= observed
        or isclose(pmf, observed, rel_tol=1e-12)
    )
    return SignTest(n_positive, n_negative, n, n_tied, min(1.0, p_value))


def wilson_interval(
    successes: int, n: int, *, level: float = 0.95
) -> Interval:
    """Wilson score interval for a proportion.

    Unlike the Wald interval it never leaves [0, 1] and never collapses to a
    point at 0 or 100%, which is the case that matters here: a link where
    five of five datasets agree is well evidenced, but five observations do
    not justify an interval of zero width.
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"{successes} successes out of {n} is not a proportion.")
    if n == 0:
        return Interval(0.0, 0.0, 1.0, level, 0)
    try:
        z = _Z[level]
    except KeyError:
        raise ValueError(
            f"No deviate stored for a {level:.0%} interval; "
            f"available: {', '.join(f'{k:.0%}' for k in sorted(_Z))}."
        ) from None

    proportion = successes / n
    denominator = 1.0 + z * z / n
    centre = (proportion + z * z / (2 * n)) / denominator
    spread = (
        z
        * sqrt(proportion * (1.0 - proportion) / n + z * z / (4.0 * n * n))
        / denominator
    )
    return Interval(
        proportion=proportion,
        low=max(0.0, centre - spread),
        high=min(1.0, centre + spread),
        level=level,
        n=n,
    )
