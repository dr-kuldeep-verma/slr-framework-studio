"""The evidence gap map as a drawing.

An empty cell is hatched rather than merely pale, so that "no dataset" cannot
be read as "a little evidence" in a greyscale print. The test below exists
because the first version drew the hatch as long diagonal lines that ran past
the cell's own edge and into its neighbour, which made a populated cell look
partly empty.
"""

from __future__ import annotations

import pytest

from slr_studio.gaps.gapmap import Cell, Grid, write_excel, write_png, write_svg


@pytest.fixture
def grid() -> Grid:
    rows = ["trust", "fairness"]
    columns = ["Kerala", "not reported"]
    cells = {
        ("trust", "Kerala"): Cell("trust", "Kerala", 4, 4, "strong", (1, 2, 3, 4)),
        ("trust", "not reported"): Cell("trust", "not reported", 0, 0, "", ()),
        ("fairness", "Kerala"): Cell("fairness", "Kerala", 0, 0, "", ()),
        ("fairness", "not reported"): Cell(
            "fairness", "not reported", 2, 2, "tentative", (5, 6)
        ),
    }
    return Grid(
        dimension="country", rows=rows, columns=columns, cells=cells,
        searched_to="2026-03-01",
    )


class TestTheDrawing:
    def test_the_svg_is_written(self, grid, tmp_path):
        path = write_svg(grid, tmp_path / "map.svg", title="A map")
        assert path.is_file()
        assert "<svg" in path.read_text(encoding="utf-8")

    def test_an_empty_cell_is_hatched_in_the_svg(self, grid, tmp_path):
        text = write_svg(grid, tmp_path / "map.svg").read_text(encoding="utf-8")
        assert 'url(#empty)' in text

    def test_the_legend_names_every_grade_and_the_empty_cell(self, grid, tmp_path):
        text = write_svg(grid, tmp_path / "map.svg").read_text(encoding="utf-8")
        for grade in ("strong", "moderate", "emerging", "tentative"):
            assert f">{grade}<" in text
        assert "no dataset" in text

    def test_the_png_is_written(self, grid, tmp_path):
        assert write_png(grid, tmp_path / "map.png") is not None

    def test_the_hatch_stays_inside_its_own_cell(self, grid, tmp_path):
        """A populated cell must be solid, with no hatch bled in from next door."""
        from PIL import Image

        from slr_studio.gaps.gapmap import (
            _CELL_HEIGHT, _CELL_WIDTH, _COLUMN_LABEL, _MARGIN, _ROW_LABEL,
        )

        path = write_png(grid, tmp_path / "map.png")
        image = Image.open(path).convert("RGB")
        scale = 2

        # "trust" x "Kerala" holds four datasets and is graded strong.
        left = (_MARGIN + _ROW_LABEL) * scale
        top = (_MARGIN + _COLUMN_LABEL) * scale
        expected = tuple(
            int(Cell("t", "c", 4, 4, "strong", ()).colour.lstrip("#")[i:i + 2], 16)
            for i in (0, 2, 4)
        )
        # Sample along the right-hand edge, where a hatch drawn in the cell
        # to the right would spill, and away from the number in the middle.
        width = int(_CELL_WIDTH * scale)
        height = int(_CELL_HEIGHT * scale)
        samples = [
            (left + 8 * scale, top + 6 * scale),
            (left + width - 4, top + 6 * scale),
            (left + width - 4, top + height // 4),
            (left + width - 4, top + height - 6),
            (left + width - 20, top + height // 4),
            (left + 8 * scale, top + height - 6 * scale),
        ]
        for point in samples:
            assert image.getpixel(point) == expected, (
                f"pixel {point} is {image.getpixel(point)}, not the cell's "
                f"colour {expected} -- something was drawn over it"
            )

    def test_the_png_carries_the_legend_too(self, grid, tmp_path):
        """The PNG is what goes into a manuscript; a key-less map is unreadable."""
        from PIL import Image

        from slr_studio.gaps.gapmap import _EMPTY_HATCH

        path = write_png(grid, tmp_path / "map.png")
        image = Image.open(path).convert("RGB")
        strong = tuple(
            int("1b5e20"[i:i + 2], 16) for i in (0, 2, 4)
        )
        # The legend swatch sits below the matrix, where no cell is drawn.
        bottom_band = [
            image.getpixel((x, y))
            for y in range(int(image.height * 0.88), image.height - 2)
            for x in range(2, image.width - 2, 3)
        ]
        assert strong in bottom_band

    def test_the_excel_has_a_sheet_listing_each_cell_s_studies(
        self, grid, tmp_path
    ):
        from openpyxl import load_workbook

        path = write_excel(grid, tmp_path / "map.xlsx", title="A map")
        workbook = load_workbook(path)
        assert "Studies per cell" in workbook.sheetnames
        rows = list(workbook["Studies per cell"].values)
        assert rows[0][0] == "Construct"
        assert any("1, 2, 3, 4" in str(row[4]) for row in rows[1:])

    def test_the_excel_says_what_the_counts_are_qualified_by(self, grid, tmp_path):
        from openpyxl import load_workbook

        path = write_excel(grid, tmp_path / "map.xlsx")
        sheet = load_workbook(path)["Gap map"]
        text = " ".join(
            str(cell.value) for row in sheet.iter_rows(max_row=3) for cell in row
            if cell.value
        )
        assert "2026-03-01" in text
        assert "included corpus" in text.casefold()


class TestTheCanvasFitsTheLegend:
    """A small matrix is narrower than its own key, and the key still has to fit."""

    def test_a_two_column_map_is_wide_enough_for_every_legend_entry(self):
        from slr_studio.gaps.gapmap import _legend_width, _size

        grid = Grid(
            dimension="country",
            rows=["trust"],
            columns=["Kerala", "not reported"],
            cells={},
        )
        width, _height = _size(grid)
        assert width >= _legend_width()

    def test_the_last_legend_swatch_is_drawn_inside_the_png(self, tmp_path):
        from PIL import Image

        from slr_studio.gaps.gapmap import _EMPTY_HATCH

        grid = Grid(
            dimension="country",
            rows=["trust"],
            columns=["Kerala"],
            cells={("trust", "Kerala"): Cell("trust", "Kerala", 1, 1,
                                             "tentative", (1,))},
        )
        path = write_png(grid, tmp_path / "narrow.png")
        image = Image.open(path).convert("RGB")
        hatch = tuple(int(_EMPTY_HATCH.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        band = [
            image.getpixel((x, y))
            for y in range(int(image.height * 0.85), image.height - 1)
            for x in range(2, image.width - 2)
        ]
        # The "no dataset" swatch is the last entry; if the canvas were too
        # narrow it would be drawn off the edge and never appear.
        assert hatch in band
