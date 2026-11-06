"""Turning the graded inventory into figures and propositions.

Nothing here may introduce evidence. Every arrow in every figure and every
link cited by every proposition comes from the graded inventory the synthesis
produced; this stage arranges and explains what is already there, and refuses
anything that is not.
"""

from .engine import FrameworkEngine, FrameworkError, FrameworkSettings, StepOutcome
from .figures import to_svg, write_figures
from .layout import Edge, Layout, Node, count_crossings, layout_graph, segment_hits_box
from .style import GRADE_ORDER, ArrowStyle, arrow_label, style_for

__all__ = [
    "ArrowStyle",
    "Edge",
    "FrameworkEngine",
    "FrameworkError",
    "FrameworkSettings",
    "GRADE_ORDER",
    "Layout",
    "Node",
    "StepOutcome",
    "arrow_label",
    "count_crossings",
    "layout_graph",
    "segment_hits_box",
    "style_for",
    "to_svg",
    "write_figures",
]
