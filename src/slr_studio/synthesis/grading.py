"""Counting votes and grading a link.

One dataset, one vote. The unit is deliberately the dataset and not the
report: a team that publishes the same survey four times has one body of
evidence, and counting it four times would let publication strategy decide
what the review concludes.

Grades are assigned by running the rules in a fixed order of precedence and
taking the first that fits:

    asserted → contested → strong → moderate → emerging → tentative

The order is not an accident. *Asserted* comes first because a claim nobody
measured cannot be strong however many papers repeat it, and repetition of an
unevidenced claim is exactly how a field acquires a fact it never tested.
*Contested* comes next because a genuine disagreement outranks a majority:
six datasets saying yes and three saying no is a finding about disagreement,
not a strong link with some noise.

Every grade carries the reason it was given, in words, including which
condition it failed. A reviewer who disagrees can override it, and the
original is kept beside the override.

Nothing here touches the database. It takes votes and returns a grade, which
is what makes the rules testable against worked examples and the sensitivity
analyses possible at all -- they are this same function over a filtered list
of votes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .stats import Interval, SignTest, sign_test, wilson_interval

__all__ = [
    "GRADES",
    "GradedLink",
    "GradingRules",
    "Vote",
    "grade_link",
    "majority_direction",
]

#: In precedence order: the first rule that fits decides the grade.
GRADES = ("asserted", "contested", "strong", "moderate", "emerging", "tentative")

#: Directions that state which way the relationship runs. 'none' is a measured
#: null -- real evidence, but evidence about size, not sign.
_DIRECTIONAL = ("positive", "negative")


@dataclass(frozen=True)
class Vote:
    """What one dataset found about one pair of constructs."""

    dataset_id: int
    direction: str
    country: str = ""
    design_family: str = ""
    evidence_basis: str = "asserted"
    appraisal_concern: str = "unclear"
    n_reports: int = 1

    @property
    def is_directional(self) -> bool:
        return self.direction in _DIRECTIONAL


@dataclass
class GradingRules:
    """The thresholds, all configurable from the interface.

    The defaults are the spec's. They are deliberately conservative, and one
    consequence is worth stating plainly because it surprises people:

    **A link supported by exactly five datasets with none opposing cannot be
    graded strong.** The strong rule requires both "at least five datasets"
    and "a two-sided exact sign test with p < .05", and at five against
    nothing that test gives p = .0625. Six agreeing datasets is the real
    floor. The grade reason says so when it happens, rather than leaving a
    reviewer to wonder.

    Set ``strong_require_sign_test`` to False, or lower
    ``strong_min_datasets`` to a number the test can reach, if that is not
    the behaviour you want.
    """

    # -- strong ---------------------------------------------------------
    strong_min_datasets: int = 5
    strong_max_opposing: int = 0
    strong_min_agreement: float = 0.60
    strong_require_sign_test: bool = True
    strong_alpha: float = 0.05
    strong_min_countries: int = 2

    # -- moderate -------------------------------------------------------
    moderate_min_datasets: int = 3
    moderate_max_opposing: int = 1
    moderate_min_agreement: float = 0.60

    # -- emerging -------------------------------------------------------
    emerging_min_datasets: int = 2
    emerging_min_agreement: float = 0.50

    # -- contested ------------------------------------------------------
    #: Datasets on the minority side that make a link contested outright.
    contested_min_minority: int = 2
    #: Or a minority this large a share of the directional votes.
    contested_min_minority_share: float = 0.34

    # -- asserted -------------------------------------------------------
    #: A link is asserted when no dataset offers more than assertion.
    asserted_bases: tuple[str, ...] = ("asserted",)

    ci_level: float = 0.95

    def as_dict(self) -> dict[str, Any]:
        return {
            "strong_min_datasets": self.strong_min_datasets,
            "strong_max_opposing": self.strong_max_opposing,
            "strong_min_agreement": self.strong_min_agreement,
            "strong_require_sign_test": self.strong_require_sign_test,
            "strong_alpha": self.strong_alpha,
            "strong_min_countries": self.strong_min_countries,
            "moderate_min_datasets": self.moderate_min_datasets,
            "moderate_max_opposing": self.moderate_max_opposing,
            "moderate_min_agreement": self.moderate_min_agreement,
            "emerging_min_datasets": self.emerging_min_datasets,
            "emerging_min_agreement": self.emerging_min_agreement,
            "contested_min_minority": self.contested_min_minority,
            "contested_min_minority_share": self.contested_min_minority_share,
            "asserted_bases": list(self.asserted_bases),
            "ci_level": self.ci_level,
        }

    def describe(self) -> str:
        parts = [
            f"strong: at least {self.strong_min_datasets} datasets, "
            f"at most {self.strong_max_opposing} opposing, "
            f"at least {self.strong_min_agreement:.0%} agreeing",
        ]
        if self.strong_require_sign_test:
            parts.append(
                f"a two-sided exact sign test with p < {self.strong_alpha:g}"
            )
        parts.append(f"at least {self.strong_min_countries} countries")
        return "; ".join(parts) + "."

    def unreachable_note(self) -> str:
        """Whether the strong rule's own conditions can all be met at its
        stated minimum, and if not, what the real minimum is.

        Computed rather than hard-coded, so that it stays true when a
        reviewer changes the thresholds.
        """
        if not self.strong_require_sign_test:
            return ""
        minimum = self.strong_min_datasets
        if sign_test(minimum, 0).p_value < self.strong_alpha:
            return ""
        needed = minimum
        while needed < minimum + 50:
            if sign_test(needed, 0).p_value < self.strong_alpha:
                break
            needed += 1
        return (
            f"At {minimum} agreeing datasets with none opposing the two-sided "
            f"exact sign test gives p = {sign_test(minimum, 0).p_value:.4f}, "
            f"which does not reach p < {self.strong_alpha:g}. With these "
            f"settings the effective minimum for a strong grade is "
            f"{needed} agreeing datasets."
        )


@dataclass
class GradedLink:
    """A counted and graded link, with everything the count rested on."""

    direction: str
    grade: str
    grade_reason: str
    rule_applied: str
    n_datasets: int
    n_reports: int
    n_agree: int
    n_oppose: int
    n_null: int
    n_unclear: int
    countries: list[str]
    interval: Interval
    sign: SignTest
    evidence_bases: list[str] = field(default_factory=list)
    design_families: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def n_countries(self) -> int:
        return len(self.countries)

    @property
    def proportion(self) -> float:
        return self.interval.proportion

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "grade": self.grade,
            "grade_reason": self.grade_reason,
            "rule_applied": self.rule_applied,
            "n_datasets": self.n_datasets,
            "n_reports": self.n_reports,
            "n_agree": self.n_agree,
            "n_oppose": self.n_oppose,
            "n_null": self.n_null,
            "n_unclear": self.n_unclear,
            "n_countries": self.n_countries,
            "countries": list(self.countries),
            "interval": self.interval.as_dict(),
            "sign": self.sign.as_dict(),
            "evidence_bases": list(self.evidence_bases),
            "design_families": list(self.design_families),
            "failed": list(self.failed),
        }


def majority_direction(votes: Sequence[Vote]) -> str:
    """Which way the directional votes point.

    A dead heat returns 'mixed': there is no majority to report, and calling
    one side the winner on a tie-break would put a coin toss into the
    inventory.
    """
    counts = Counter(v.direction for v in votes if v.is_directional)
    if not counts:
        return "none" if any(v.direction == "none" for v in votes) else "unclear"
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return "mixed"
    return ranked[0][0]


def grade_link(
    votes: Iterable[Vote], rules: GradingRules | None = None
) -> GradedLink:
    """Count one link's votes and grade it.

    The proportion agreeing -- and so the Wilson interval -- is taken over
    every dataset that reported a usable result, nulls included. A link where
    five datasets found a positive effect and five found nothing is not 100%
    agreement, and counting it that way would turn an uncertain finding into
    a strong one.
    """
    rules = rules or GradingRules()
    votes = list(votes)

    n_datasets = len(votes)
    n_reports = sum(v.n_reports for v in votes)
    direction = majority_direction(votes)
    bases = sorted({v.evidence_basis for v in votes if v.evidence_basis})
    families = sorted({v.design_family for v in votes if v.design_family})

    if direction in _DIRECTIONAL:
        opposite = "negative" if direction == "positive" else "positive"
        n_agree = sum(1 for v in votes if v.direction == direction)
        n_oppose = sum(1 for v in votes if v.direction == opposite)
    else:
        # No majority to agree with: count each side as it falls.
        n_agree = sum(1 for v in votes if v.direction == "positive")
        n_oppose = sum(1 for v in votes if v.direction == "negative")
    n_null = sum(1 for v in votes if v.direction == "none")
    n_unclear = sum(1 for v in votes if v.direction in ("mixed", "unclear"))

    countries = sorted({v.country.strip() for v in votes if v.country.strip()})

    # Nulls count in the denominator; 'unclear' does not, because a report
    # that could not say what it found is not a vote either way.
    n_usable = n_agree + n_oppose + n_null
    interval = wilson_interval(n_agree, n_usable, level=rules.ci_level)
    sign = sign_test(n_agree, n_oppose, n_tied=n_null)

    graded = GradedLink(
        direction=direction,
        grade="tentative",
        grade_reason="",
        rule_applied="",
        n_datasets=n_datasets,
        n_reports=n_reports,
        n_agree=n_agree,
        n_oppose=n_oppose,
        n_null=n_null,
        n_unclear=n_unclear,
        countries=countries,
        interval=interval,
        sign=sign,
        evidence_bases=bases,
        design_families=families,
    )

    for rule in (_asserted, _contested, _strong, _moderate, _emerging):
        decided = rule(graded, votes, rules)
        if decided is not None:
            return decided
    return _tentative(graded, rules)


# -- the rules, in precedence order ---------------------------------------


def _asserted(
    graded: GradedLink, votes: Sequence[Vote], rules: GradingRules
) -> GradedLink | None:
    """No dataset offered more than assertion.

    First in the order on purpose: a claim repeated in twenty papers and
    measured in none is not strong evidence of anything except that the
    claim is popular.
    """
    if not votes:
        return None
    if all(v.evidence_basis in rules.asserted_bases for v in votes):
        return replace(
            graded,
            grade="asserted",
            rule_applied="asserted",
            grade_reason=(
                f"All {graded.n_datasets} dataset(s) state this link without "
                "measuring it. It is recorded so the framework can show it as "
                "an untested pathway, and it carries no evidential weight."
            ),
        )
    return None


def _contested(
    graded: GradedLink, votes: Sequence[Vote], rules: GradingRules
) -> GradedLink | None:
    """Both directions are seriously represented.

    Ahead of strong deliberately: a real split is a finding in its own right
    and gets a contradiction table, rather than being flattened into a
    majority verdict.
    """
    directional = graded.n_agree + graded.n_oppose
    if directional < 2 or graded.n_oppose == 0:
        return None
    minority = min(graded.n_agree, graded.n_oppose)
    share = minority / directional
    by_count = minority >= rules.contested_min_minority
    by_share = share >= rules.contested_min_minority_share
    if not (by_count or by_share):
        return None
    why = []
    if by_count:
        why.append(f"{minority} dataset(s) on the minority side")
    if by_share:
        why.append(f"the minority is {share:.0%} of the directional votes")
    return replace(
        graded,
        grade="contested",
        rule_applied="contested",
        grade_reason=(
            f"Datasets disagree: {graded.n_agree} found {graded.direction if graded.direction in _DIRECTIONAL else 'one direction'}"
            f" and {graded.n_oppose} the opposite ("
            + ", ".join(why)
            + "). The disagreement is explained in the contradiction table "
            "rather than resolved by majority."
        ),
    )


def _strong(
    graded: GradedLink, votes: Sequence[Vote], rules: GradingRules
) -> GradedLink | None:
    if graded.direction not in _DIRECTIONAL:
        return None
    failed: list[str] = []
    if graded.n_datasets < rules.strong_min_datasets:
        failed.append(
            f"{graded.n_datasets} dataset(s), needs {rules.strong_min_datasets}"
        )
    if graded.n_oppose > rules.strong_max_opposing:
        failed.append(
            f"{graded.n_oppose} opposing, allows {rules.strong_max_opposing}"
        )
    if graded.proportion < rules.strong_min_agreement:
        failed.append(
            f"{graded.proportion:.0%} agreeing, needs "
            f"{rules.strong_min_agreement:.0%}"
        )
    if rules.strong_require_sign_test:
        p = graded.sign.p_value
        if p is None or p >= rules.strong_alpha:
            shown = "undefined" if p is None else f"{p:.4f}"
            note = f"sign test p = {shown}, needs p < {rules.strong_alpha:g}"
            # The condition most likely to surprise: say why in place.
            if (
                p is not None
                and graded.n_oppose == 0
                and graded.n_datasets >= rules.strong_min_datasets
            ):
                note += (
                    f" — with no opposing datasets the test cannot reach that "
                    f"until {_sign_floor(rules)} agree"
                )
            failed.append(note)
    if graded.n_countries < rules.strong_min_countries:
        failed.append(
            f"{graded.n_countries} country/countries, needs "
            f"{rules.strong_min_countries}"
        )
    if failed:
        # Recorded on the link and handed to the next rule down, which quotes
        # it: a reviewer looking at a moderate grade wants to know which
        # condition it missed. Returning the link here instead would end the
        # chain and grade everything that is not strong as tentative.
        graded.failed = failed
        return None
    return replace(
        graded,
        grade="strong",
        rule_applied="strong",
        grade_reason=(
            f"{graded.n_agree} of {graded.n_datasets} datasets agree "
            f"({graded.interval.describe()}), none oppose, across "
            f"{graded.n_countries} countries, "
            f"{graded.sign.describe().lower()}."
        ),
    )


def _moderate(
    graded: GradedLink, votes: Sequence[Vote], rules: GradingRules
) -> GradedLink | None:
    if graded.direction not in _DIRECTIONAL:
        return None
    if graded.n_datasets < rules.moderate_min_datasets:
        return None
    if graded.n_oppose > rules.moderate_max_opposing:
        return None
    if graded.proportion < rules.moderate_min_agreement:
        return None
    reason = (
        f"{graded.n_agree} of {graded.n_datasets} datasets agree "
        f"({graded.interval.describe()})."
    )
    if graded.failed:
        reason += " Short of strong on: " + "; ".join(graded.failed) + "."
    return replace(graded, grade="moderate", rule_applied="moderate", grade_reason=reason)


def _emerging(
    graded: GradedLink, votes: Sequence[Vote], rules: GradingRules
) -> GradedLink | None:
    if graded.direction not in _DIRECTIONAL:
        return None
    if graded.n_datasets < rules.emerging_min_datasets:
        return None
    if graded.proportion < rules.emerging_min_agreement:
        return None
    return replace(
        graded,
        grade="emerging",
        rule_applied="emerging",
        grade_reason=(
            f"{graded.n_agree} of {graded.n_datasets} datasets agree "
            f"({graded.interval.describe()}); too few to say more."
        ),
    )


def _tentative(graded: GradedLink, rules: GradingRules) -> GradedLink:
    if graded.n_datasets == 0:
        reason = "No dataset reports this link."
    elif graded.direction == "mixed":
        reason = (
            f"The directional votes are evenly split "
            f"({graded.n_agree} against {graded.n_oppose}), so there is no "
            "majority direction to report."
        )
    elif graded.n_datasets == 1:
        reason = "A single dataset reports this link."
    else:
        reason = (
            f"{graded.n_agree} of {graded.n_datasets} datasets agree "
            f"({graded.interval.describe()}), below every higher threshold."
        )
    return replace(
        graded, grade="tentative", rule_applied="tentative", grade_reason=reason
    )


def _sign_floor(rules: GradingRules) -> int:
    """Smallest unanimous count whose two-sided p clears the threshold."""
    n = max(1, rules.strong_min_datasets)
    while n < 200:
        if sign_test(n, 0).p_value < rules.strong_alpha:
            return n
        n += 1
    return n
