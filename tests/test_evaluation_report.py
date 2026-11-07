"""The benchmark report.

A report is not decoration here: it is the thing somebody quotes in a
paper, so what it chooses to put first and what it refuses to leave out
are the substance. These tests pin that, not the prose.
"""

from __future__ import annotations

import pytest

from slr_studio.evaluation.benchmark import BenchmarkResult
from slr_studio.evaluation.metrics import score_screening
from slr_studio.evaluation.report import _doi_url, _tidy_title, as_markdown


def _result(**overrides) -> BenchmarkResult:
    gold = [1] * 10 + [0] * 90
    decisions = (
        ["include"] * 7 + ["unclear"] * 2 + ["exclude"]          # relevant
        + ["include"] * 8 + ["unclear"] * 2 + ["exclude"] * 80   # irrelevant
    )
    base = dict(
        dataset={
            "name": "Example_2021",
            "n_records": 100,
            "n_relevant": 10,
            "prevalence": 0.1,
            "n_without_abstract": 32,
            "share_without_abstract": 0.32,
            "label_column": "label_abstract_screening",
            "source": "example.csv",
            "licence": "CC-BY 4.0",
            "reference": "https://doi.org/10.1000/example",
            "notes": [],
        },
        score=score_screening(gold=gold, decisions=decisions),
        protocol="Include controlled studies of X. Exclude animal studies.",
        model="test-model-1",
        provider="ScriptedProvider",
        started_at="2026-11-06T10:00:00+00:00",
        finished_at="2026-11-06T10:20:00+00:00",
        seconds=1200.0,
        n_ai_calls=250,
        tokens_in=100_000,
        tokens_out=20_000,
        cost=1.23,
        lost_records=[
            {
                "title": "A study the screener threw away",
                "year": 2019,
                "doi": "10.1000/lost",
                "had_abstract": False,
                "source_row": 42,
            }
        ],
        agreement={"kappa": 0.81, "n": 100},
        notes=["32% of records have no abstract."],
    )
    base.update(overrides)
    return BenchmarkResult(**base)


class TestWhatItRefusesToHide:
    def test_the_lost_records_are_named(self):
        """A count is skimmed past; a title is not."""
        text = as_markdown(_result())
        assert "A study the screener threw away" in text

    def test_a_lost_record_with_no_abstract_says_so(self):
        """Otherwise the reader blames the tool for the corpus."""
        assert "title only, no abstract" in as_markdown(_result())

    def test_the_corpus_properties_travel_with_the_score(self):
        text = as_markdown(_result())
        assert "32" in text and "no abstract" in text.lower()
        assert "label_abstract_screening" in text
        assert "CC-BY 4.0" in text

    def test_the_protocol_is_printed_in_full(self):
        """The result is a property of the protocol as much as the model."""
        text = as_markdown(_result())
        assert "Include controlled studies of X" in text

    def test_the_model_is_named(self):
        assert "test-model-1" in as_markdown(_result())

    def test_a_small_benchmark_is_flagged_as_small(self):
        """Ten relevant records cannot support a quotable point estimate."""
        text = as_markdown(_result())
        assert "should not be quoted on its own" in text

    def test_a_large_benchmark_is_not_flagged(self):
        gold = [1] * 200 + [0] * 200
        score = score_screening(
            gold=gold, decisions=["include"] * 200 + ["exclude"] * 200
        )
        text = as_markdown(_result(score=score))
        assert "should not be quoted on its own" not in text

    def test_the_limits_section_is_always_there(self):
        text = as_markdown(_result())
        assert "What this does not show" in text
        assert "one corpus in one field" in text
        assert "not ground truth" in text

    def test_recall_comes_before_specificity(self):
        """The ordering is the argument: a missed study cannot be recovered."""
        text = as_markdown(_result())
        assert text.index("Workflow recall") < text.index("Specificity")

    def test_intervals_are_shown_not_just_point_estimates(self):
        assert "95% CI" in as_markdown(_result())

    def test_no_losses_is_stated_rather_than_left_blank(self):
        score = score_screening(gold=[1, 1, 0], decisions=["include"] * 2 + ["exclude"])
        text = as_markdown(_result(score=score, lost_records=[]))
        assert "Every relevant record reached a human" in text


class TestTidyingTheData:
    @pytest.mark.parametrize(
        "raw",
        [
            "10.1007/x",
            "doi:10.1007/x",
            "https://doi.org/10.1007/x",
            "http://dx.doi.org/10.1007/x",
            "https://dx.doi.org/10.1007/x",
        ],
    )
    def test_every_doi_spelling_makes_one_working_link(self, raw):
        """Real exports hold all of these in the same column."""
        assert _doi_url(raw) == "https://doi.org/10.1007/x"

    def test_erratum_boilerplate_is_trimmed(self):
        """One real title carried 300 characters of correction notes."""
        title = (
            "Treatment with D-penicillamine affects copper metabolism"
            ".[Erratum appears in Biometals. 2014 Feb;27(1):217 Note: "
            "Grazyna, Gromadzka [corrected to Gromadzka, Grazyna]]"
        )
        tidied = _tidy_title(title)
        assert tidied.startswith("Treatment with D-penicillamine")
        assert "corrected to" not in tidied
        assert "erratum note omitted" in tidied

    def test_an_ordinary_title_is_left_alone(self):
        title = "Resident participation in tourism planning: a case study"
        assert _tidy_title(title) == title
