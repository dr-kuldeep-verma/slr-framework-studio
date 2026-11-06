"""Turning extracted relationships into a graded evidence inventory.

The stage the whole review turns on. Up to here every step has been about
one report at a time; this is where reports are compared, and comparison
needs three things the extraction deliberately did not do: a shared
vocabulary, a shared sign convention, and a unit of evidence that is not the
paper.
"""

from .engine import (
    NON_VOTING_FAMILIES,
    StepOutcome,
    SynthesisEngine,
    SynthesisError,
    SynthesisSettings,
    link_key,
)
from .grading import GRADES, GradedLink, GradingRules, Vote, grade_link
from .linkage import score_shared_dataset
from .stats import Interval, SignTest, sign_test, wilson_interval

__all__ = [
    "GRADES",
    "GradedLink",
    "GradingRules",
    "Interval",
    "NON_VOTING_FAMILIES",
    "SignTest",
    "StepOutcome",
    "SynthesisEngine",
    "SynthesisError",
    "SynthesisSettings",
    "Vote",
    "grade_link",
    "link_key",
    "score_shared_dataset",
    "sign_test",
    "wilson_interval",
]
