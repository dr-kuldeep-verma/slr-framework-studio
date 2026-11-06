"""Three-rule deduplication: DOI, title plus year, then fuzzy title."""

from .rules import (
    CandidateRecord,
    DedupSettings,
    PairVerdict,
    candidate_pairs,
    compare,
    title_similarity,
)
from .engine import DedupEngine, DedupRunResult

__all__ = [
    "CandidateRecord",
    "DedupSettings",
    "PairVerdict",
    "candidate_pairs",
    "compare",
    "title_similarity",
    "DedupEngine",
    "DedupRunResult",
]
