"""Finding, evidencing and ranking the gaps in the corpus.

Two modes, and they are not the same kind of claim. The quick scan clusters
titles and abstracts before the review proper; it is a scoping view for
refining objectives, and nothing downstream may cite it. The full analysis
builds gaps from coded evidence, and every one of those carries the counts and
the studies behind it, qualified to this corpus and the date it was searched
to.

The one rule running through the whole stage: an absence is only ever an
absence *within the included corpus*. A gap statement that does not say so is
a claim about the literature, which no review is entitled to make.
"""

from .detect import Corpus, Dataset, GapCandidate, Link, Report, Statement, detect_all
from .engine import GapEngine, GapError, GapSettings, StepOutcome
from .freshness import FreshnessChecker, FreshnessResult
from .gapmap import Cell, Grid, build_grid
from .priority import Scores, Weights, rank, score
from .statements import UnsupportedGap, write_statement

__all__ = [
    "Cell",
    "Corpus",
    "Dataset",
    "FreshnessChecker",
    "FreshnessResult",
    "GapCandidate",
    "GapEngine",
    "GapError",
    "GapSettings",
    "Grid",
    "Link",
    "Report",
    "Scores",
    "Statement",
    "StepOutcome",
    "UnsupportedGap",
    "Weights",
    "build_grid",
    "detect_all",
    "rank",
    "score",
    "write_statement",
]
