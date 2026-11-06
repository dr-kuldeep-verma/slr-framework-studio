"""How a graded link is drawn.

The spec fixes this exactly: thick for strong, solid for moderate, dashed for
emerging, double-headed for an undirected association, and every arrow
labelled with its link id and sign. A reader has to be able to tell the grade
from the line without consulting a key, so these are tested as rules rather
than left to the renderer.
"""

from __future__ import annotations

import pytest

from slr_studio.framework.style import (
    GRADE_ORDER,
    ArrowStyle,
    arrow_label,
    style_for,
)


class TestLineWeight:
    def test_strong_is_the_thickest(self):
        weights = {grade: style_for(grade, "positive").width for grade in GRADE_ORDER}
        assert weights["strong"] == max(weights.values())

    def test_weight_never_increases_as_evidence_weakens(self):
        widths = [style_for(grade, "positive").width for grade in GRADE_ORDER]
        assert widths == sorted(widths, reverse=True)

    def test_moderate_is_solid(self):
        assert style_for("moderate", "positive").dash == ()

    def test_emerging_is_dashed(self):
        assert style_for("emerging", "positive").dash != ()

    def test_strong_is_solid_too(self):
        """Dashing is how emerging and weaker are marked, not strong."""
        assert style_for("strong", "positive").dash == ()


class TestArrowheads:
    def test_a_direction_gets_one_arrowhead(self):
        for direction in ("positive", "negative"):
            style = style_for("strong", direction)
            assert style.heads == 1

    def test_an_undirected_association_is_double_headed(self):
        assert style_for("strong", "none").heads == 2
        assert style_for("moderate", "mixed").heads == 2

    def test_an_unclear_direction_is_not_drawn_as_a_finding(self):
        """Nothing was established, so nothing should look established."""
        style = style_for("tentative", "unclear")
        assert style.dash != ()


class TestContestedAndAsserted:
    def test_a_contested_link_is_visibly_not_a_finding(self):
        style = style_for("contested", "positive")
        assert style.dash != ()

    def test_an_asserted_link_is_the_faintest(self):
        """Nobody tested it, so it must not outweigh anything that was."""
        asserted = style_for("asserted", "positive")
        assert asserted.width == min(
            style_for(grade, "positive").width for grade in GRADE_ORDER
        )

    def test_every_grade_has_a_style(self):
        from slr_studio.synthesis import GRADES

        for grade in GRADES:
            assert isinstance(style_for(grade, "positive"), ArrowStyle)

    def test_an_unknown_grade_is_refused_rather_than_drawn_as_strong(self):
        with pytest.raises(ValueError):
            style_for("excellent", "positive")


class TestArrowLabel:
    def test_it_carries_the_link_id_and_the_sign(self):
        label = arrow_label(7, "positive")
        assert "L7" in label
        assert "+" in label

    def test_a_negative_link_reads_as_negative(self):
        assert "−" in arrow_label(7, "negative") or "-" in arrow_label(7, "negative")

    def test_an_undirected_association_carries_no_sign(self):
        label = arrow_label(7, "none")
        assert "L7" in label
        assert "+" not in label and "−" not in label

    def test_the_id_is_recognisable_as_a_link_not_a_record(self):
        """Figures are read beside tables of record ids; L disambiguates."""
        assert arrow_label(42, "positive").startswith("L42")
