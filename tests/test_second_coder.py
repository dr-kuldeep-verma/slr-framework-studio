"""The second coder, inter-coder agreement, and the extraction matrix."""

from __future__ import annotations

import pytest
from openpyxl import load_workbook

from slr_studio.ai import AIClient, MockProvider
from slr_studio.dedup import DedupEngine
from slr_studio.exports import write_extraction_matrix
from slr_studio.extraction import (
    ExtractionEngine,
    ExtractionSettings,
    Relationship,
    ReportExtraction,
    SecondCoder,
)
from slr_studio.extraction.second_coder import REPORT_FIELDS
from slr_studio.fulltext import EligibilityEngine, RetrievalService
from slr_studio.screening import ScreeningEngine, ScreeningSettings


@pytest.fixture
def extracted(imported_project, fixtures):
    """A project taken all the way through extraction."""
    DedupEngine(imported_project).run()
    imported_project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    imported_project.protocol.lock(actor="human:test")
    screening = ScreeningEngine(
        imported_project,
        AIClient(imported_project, MockProvider(), stage="screen"),
        ScreeningSettings(),
    )
    screening.waive_pilot("test fixture")
    screening.run_full(actor="human:test")

    retrieval = RetrievalService(imported_project)
    retrieval.attach_pdf(fixtures / "pdfs" / "menon_2021_kerala.pdf")
    retrieval.attach_pdf(fixtures / "pdfs" / "okonkwo_2019_benefit_sharing.pdf")
    eligibility = EligibilityEngine(
        imported_project, AIClient(imported_project, MockProvider(), stage="fulltext")
    )
    eligibility.run(actor="human:test")
    for item in eligibility.review_queue():
        eligibility.resolve(item["record_id"], "include", actor="human:test")

    ExtractionEngine(
        imported_project,
        AIClient(imported_project, MockProvider(), stage="extract"),
        ExtractionSettings(),
    ).run(actor="human:test")
    return imported_project


class TestChoosingTheSample:
    def test_the_sample_is_reproducible(self, extracted):
        first = SecondCoder(extracted).choose_sample(fraction=0.5, minimum=1)
        second = SecondCoder(extracted).choose_sample(fraction=0.5, minimum=1)
        assert first == second and first

    def test_the_minimum_applies_to_a_small_corpus(self, extracted):
        chosen = SecondCoder(extracted).choose_sample(fraction=0.01, minimum=2)
        assert len(chosen) == 2

    def test_the_sample_never_exceeds_what_has_been_coded(self, extracted):
        chosen = SecondCoder(extracted).choose_sample(fraction=1.0, minimum=50)
        coded = extracted.db.scalar(
            "SELECT COUNT(*) FROM report_extraction WHERE coder_role = 'ai'"
        )
        assert len(chosen) == coded

    def test_choosing_is_logged(self, extracted):
        SecondCoder(extracted).choose_sample(minimum=1, actor="human:test")
        assert any(
            e.action == "second_coder_sample"
            for e in extracted.audit.entries(stage="extract")
        )

    def test_the_sample_is_empty_before_anything_is_extracted(self, imported_project):
        assert SecondCoder(imported_project).choose_sample() == []


class TestComparing:
    def _code_second(self, project, record_id, **overrides):
        first = ExtractionEngine(
            project, AIClient(project, MockProvider(), stage="extract")
        ).extraction_for(record_id)
        payload = {
            "country": first["country"],
            "design_family": first["design_family"],
            "appraisal_instrument": first["appraisal_instrument"],
            "appraisal_concern": first["appraisal_concern"],
            "sample_size": first["sample_size"],
            "time_horizon": first["time_horizon"],
        }
        payload.update(overrides)
        SecondCoder(project).record_report(
            record_id, ReportExtraction(**payload), coder="human:second"
        )

    def test_nothing_to_compare_is_said_plainly(self, extracted):
        comparison = SecondCoder(extracted).compare()
        assert comparison.n_reports_compared == 0
        assert "nothing to compare" in comparison.note
        assert "No report has been coded twice" in comparison.describe()

    def test_identical_coding_gives_perfect_agreement(self, extracted):
        second = SecondCoder(extracted)
        sample = second.choose_sample(fraction=1.0, minimum=1)
        for record_id in sample:
            self._code_second(extracted, record_id)
        comparison = second.compare()
        assert comparison.n_reports_compared == len(sample)
        for name in REPORT_FIELDS:
            assert comparison.report_fields[name].percent_agreement == 1.0

    def test_a_disagreement_shows_in_that_field_only(self, extracted):
        second = SecondCoder(extracted)
        sample = second.choose_sample(fraction=1.0, minimum=1)
        for index, record_id in enumerate(sample):
            overrides = {"design_family": "qualitative"} if index == 0 else {}
            self._code_second(extracted, record_id, **overrides)
        comparison = second.compare()
        assert comparison.report_fields["design_family"].percent_agreement < 1.0
        assert comparison.report_fields["country"].percent_agreement == 1.0

    def test_agreement_is_reported_field_by_field(self, extracted):
        second = SecondCoder(extracted)
        for record_id in second.choose_sample(fraction=1.0, minimum=1):
            self._code_second(extracted, record_id)
        comparison = second.compare()
        assert set(comparison.report_fields) == set(REPORT_FIELDS)
        assert "design_family" in comparison.describe()

    def test_relationships_are_paired_on_the_authors_labels(self, extracted):
        second = SecondCoder(extracted)
        record_id = second.choose_sample(fraction=1.0, minimum=1)[0]
        engine = ExtractionEngine(
            extracted, AIClient(extracted, MockProvider(), stage="extract")
        )
        first = engine.relationships_for(record_id)
        relationships = [
            Relationship(
                predictor_label=row["predictor_label"],
                outcome_label=row["outcome_label"],
                direction="negative",   # the second coder reads it the other way
            )
            for row in first
        ]
        SecondCoder(extracted).record_report(
            record_id,
            ReportExtraction(**{"design_family": "cross_sectional"}),
            coder="human:second",
            relationships=relationships,
        )
        comparison = SecondCoder(extracted).compare()
        distinct_pairs = len({
            (row["predictor_label"].casefold(), row["outcome_label"].casefold())
            for row in first
        })
        assert comparison.n_relationships_matched == distinct_pairs
        assert comparison.relationship_fields["direction"].percent_agreement == 0.0

    def test_a_repeated_pair_of_labels_is_counted_and_reported(self, extracted):
        second = SecondCoder(extracted)
        record_id = second.choose_sample(fraction=1.0, minimum=1)[0]
        engine = ExtractionEngine(
            extracted, AIClient(extracted, MockProvider(), stage="extract")
        )
        first = engine.relationships_for(record_id)
        repeated = len(first) - len({
            (row["predictor_label"].casefold(), row["outcome_label"].casefold())
            for row in first
        })
        second.record_report(
            record_id,
            ReportExtraction(design_family="cross_sectional"),
            coder="human:second",
            relationships=[
                Relationship(
                    predictor_label=row["predictor_label"],
                    outcome_label=row["outcome_label"],
                )
                for row in first
            ],
        )
        comparison = SecondCoder(extracted).compare()
        # Repeats on both sides, so they are counted twice over.
        assert comparison.n_duplicate_pairs == repeated * 2
        if repeated:
            assert "repeat a pair of labels" in comparison.describe()

    def test_relationships_only_one_coder_found_are_counted_separately(self, extracted):
        second = SecondCoder(extracted)
        record_id = second.choose_sample(fraction=1.0, minimum=1)[0]
        second.record_report(
            record_id,
            ReportExtraction(design_family="cross_sectional"),
            coder="human:second",
            relationships=[
                Relationship(predictor_label="something", outcome_label="else")
            ],
        )
        comparison = SecondCoder(extracted).compare()
        assert comparison.n_only_second >= 1

    def test_the_comparison_is_stored_and_logged(self, extracted):
        second = SecondCoder(extracted)
        for record_id in second.choose_sample(fraction=1.0, minimum=1):
            self._code_second(extracted, record_id)
        second.compare(actor="human:test")
        rows = second.latest()
        assert rows and {r["scope"] for r in rows} >= {
            f"report:{name}" for name in REPORT_FIELDS
        }
        assert any(
            e.action == "coder_agreement" for e in extracted.audit.entries()
        )
        assert extracted.audit.verify() == []

    def test_a_second_coding_does_not_overwrite_the_first(self, extracted):
        second = SecondCoder(extracted)
        record_id = second.choose_sample(fraction=1.0, minimum=1)[0]
        before = ExtractionEngine(
            extracted, AIClient(extracted, MockProvider(), stage="extract")
        ).extraction_for(record_id)
        self._code_second(extracted, record_id, design_family="qualitative")
        after = ExtractionEngine(
            extracted, AIClient(extracted, MockProvider(), stage="extract")
        ).extraction_for(record_id)
        assert after["design_family"] == before["design_family"]
        assert extracted.db.scalar(
            "SELECT design_family FROM report_extraction WHERE record_id = ? "
            "AND coder_role = 'second_coder'",
            (record_id,),
        ) == "qualitative"

    def test_coding_is_marked_as_done_in_the_sample(self, extracted):
        second = SecondCoder(extracted)
        record_id = second.choose_sample(fraction=1.0, minimum=1)[0]
        self._code_second(extracted, record_id)
        row = next(r for r in second.sample() if r["record_id"] == record_id)
        assert row["coded_at"] is not None and row["coded"] == 1


class TestExtractionMatrix:
    @pytest.fixture
    def workbook(self, extracted):
        return load_workbook(write_extraction_matrix(extracted))

    def test_the_four_sheets_are_present(self, workbook):
        assert workbook.sheetnames == [
            "Studies", "Relationships", "Checks", "Inter-coder agreement"
        ]

    def test_one_row_per_extracted_study(self, workbook, extracted):
        expected = extracted.db.scalar(
            "SELECT COUNT(*) FROM report_extraction WHERE coder_role = 'ai'"
        )
        assert workbook["Studies"].max_row == expected + 1

    def test_one_row_per_relationship(self, workbook, extracted):
        expected = extracted.db.scalar(
            "SELECT COUNT(*) FROM relationship WHERE coder_role = 'ai'"
        )
        assert workbook["Relationships"].max_row == expected + 1

    def test_the_relationships_sheet_carries_the_check(self, workbook):
        headers = [cell.value for cell in workbook["Relationships"][1]]
        for name in ("Direction", "Direction re-derived", "Check agrees",
                     "Quotation", "Page", "Quotation found"):
            assert name in headers

    def test_the_labels_are_the_authors_own(self, workbook, extracted):
        sheet = workbook["Relationships"]
        headers = [cell.value for cell in sheet[1]]
        column = headers.index("Predictor (authors' label)") + 1
        stored = {
            r["predictor_label"]
            for r in extracted.db.query(
                "SELECT predictor_label FROM relationship WHERE coder_role = 'ai'"
            )
        }
        written = {
            sheet.cell(row=r, column=column).value
            for r in range(2, sheet.max_row + 1)
        }
        assert written == stored

    def test_the_experiment_rule_is_visible_in_the_studies_sheet(self, workbook):
        headers = [cell.value for cell in workbook["Studies"][1]]
        for name in ("Manipulated treatment", "Control group", "Design downgraded",
                     "Instrument fits design"):
            assert name in headers

    def test_an_empty_agreement_sheet_says_why(self, workbook):
        rows = [
            [cell.value for cell in row]
            for row in workbook["Inter-coder agreement"].iter_rows()
        ]
        assert any(
            row[0] and "second coder" in str(row[0]) for row in rows[1:]
        )

    def test_the_agreement_sheet_fills_once_a_second_coder_has_coded(self, extracted):
        second = SecondCoder(extracted)
        record_id = second.choose_sample(fraction=1.0, minimum=1)[0]
        second.record_report(
            record_id, ReportExtraction(design_family="cross_sectional"),
            coder="human:second",
        )
        second.compare()
        sheet = load_workbook(write_extraction_matrix(extracted))["Inter-coder agreement"]
        scopes = {sheet.cell(row=r, column=1).value for r in range(2, sheet.max_row + 1)}
        assert "report:design_family" in scopes

    def test_writing_the_matrix_is_logged(self, extracted):
        write_extraction_matrix(extracted)
        assert any(
            e.action == "extraction_matrix_exported"
            for e in extracted.audit.entries(stage="extract")
        )
        assert extracted.audit.verify() == []

    def test_it_lands_in_the_exports_folder_by_default(self, extracted):
        path = write_extraction_matrix(extracted)
        assert path.parent == extracted.exports_dir
        assert path.name == "extraction-matrix.xlsx"
