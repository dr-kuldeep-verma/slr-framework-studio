"""Data extraction.

One row per report describing the study, one row per relationship it states,
each with the authors' own labels and the page the words came from. A separate
blind pass re-derives every direction from its quotation, and an optional
second coder codes a reproducible sample so inter-coder agreement can be
reported.
"""

from .engine import (
    ExtractionEngine,
    ExtractionError,
    ExtractionOutcome,
    ExtractionSettings,
)
from .schemas import (
    DirectionCheck,
    ExtractionAnswer,
    Relationship,
    ReportExtraction,
    Theory,
    instrument_for,
)
from .second_coder import SecondCoder, CoderComparison

__all__ = [
    "ExtractionEngine",
    "ExtractionSettings",
    "ExtractionOutcome",
    "ExtractionError",
    "ExtractionAnswer",
    "ReportExtraction",
    "Relationship",
    "DirectionCheck",
    "Theory",
    "instrument_for",
    "SecondCoder",
    "CoderComparison",
]
