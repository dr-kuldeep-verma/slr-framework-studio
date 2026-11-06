"""The PRISMA 2020 flow diagram and the checklist.

The rule for the diagram is the one the whole application is built around:
every number in it is read from the database, and none of it can be typed.
A reviewer who could edit a box could publish a flow diagram that disagrees
with their own data, and the diagram is the first thing a referee checks.

The checklist is different in kind. Twenty-seven items, of which the
application can answer some from what it recorded and cannot answer the rest.
Those are marked as the reviewer's to complete rather than guessed at, because
a checklist that claims an item was done when nobody checked is worse than a
blank one.
"""

from __future__ import annotations

import pytest

from slr_studio.exports.prisma import (
    CHECKLIST_ITEMS,
    PrismaCounts,
    read_counts,
    write_prisma_checklist,
    write_prisma_diagram,
)


@pytest.fixture
def reviewed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", extract=True)
    yield project
    project.close()


class TestTheCountsComeFromTheDatabase:
    def test_the_counts_match_the_views(self, reviewed):
        counts = read_counts(reviewed)
        identification = reviewed.db.query_one(
            "SELECT * FROM v_prisma_identification"
        )
        assert counts.n_identified == identification["n_identified"]
        assert counts.n_duplicates_removed == (
            identification["n_duplicates_removed"]
        )

    def test_the_arithmetic_of_the_flow_holds(self, reviewed):
        """Each stage has to account for everything the one above passed on."""
        counts = read_counts(reviewed)
        assert counts.n_screened == (
            counts.n_identified - counts.n_duplicates_removed
        )
        assert counts.n_screened == (
            counts.n_excluded_screening
            + counts.n_sought
            + counts.n_not_yet_screened
        )
        assert counts.n_sought == counts.n_retrieved + counts.n_not_retrieved
        assert counts.n_assessed == (
            counts.n_included + counts.n_excluded_fulltext
            + counts.n_fulltext_undecided
        )

    def test_nothing_in_the_diagram_can_be_typed(self):
        """There is no argument that lets a caller supply a number."""
        import inspect

        signature = inspect.signature(write_prisma_diagram)
        assert set(signature.parameters) <= {"project", "path", "fmt"}

    def test_the_counts_object_is_frozen(self):
        counts = PrismaCounts()
        with pytest.raises(Exception):
            counts.n_identified = 99


class TestTheDiagram:
    def test_a_word_file_is_written(self, reviewed):
        path = write_prisma_diagram(
            reviewed, reviewed.exports_dir / "prisma.docx"
        )
        assert path.is_file() and path.stat().st_size > 5_000

    def test_every_count_appears_in_the_word_file(self, reviewed):
        from docx import Document

        path = write_prisma_diagram(
            reviewed, reviewed.exports_dir / "prisma.docx"
        )
        document = Document(str(path))
        text = "\n".join(p.text for p in document.paragraphs)
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    text += "\n" + cell.text
        counts = read_counts(reviewed)
        for value in (
            counts.n_identified, counts.n_duplicates_removed,
            counts.n_screened, counts.n_included,
        ):
            assert str(value) in text

    def test_a_pdf_is_written(self, reviewed):
        path = write_prisma_diagram(
            reviewed, reviewed.exports_dir / "prisma.pdf", fmt="pdf"
        )
        assert path.is_file()
        assert path.read_bytes().startswith(b"%PDF")

    def test_the_databases_searched_are_named(self, reviewed):
        from docx import Document

        path = write_prisma_diagram(
            reviewed, reviewed.exports_dir / "prisma.docx"
        )
        text = "\n".join(
            cell.text
            for table in Document(str(path)).tables
            for row in table.rows
            for cell in row.cells
        )
        assert "Scopus" in text

    def test_the_exclusion_reasons_are_broken_out(self, reviewed):
        """PRISMA 2020 asks for full-text exclusions with reasons."""
        from docx import Document

        path = write_prisma_diagram(
            reviewed, reviewed.exports_dir / "prisma.docx"
        )
        text = "\n".join(
            cell.text
            for table in Document(str(path)).tables
            for row in table.rows
            for cell in row.cells
        ).casefold()
        assert "reason" in text

    def test_an_unfinished_review_says_so_rather_than_showing_zero(
        self, tmp_path
    ):
        """Nought included and not yet screened are not the same thing."""
        from docx import Document

        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "early")
        try:
            path = write_prisma_diagram(
                project, project.exports_dir / "prisma.docx"
            )
            text = "\n".join(
                p.text for p in Document(str(path)).paragraphs
            ).casefold()
            assert "not finished" in text or "not yet" in text
        finally:
            project.close()


class TestTheChecklist:
    def test_all_twenty_seven_items_are_present(self):
        numbers = {item.number for item in CHECKLIST_ITEMS}
        assert len(CHECKLIST_ITEMS) >= 27
        assert "1" in numbers and "27" in numbers

    def test_it_is_written_as_a_word_table(self, reviewed):
        from docx import Document

        path = write_prisma_checklist(
            reviewed, reviewed.exports_dir / "checklist.docx"
        )
        document = Document(str(path))
        assert document.tables
        assert len(document.tables[0].rows) == len(CHECKLIST_ITEMS) + 1

    def test_items_the_project_can_answer_are_pre_filled(self, reviewed):
        from docx import Document

        path = write_prisma_checklist(
            reviewed, reviewed.exports_dir / "checklist.docx"
        )
        rows = Document(str(path)).tables[0].rows
        filled = [
            row for row in rows[1:] if row.cells[2].text.strip()
            and "reviewer" not in row.cells[2].text.casefold()
        ]
        assert filled

    def test_items_it_cannot_answer_say_so_rather_than_claiming_compliance(
        self, reviewed
    ):
        from docx import Document

        path = write_prisma_checklist(
            reviewed, reviewed.exports_dir / "checklist.docx"
        )
        rows = Document(str(path)).tables[0].rows
        unanswered = [
            row for row in rows[1:]
            if "reviewer" in row.cells[2].text.casefold()
        ]
        assert unanswered, "no item is left for the reviewer, which cannot be right"
        for row in unanswered:
            assert "yes" != row.cells[2].text.strip().casefold()

    def test_the_search_date_is_carried_through_when_it_is_known(self, reviewed):
        from docx import Document

        reviewed.update_info(searched_to="2026-03-01")
        path = write_prisma_checklist(
            reviewed, reviewed.exports_dir / "checklist.docx"
        )
        text = "\n".join(
            cell.text
            for row in Document(str(path)).tables[0].rows
            for cell in row.cells
        )
        assert "2026-03-01" in text
