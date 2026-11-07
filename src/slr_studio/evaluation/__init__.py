"""Evaluating this application against human-labelled benchmarks.

Nothing in here runs as part of the test suite against a real model, and
nothing in here is imported by the application itself. It exists to answer
the one question the 1,400 tests cannot: when this screens real abstracts
from a published review, does it keep the studies the human reviewers
kept?

The answer is not a property of the software alone -- it depends on the
model, the protocol wording and the corpus -- so a result is only ever
reported alongside all three.
"""

from .metrics import ScreeningScore, score_screening

__all__ = ["ScreeningScore", "score_screening"]
