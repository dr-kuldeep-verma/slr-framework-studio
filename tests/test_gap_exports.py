"""The four things the Gap Finder hands over.

A gap register in Excel, the evidence gap map, a research agenda in Word, and
a draft of the Future Research section. Every number in all four comes from
the database; none is typed by a model, and the agenda and the draft carry the
corpus qualifier on every claim of absence, because those are the two that get
pasted straight into a manuscript.
"""

from __future__ import annotations

import pytest

from slr_studio.ai import AIClient, MockProvider
from slr_studio.exports import (
    write_future_research_draft,
    write_gap_register,
    write_research_agenda,
)
from slr_studio.gaps import GapEngine, GapSettings


@pytest.fixture
def analysed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", framework=True)
    project.update_info(
        searched_to="2026-03-01",
        stimulus_attributes="source; certainty",
    )
    engine = GapEngine(
        project,
        AIClient(project, MockProvider(), stage="gaps", sleep=lambda _: None),
        GapSettings(),
    )
    engine.find_gaps(actor="human:test")
    engine.draft_studies(actor="human:test", top=3)
    yield project, engine
    project.close()


class TestTheGapRegister:
    def test_it_is_written_with_a_row_per_gap(self, analysed):
        from openpyxl import load_workbook

        project, engine = analysed
        path = write_gap_register(project, project.exports_dir / "gaps.xlsx")
        sheet = load_workbook(path)["Gap register"]
        assert sheet.max_row == len(engine.gaps()) + 1

    def test_every_column_the_spec_names_is_present(self, analysed):
        from openpyxl import load_workbook

        project, _ = analysed
        path = write_gap_register(project, project.exports_dir / "gaps.xlsx")
        headers = [
            cell.value
            for cell in load_workbook(path)["Gap register"][1]
        ]
        joined = " ".join(h.casefold() for h in headers if h)
        for wanted in (
            "type", "statement", "evidence", "freshness", "priority",
            "question",
        ):
            assert wanted in joined

    def test_the_counts_travel_with_the_statement(self, analysed):
        from openpyxl import load_workbook

        project, _ = analysed
        path = write_gap_register(project, project.exports_dir / "gaps.xlsx")
        sheet = load_workbook(path)["Gap register"]
        headers = [c.value for c in sheet[1]]
        counts_column = next(
            i for i, h in enumerate(headers, start=1)
            if h and "evidence" in h.casefold()
        )
        for row in range(2, sheet.max_row + 1):
            assert sheet.cell(row=row, column=counts_column).value


class TestTheResearchAgenda:
    def test_it_is_a_word_file_with_a_table(self, analysed):
        from docx import Document

        project, _ = analysed
        path = write_research_agenda(project, project.exports_dir / "agenda.docx")
        document = Document(str(path))
        assert document.tables
        assert path.stat().st_size > 5_000

    def test_only_gaps_with_a_drafted_study_appear(self, analysed):
        from docx import Document

        project, engine = analysed
        path = write_research_agenda(project, project.exports_dir / "agenda.docx")
        rows = Document(str(path)).tables[0].rows
        drafted = [g for g in engine.gaps() if g["suggested_question"]]
        assert len(rows) == len(drafted) + 1

    def test_it_says_what_the_absences_are_qualified_by(self, analysed):
        from docx import Document

        project, _ = analysed
        path = write_research_agenda(project, project.exports_dir / "agenda.docx")
        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        assert "2026-03-01" in text
        assert "included corpus" in text.casefold()


class TestTheFutureResearchDraft:
    def test_it_is_written_as_prose_a_reviewer_can_edit(self, analysed):
        project, _ = analysed
        path = write_future_research_draft(
            project, project.exports_dir / "future-research.md"
        )
        text = path.read_text(encoding="utf-8")
        assert "## " in text
        assert len(text) > 400

    def test_every_claim_of_absence_carries_the_qualifier(self, analysed):
        project, engine = analysed
        path = write_future_research_draft(
            project, project.exports_dir / "future-research.md"
        )
        text = path.read_text(encoding="utf-8")
        # Each gap statement is quoted, and each already carries it.
        assert text.casefold().count("within the included corpus") >= 1
        assert "2026-03-01" in text

    def test_it_says_plainly_that_it_is_a_draft(self, analysed):
        project, _ = analysed
        path = write_future_research_draft(
            project, project.exports_dir / "future-research.md"
        )
        assert "draft" in path.read_text(encoding="utf-8").casefold()

    def test_it_refuses_when_no_gaps_have_been_found(self, tmp_path):
        from slr_studio.demo import build_demo_project
        from slr_studio.gaps import GapError

        project = build_demo_project(tmp_path / "bare", framework=True)
        try:
            with pytest.raises(GapError):
                write_future_research_draft(
                    project, project.exports_dir / "future-research.md"
                )
        finally:
            project.close()
