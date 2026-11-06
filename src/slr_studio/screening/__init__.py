"""Title and abstract screening.

Two AI screeners judge each record from the protocol alone and without seeing
each other's answer, a third call adjudicates their disagreements, a
quality-control pass re-reads a reproducible sample of the records both
excluded, and a human settles everything that is left. Agreement is reported
before the reviewer is allowed to trust any of it.
"""

from .engine import ScreeningEngine, ScreeningSettings, RunOutcome
from .schemas import AdjudicationAnswer, ScreeningAnswer
from .stats import AgreementResult, agreement, cohens_kappa, pabak

__all__ = [
    "ScreeningEngine",
    "ScreeningSettings",
    "RunOutcome",
    "ScreeningAnswer",
    "AdjudicationAnswer",
    "AgreementResult",
    "agreement",
    "cohens_kappa",
    "pabak",
]
