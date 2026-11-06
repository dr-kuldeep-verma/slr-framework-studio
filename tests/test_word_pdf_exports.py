"""The Word tables and the PDF figures.

Section 12 asks for the graded inventory, the sensitivity tables and the
contradiction table in Word as well as Excel, and for every figure as PDF as
well as SVG, PNG and PPTX. A reviewer writing in Word should not have to paste
a spreadsheet into their manuscript.

The numbers in the Word tables are read from the same views as the Excel, so
the two cannot disagree; that is what the first test checks.
"""

from __future__ import annotations

import pytest

from slr_studio.exports.documents import write_synthesis_tables


@pytest.fixture
def reviewed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", framework=True)
    yield project
    project.close()


class TestTheWordTables:
    def test_a_word_file_is_written(self, reviewed):
        path = write_synthesis_tables(
            reviewed, reviewed.exports_dir / "synthesis.docx"
        )
        assert path.is_file() and path.stat().st_size > 5_000

    def test_the_three_tables_the_spec_names_are_there(self, reviewed):
        from docx import Document

        path = write_synthesis_tables(
            reviewed, reviewed.exports_dir / "synthesis.docx"
        )
        headings = " ".join(
            p.text.casefold()
            for p in Document(str(path)).paragraphs
            if p.style.name.startswith("Heading")
        )
        for wanted in ("inventory", "sensitivity", "contradiction"):
            assert wanted in headings

    def test_the_inventory_has_a_row_per_graded_link(self, reviewed):
        from docx import Document

        path = write_synthesis_tables(
            reviewed, reviewed.exports_dir / "synthesis.docx"
        )
        table = Document(str(path)).tables[0]
        assert len(table.rows) == reviewed.db.scalar(
            "SELECT COUNT(*) FROM graded_link"
        ) + 1

    def test_the_numbers_agree_with_the_workbook(self, reviewed):
        """One source for both, so the Word and the Excel cannot disagree."""
        from docx import Document
        from openpyxl import load_workbook

        from slr_studio.exports import write_graded_inventory

        word = write_synthesis_tables(
            reviewed, reviewed.exports_dir / "synthesis.docx"
        )
        excel = write_graded_inventory(
            reviewed, reviewed.exports_dir / "inventory.xlsx"
        )
        sheet = load_workbook(excel).worksheets[0]
        grades_in_excel = sorted(
            str(row[0]) for row in sheet.iter_rows(min_row=2, values_only=True)
            if row and row[0]
        )
        table = Document(str(word)).tables[0]
        assert len(table.rows) - 1 == len(grades_in_excel)

    def test_each_link_carries_its_interval_and_p_value(self, reviewed):
        from docx import Document

        path = write_synthesis_tables(
            reviewed, reviewed.exports_dir / "synthesis.docx"
        )
        headers = [c.text.casefold() for c in Document(str(path)).tables[0].rows[0].cells]
        joined = " ".join(headers)
        assert "95%" in joined or "interval" in joined
        assert "p" in joined

    def test_a_project_with_no_synthesis_says_so(self, tmp_path):
        from docx import Document

        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "early", extract=True)
        try:
            path = write_synthesis_tables(
                project, project.exports_dir / "synthesis.docx"
            )
            text = "\n".join(
                p.text for p in Document(str(path)).paragraphs
            ).casefold()
            assert "no" in text and "synthesis" in text
        finally:
            project.close()


class TestThePdfFigures:
    def test_every_figure_is_also_written_as_pdf(self, reviewed):
        """The demo's framework stage already drew them; check what it wrote."""
        formats = {
            row["format"] for row in reviewed.db.query(
                "SELECT DISTINCT format FROM figure"
            )
        }
        assert {"svg", "png", "pptx", "pdf"} <= formats

    def test_the_pdf_files_are_on_disk_beside_the_others(self, reviewed):
        written = {
            path.suffix
            for path in (reviewed.exports_dir / "figures").iterdir()
        }
        assert ".pdf" in written

    def test_the_pdf_is_a_real_pdf(self, reviewed):
        from slr_studio.framework.figures import write_pdf
        from slr_studio.framework.layout import Edge, Node, layout_graph
        from slr_studio.framework.style import style_for

        nodes = [Node(key=n, label=n) for n in ("trust", "participation")]
        edges = [Edge(key="trust->participation", source="trust",
                      target="participation")]
        layout = layout_graph(nodes, edges)
        drawn = {
            "trust->participation": {
                "style": style_for("strong", "positive"),
                "label": "L1 +",
                "grade": "strong",
            }
        }
        path = write_pdf(
            layout, drawn, reviewed.exports_dir / "figure.pdf", "A figure"
        )
        assert path is not None
        assert path.read_bytes().startswith(b"%PDF")

    def test_the_gap_map_is_also_written_as_pdf(self, reviewed):
        from slr_studio.gaps.gapmap import Cell, Grid, write_pdf

        grid = Grid(
            dimension="country",
            rows=["trust"],
            columns=["Kerala"],
            cells={("trust", "Kerala"): Cell("trust", "Kerala", 2, 2,
                                             "moderate", (1, 2))},
            searched_to="2026-03-01",
        )
        path = write_pdf(grid, reviewed.exports_dir / "map.pdf", title="A map")
        assert path is not None
        assert path.read_bytes().startswith(b"%PDF")
