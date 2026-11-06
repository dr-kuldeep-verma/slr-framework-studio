"""The grading rules and their order of precedence.

Each test builds the votes that should produce one grade and checks that it
does, including the near misses: the cases that fall one dataset, one
country or one opposing study short are where a grading rule earns its keep.
"""

from __future__ import annotations

import pytest

from slr_studio.synthesis.grading import (
    GRADES,
    GradingRules,
    Vote,
    grade_link,
    majority_direction,
)


def votes(
    spec: str,
    *,
    countries: list[str] | None = None,
    basis: str = "statistical",
    concern: str = "low",
    family: str = "survey",
) -> list[Vote]:
    """Build votes from a compact string: '+' positive, '-' negative,
    '0' a measured null, '?' unclear."""
    symbols = {"+": "positive", "-": "negative", "0": "none", "?": "unclear"}
    places = countries or ["Fiji", "Nepal", "Peru", "Kenya", "Spain", "Chile",
                           "Japan", "Ghana", "Italy", "Samoa"]
    return [
        Vote(
            dataset_id=i + 1,
            direction=symbols[ch],
            country=places[i % len(places)],
            design_family=family,
            evidence_basis=basis,
            appraisal_concern=concern,
        )
        for i, ch in enumerate(spec)
    ]


class TestPrecedence:
    def test_the_order_is_the_one_the_spec_names(self):
        assert GRADES == (
            "asserted", "contested", "strong", "moderate", "emerging", "tentative"
        )

    def test_assertion_outranks_everything(self):
        """Twenty papers repeating an unmeasured claim is not strong."""
        result = grade_link(votes("+" * 20, basis="asserted"))
        assert result.grade == "asserted"

    def test_one_measured_dataset_takes_a_link_out_of_asserted(self):
        v = votes("+" * 8, basis="asserted")
        v[3] = Vote(**{**v[3].__dict__, "evidence_basis": "statistical"})
        assert grade_link(v).grade != "asserted"

    def test_disagreement_outranks_a_majority(self):
        """Six to three is a finding about disagreement, not a strong link."""
        result = grade_link(votes("++++++---"))
        assert result.grade == "contested"

    def test_a_thematic_basis_is_not_mere_assertion(self):
        assert grade_link(votes("+++", basis="thematic")).grade != "asserted"


class TestStrong:
    def test_six_agreeing_datasets_in_two_countries_are_strong(self):
        result = grade_link(votes("++++++", countries=["Fiji", "Nepal"]))
        assert result.grade == "strong"
        assert result.sign.significant
        assert result.n_countries == 2

    def test_five_agreeing_datasets_fall_short_on_the_sign_test(self):
        """The spec's two conditions do not meet at five; the reason says so."""
        result = grade_link(votes("+++++", countries=["Fiji", "Nepal"]))
        assert result.grade == "moderate"
        assert "sign test" in result.grade_reason
        assert "until 6 agree" in result.grade_reason

    def test_one_country_is_not_enough(self):
        result = grade_link(votes("++++++", countries=["Fiji"]))
        assert result.grade == "moderate"
        assert "country" in result.grade_reason

    def test_an_opposing_dataset_blocks_strong(self):
        result = grade_link(votes("++++++++-"))
        assert result.grade != "strong"

    def test_nulls_count_against_the_agreement_share(self):
        """Five positives and five nulls is not a strong link."""
        result = grade_link(votes("+++++00000"))
        assert result.n_null == 5
        assert result.proportion == pytest.approx(0.5)
        assert result.grade != "strong"

    def test_the_reason_quotes_the_interval(self):
        result = grade_link(votes("++++++", countries=["Fiji", "Nepal"]))
        assert "95% CI" in result.grade_reason

    def test_turning_the_sign_test_off_lets_five_be_strong(self):
        rules = GradingRules(strong_require_sign_test=False)
        result = grade_link(votes("+++++", countries=["Fiji", "Nepal"]), rules)
        assert result.grade == "strong"

    def test_a_negative_link_can_be_strong_too(self):
        result = grade_link(votes("------", countries=["Fiji", "Nepal"]))
        assert result.grade == "strong" and result.direction == "negative"


class TestOtherGrades:
    def test_three_agreeing_datasets_are_moderate(self):
        assert grade_link(votes("+++")).grade == "moderate"

    def test_two_agreeing_datasets_are_emerging(self):
        assert grade_link(votes("++")).grade == "emerging"

    def test_one_dataset_is_tentative(self):
        result = grade_link(votes("+"))
        assert result.grade == "tentative"
        assert "single dataset" in result.grade_reason

    def test_no_votes_at_all_is_tentative_and_says_so(self):
        result = grade_link([])
        assert result.grade == "tentative"
        assert result.n_datasets == 0
        assert "No dataset" in result.grade_reason

    def test_an_even_split_has_no_majority_direction(self):
        result = grade_link(votes("+-"))
        assert result.direction == "mixed"
        assert result.grade in ("contested", "tentative")

    def test_a_link_only_ever_reported_null_is_not_graded_upward(self):
        result = grade_link(votes("0000"))
        assert result.direction == "none"
        assert result.grade == "tentative"
        assert result.sign.p_value is None


class TestCounting:
    def test_the_majority_direction_is_the_one_reported(self):
        assert majority_direction(votes("++++-")) == "positive"
        assert majority_direction(votes("--+")) == "negative"

    def test_a_tie_is_mixed_not_a_coin_toss(self):
        assert majority_direction(votes("++--")) == "mixed"

    def test_unclear_votes_are_counted_but_do_not_vote(self):
        result = grade_link(votes("+++???"))
        assert result.n_unclear == 3
        assert result.n_agree == 3
        # The three unclear datasets are not in the denominator.
        assert result.proportion == pytest.approx(1.0)

    def test_reports_are_counted_separately_from_datasets(self):
        v = [
            Vote(dataset_id=1, direction="positive", country="Fiji", n_reports=4),
            Vote(dataset_id=2, direction="positive", country="Nepal", n_reports=1),
        ]
        result = grade_link(v)
        assert result.n_datasets == 2 and result.n_reports == 5

    def test_countries_are_deduplicated_and_blanks_ignored(self):
        v = votes("++++", countries=["Fiji", "Fiji", "", "Nepal"])
        assert grade_link(v).countries == ["Fiji", "Nepal"]

    def test_every_link_carries_an_interval(self):
        for spec in ("+", "++", "+-", "++++++", "0000"):
            result = grade_link(votes(spec))
            assert 0.0 <= result.interval.low <= result.interval.high <= 1.0

    def test_unanimity_does_not_claim_certainty(self):
        result = grade_link(votes("++++++", countries=["Fiji", "Nepal"]))
        assert result.interval.high == 1.0
        assert result.interval.low < 1.0


class TestConfigurability:
    def test_the_thresholds_can_be_loosened(self):
        rules = GradingRules(
            strong_min_datasets=3,
            strong_min_countries=1,
            strong_require_sign_test=False,
        )
        assert grade_link(votes("+++", countries=["Fiji"]), rules).grade == "strong"

    def test_the_rules_describe_themselves(self):
        text = GradingRules().describe()
        assert "at least 5 datasets" in text and "sign test" in text

    def test_the_rules_warn_when_their_own_minimum_is_unreachable(self):
        note = GradingRules().unreachable_note()
        assert "effective minimum for a strong grade is 6" in note

    def test_consistent_settings_produce_no_warning(self):
        assert GradingRules(strong_min_datasets=6).unreachable_note() == ""
        assert GradingRules(strong_require_sign_test=False).unreachable_note() == ""

    def test_the_warning_follows_a_changed_threshold(self):
        note = GradingRules(strong_min_datasets=4, strong_alpha=0.01).unreachable_note()
        assert "effective minimum for a strong grade is 8" in note


class TestSerialisation:
    def test_a_graded_link_round_trips_to_plain_data(self):
        result = grade_link(votes("++++++", countries=["Fiji", "Nepal"])).as_dict()
        assert result["grade"] == "strong"
        assert result["interval"]["n"] == 6
        assert result["sign"]["significant"] is True

    def test_the_settings_round_trip(self):
        assert GradingRules().as_dict()["strong_min_datasets"] == 5
