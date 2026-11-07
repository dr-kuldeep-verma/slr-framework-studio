"""Metrics for screening against a human-labelled gold standard.

Every number here is checked against one worked out independently in the
test, not against whatever the implementation happens to return. A metrics
module that agrees with itself is worth nothing: these figures are the ones
a paper would report and a reviewer would challenge.

The framing matters as much as the arithmetic. This application's workflow
is an AI first pass that a human confirms, so there are two different
recalls and conflating them would flatter the tool:

*Strict recall* -- of the relevant records, how many did the AI call
`include` on its own.

*Workflow recall* -- of the relevant records, how many survived to a human,
which is `include` plus `unclear` plus anything queued for review. This is
the number that matters in use, because only a record the AI excluded
outright is actually lost.
"""

from __future__ import annotations

import math

import pytest

from slr_studio.evaluation.metrics import (
    ScreeningScore,
    score_screening,
)


def _labels(relevant: int, irrelevant: int):
    """`relevant` true positives first, then `irrelevant` true negatives."""
    return [1] * relevant + [0] * irrelevant


class TestTheTwoRecalls:
    def test_strict_recall_counts_only_include(self):
        #     gold:      1  1  1  1
        #     decision:  in in un ex   -> strict 2/4
        score = score_screening(
            gold=[1, 1, 1, 1],
            decisions=["include", "include", "unclear", "exclude"],
        )
        assert score.strict_recall == 0.5

    def test_workflow_recall_counts_anything_a_human_still_sees(self):
        """`unclear` is not a miss: it goes to the human queue."""
        score = score_screening(
            gold=[1, 1, 1, 1],
            decisions=["include", "include", "unclear", "exclude"],
        )
        assert score.workflow_recall == 0.75

    def test_a_record_excluded_outright_is_the_only_real_loss(self):
        score = score_screening(
            gold=[1, 1, 1, 1],
            decisions=["include", "include", "unclear", "exclude"],
        )
        assert score.n_lost == 1
        assert score.lost_indices == [3]

    def test_perfect_screening(self):
        score = score_screening(
            gold=[1, 1, 0, 0], decisions=["include", "include", "exclude", "exclude"]
        )
        assert score.strict_recall == 1.0
        assert score.specificity == 1.0
        assert score.n_lost == 0

    def test_the_worst_case_is_reported_as_such(self):
        """Everything excluded: no gaps, no findings, nothing found."""
        score = score_screening(gold=[1, 1, 0, 0], decisions=["exclude"] * 4)
        assert score.strict_recall == 0.0
        assert score.workflow_recall == 0.0
        assert score.n_lost == 2
        assert score.specificity == 1.0  # perfectly specific and useless


class TestSpecificityAndBurden:
    def test_specificity_is_over_the_irrelevant_records(self):
        #  gold:      0  0  0  0
        #  decision:  ex ex ex in   -> 3/4 correctly rejected
        score = score_screening(
            gold=[0, 0, 0, 0],
            decisions=["exclude", "exclude", "exclude", "include"],
        )
        assert score.specificity == 0.75

    def test_burden_is_what_a_human_still_has_to_read(self):
        """The reason to use the tool at all, so it is measured, not assumed."""
        score = score_screening(
            gold=[1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            decisions=["include"] + ["exclude"] * 7 + ["unclear"] * 2,
        )
        # 1 include + 2 unclear = 3 of 10 reach a human.
        assert score.burden == 0.3
        assert score.n_to_human == 3

    def test_screening_nothing_out_is_no_saving(self):
        score = score_screening(gold=_labels(2, 8), decisions=["include"] * 10)
        assert score.burden == 1.0
        assert score.specificity == 0.0


class TestPrecisionAndYield:
    def test_precision_over_what_reached_a_human(self):
        #  2 of the 5 sent to a human are truly relevant
        score = score_screening(
            gold=[1, 1, 0, 0, 0, 0, 0, 0],
            decisions=["include"] * 5 + ["exclude"] * 3,
        )
        assert score.precision == pytest.approx(2 / 5)

    def test_number_needed_to_read(self):
        """How many abstracts a human reads per relevant study found."""
        score = score_screening(
            gold=[1, 1, 0, 0, 0, 0, 0, 0],
            decisions=["include"] * 5 + ["exclude"] * 3,
        )
        assert score.number_needed_to_read == pytest.approx(2.5)

    def test_number_needed_to_read_is_undefined_with_no_finds(self):
        """Not zero, and not infinity dressed up as a number."""
        score = score_screening(gold=[1, 0, 0], decisions=["exclude"] * 3)
        assert score.number_needed_to_read is None


class TestTheIntervals:
    def test_recall_carries_a_wilson_interval(self):
        """A recall of 1.0 on 4 records is not evidence of a perfect tool.

        The interval is what stops a tiny benchmark reading as a strong
        result, which is the most common way these numbers mislead.
        """
        score = score_screening(gold=_labels(4, 0), decisions=["include"] * 4)
        assert score.strict_recall == 1.0
        low, high = score.strict_recall_interval
        assert high == pytest.approx(1.0, abs=1e-9)
        assert low < 0.6, (
            f"4 for 4 should not imply recall above {low:.2f}; a benchmark "
            "this small cannot support it"
        )

    def test_a_bigger_sample_narrows_it(self):
        small = score_screening(gold=_labels(10, 0), decisions=["include"] * 10)
        large = score_screening(gold=_labels(400, 0), decisions=["include"] * 400)
        assert large.strict_recall_interval[0] > small.strict_recall_interval[0]

    def test_the_interval_brackets_the_estimate(self):
        score = score_screening(
            gold=_labels(30, 70),
            decisions=["include"] * 25 + ["exclude"] * 5 + ["exclude"] * 70,
        )
        low, high = score.strict_recall_interval
        assert low <= score.strict_recall <= high


class TestItRefusesToMislead:
    def test_mismatched_lengths_are_refused(self):
        with pytest.raises(ValueError, match="same length"):
            score_screening(gold=[1, 0, 1], decisions=["include"])

    def test_an_unknown_decision_is_refused(self):
        """Silently bucketing an unexpected value would corrupt every figure."""
        with pytest.raises(ValueError, match="maybe"):
            score_screening(gold=[1], decisions=["maybe"])

    def test_no_relevant_records_means_no_recall_not_zero_recall(self):
        """Zero would read as total failure; there was nothing to find."""
        score = score_screening(gold=[0, 0, 0], decisions=["exclude"] * 3)
        assert score.strict_recall is None
        assert score.workflow_recall is None
        assert score.specificity == 1.0

    def test_an_empty_benchmark_scores_nothing(self):
        score = score_screening(gold=[], decisions=[])
        assert score.strict_recall is None
        assert score.specificity is None
        assert score.n_lost == 0


class TestAgainstIndependentlyComputedFigures:
    """One worked example, computed by hand in the test.

    20 relevant and 80 irrelevant. The AI includes 14 relevant and 10
    irrelevant, is unclear on 3 relevant and 5 irrelevant, and excludes the
    rest.
    """

    def _score(self) -> ScreeningScore:
        gold = _labels(20, 80)
        decisions = (
            ["include"] * 14 + ["unclear"] * 3 + ["exclude"] * 3      # relevant
            + ["include"] * 10 + ["unclear"] * 5 + ["exclude"] * 65   # irrelevant
        )
        return score_screening(gold=gold, decisions=decisions)

    def test_every_figure(self):
        s = self._score()
        assert s.n_relevant == 20
        assert s.n_records == 100
        assert s.strict_recall == pytest.approx(14 / 20)
        assert s.workflow_recall == pytest.approx(17 / 20)
        assert s.n_lost == 3
        # Specificity over the 80 irrelevant: 65 correctly excluded.
        assert s.specificity == pytest.approx(65 / 80)
        # 17 relevant + 15 irrelevant = 32 reach a human.
        assert s.n_to_human == 32
        assert s.burden == pytest.approx(0.32)
        assert s.precision == pytest.approx(17 / 32)
        assert s.number_needed_to_read == pytest.approx(32 / 17)

    def test_the_wilson_interval_matches_the_formula(self):
        """Checked against the closed form written out here, not the code."""
        s = self._score()
        p, n, z = 14 / 20, 20, 1.959963984540054
        centre = (p + z * z / (2 * n)) / (1 + z * z / n)
        spread = (
            z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
        )
        assert s.strict_recall_interval[0] == pytest.approx(centre - spread, abs=1e-9)
        assert s.strict_recall_interval[1] == pytest.approx(centre + spread, abs=1e-9)
