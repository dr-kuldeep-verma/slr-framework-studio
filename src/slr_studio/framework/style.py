"""How a graded link is drawn.

The rule the spec fixes is that a reader can tell the grade from the line
without looking anything up: thick for strong, solid for moderate, dashed for
emerging, double-headed where the association has no direction. Everything
here is a pure function of the grade and the direction, so the same link looks
the same in an evidence map, in the integrated diagram and in the PowerPoint
file.

Two grades are not findings at all. `asserted` means nobody tested the claim
and `contested` means the studies disagree; both are drawn faintly and broken
so that they cannot be mistaken for evidence at a glance, which is the one way
a figure can mislead a reader who never reads the table.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GRADE_ORDER", "ArrowStyle", "arrow_label", "style_for"]

#: Strongest first. The order the line weights step down through.
GRADE_ORDER = ("strong", "moderate", "emerging", "tentative", "contested", "asserted")

#: Directions that assert no sign, and so are drawn with a head at each end.
_UNDIRECTED = frozenset({"none", "mixed", "unclear"})

#: Directions whose sign is shown on the label.
_SIGNS = {"positive": "+", "negative": "−"}


@dataclass(frozen=True)
class ArrowStyle:
    """Everything a renderer needs to draw one link.

    `width` is in points, `dash` is an on/off pattern in points -- empty for a
    solid line -- and `heads` is 1 for a direction and 2 for an association
    with no direction.
    """

    width: float
    dash: tuple[float, ...]
    heads: int
    #: Grey for the two that are not findings, black for the rest, so the eye
    #: ranks them correctly in a printed figure as well as on screen.
    colour: str = "#1a1a1a"

    @property
    def is_solid(self) -> bool:
        return not self.dash


#: Width in points and dash pattern per grade. The widths step down evenly so
#: that a reader comparing two lines sees a difference rather than guessing.
_BY_GRADE: dict[str, tuple[float, tuple[float, ...], str]] = {
    "strong":    (3.0, (),          "#1a1a1a"),
    "moderate":  (2.0, (),          "#1a1a1a"),
    "emerging":  (1.5, (6.0, 3.0),  "#1a1a1a"),
    "tentative": (1.0, (3.0, 3.0),  "#4a4a4a"),
    "contested": (0.9, (1.5, 3.0),  "#6b6b6b"),
    "asserted":  (0.75, (1.0, 4.0), "#8a8a8a"),
}


def style_for(grade: str, direction: str) -> ArrowStyle:
    """The line for one graded link.

    Raises on a grade it does not know rather than falling back to a default:
    a new grade drawn as a strong line would overstate the evidence, and
    silence is how that kind of mistake survives a release.
    """
    try:
        width, dash, colour = _BY_GRADE[grade]
    except KeyError:
        raise ValueError(
            f"No line style for grade {grade!r}. Known grades: "
            + ", ".join(GRADE_ORDER)
        ) from None
    return ArrowStyle(
        width=width,
        dash=dash,
        heads=2 if direction in _UNDIRECTED else 1,
        colour=colour,
    )


def arrow_label(link_id: int, direction: str) -> str:
    """The text on the arrow: its id, and its sign where it has one.

    The id is prefixed so that a figure read beside a table of record ids
    cannot be misread; `L7` is link seven, not record seven.
    """
    sign = _SIGNS.get(direction, "")
    return f"L{link_id} {sign}".strip()
