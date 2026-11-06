"""The evidence gap map: a matrix of what has been studied where.

Rows are constructs, columns are a context dimension the reviewer chooses --
country, region, income group, setting, population, activity or design. Each
cell carries the number of datasets behind it, the best grade among them, and
the records themselves, so that clicking a cell can list its studies.

Colour runs from the strongest grade present down to empty. An empty cell is
drawn distinctly rather than as a pale version of a weak one: the difference
between "a little evidence" and "none" is the whole point of the figure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape

__all__ = [
    "Cell", "Grid", "build_grid", "write_excel", "write_pdf", "write_png",
    "write_svg",
]

#: Strongest first; a cell takes the colour of the best grade in it.
_GRADE_COLOURS = (
    ("strong", "#1b5e20"),
    ("moderate", "#43a047"),
    ("emerging", "#9ccc65"),
    ("tentative", "#fff59d"),
    ("contested", "#ef9a9a"),
    ("asserted", "#e0e0e0"),
)
_EMPTY_COLOUR = "#ffffff"
_EMPTY_HATCH = "#cfd6dd"

_CELL_WIDTH = 86.0
_CELL_HEIGHT = 34.0
_ROW_LABEL = 150.0
_COLUMN_LABEL = 96.0
_MARGIN = 16.0
_LEGEND = 34.0


@dataclass(frozen=True)
class Cell:
    row: str
    column: str
    n_datasets: int = 0
    n_records: int = 0
    best_grade: str = ""
    record_ids: tuple[int, ...] = ()

    @property
    def colour(self) -> str:
        if self.n_datasets == 0:
            return _EMPTY_COLOUR
        for grade, colour in _GRADE_COLOURS:
            if self.best_grade == grade:
                return colour
        return _GRADE_COLOURS[-1][1]


@dataclass
class Grid:
    dimension: str
    rows: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    cells: dict[tuple[str, str], Cell] = field(default_factory=dict)
    searched_to: str = ""

    def cell(self, row: str, column: str) -> Cell:
        return self.cells.get((row, column), Cell(row=row, column=column))

    def studies_in(self, row: str, column: str) -> tuple[int, ...]:
        """The records behind one cell, for the click-through."""
        return self.cell(row, column).record_ids


def build_grid(
    constructs: Sequence[str],
    datasets: Sequence[Any],
    links: Sequence[Any],
    *,
    dimension: str,
    searched_to: str = "",
) -> Grid:
    """Cross the constructs against one context dimension.

    Counted in datasets, because a dataset is what votes; the records are kept
    alongside so a reviewer can see which papers a cell stands for.
    """
    constructs_by_record: dict[int, set[str]] = {}
    grade_by_record: dict[int, set[str]] = {}
    for link in links:
        for record_id in link.record_ids:
            constructs_by_record.setdefault(record_id, set()).update(
                {link.predictor, link.outcome}
            )
            grade_by_record.setdefault(record_id, set()).add(link.grade)

    columns = sorted(
        {(getattr(d, dimension, "") or "not reported") for d in datasets}
    )
    rows = list(constructs)
    grid = Grid(
        dimension=dimension, rows=rows, columns=columns, searched_to=searched_to
    )

    order = [grade for grade, _ in _GRADE_COLOURS]
    for row in rows:
        for column in columns:
            members = [
                d
                for d in datasets
                if (getattr(d, dimension, "") or "not reported") == column
                and any(
                    row in constructs_by_record.get(record_id, set())
                    for record_id in d.record_ids
                )
            ]
            record_ids = sorted({r for d in members for r in d.record_ids})
            grades = {
                grade
                for record_id in record_ids
                for grade in grade_by_record.get(record_id, set())
            }
            best = next((g for g in order if g in grades), "")
            grid.cells[(row, column)] = Cell(
                row=row,
                column=column,
                n_datasets=len(members),
                n_records=len(record_ids),
                best_grade=best,
                record_ids=tuple(record_ids),
            )
    return grid


# -- drawing ----------------------------------------------------------------


def _legend_width() -> float:
    """How wide the key is, which a small matrix can easily be narrower than."""
    width = _MARGIN
    for grade, _colour in _GRADE_COLOURS:
        width += 15 + len(grade) * 5.2 + 12
    width += 15 + len("no dataset") * 5.2 + _MARGIN
    return width


def _size(grid: Grid) -> tuple[float, float]:
    width = _ROW_LABEL + len(grid.columns) * _CELL_WIDTH + 2 * _MARGIN
    height = (
        _COLUMN_LABEL + len(grid.rows) * _CELL_HEIGHT + 2 * _MARGIN + _LEGEND
    )
    # A map of two columns is narrower than its own key; the key still has to
    # fit, or the last grades are drawn off the edge and never seen.
    return max(width, _legend_width(), 320.0), max(height, 200.0)


def write_svg(grid: Grid, path: Path, *, title: str = "") -> Path:
    width, height = _size(grid)
    top = _MARGIN + _COLUMN_LABEL
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        # The hatch an empty cell is filled with, so "none" cannot be mistaken
        # for "a little" in a greyscale print.
        '<defs><pattern id="empty" width="6" height="6" '
        'patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        f'<line x1="0" y1="0" x2="0" y2="6" stroke="{_EMPTY_HATCH}" '
        'stroke-width="1.5"/></pattern></defs>',
    ]
    if title:
        parts.append(
            f'<text x="{_MARGIN}" y="{_MARGIN + 10}" font-family="Helvetica" '
            f'font-size="12" font-weight="bold" fill="#1a1a1a">'
            f"{escape(title)}</text>"
        )

    for index, column in enumerate(grid.columns):
        x = _MARGIN + _ROW_LABEL + index * _CELL_WIDTH + _CELL_WIDTH / 2
        parts.append(
            f'<text x="{x:.1f}" y="{top - 6:.1f}" font-family="Helvetica" '
            f'font-size="9" fill="#44505e" text-anchor="end" '
            f'transform="rotate(-45 {x:.1f} {top - 6:.1f})">'
            f"{escape(column[:28])}</text>"
        )

    for row_index, row in enumerate(grid.rows):
        y = top + row_index * _CELL_HEIGHT
        parts.append(
            f'<text x="{_MARGIN + _ROW_LABEL - 6:.1f}" '
            f'y="{y + _CELL_HEIGHT / 2 + 3.5:.1f}" font-family="Helvetica" '
            f'font-size="10" fill="#1a1a1a" text-anchor="end">'
            f"{escape(row[:30])}</text>"
        )
        for column_index, column in enumerate(grid.columns):
            cell = grid.cell(row, column)
            x = _MARGIN + _ROW_LABEL + column_index * _CELL_WIDTH
            fill = "url(#empty)" if cell.n_datasets == 0 else cell.colour
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{_CELL_WIDTH:.1f}" '
                f'height="{_CELL_HEIGHT:.1f}" fill="{fill}" '
                'stroke="#ffffff" stroke-width="1"/>'
            )
            label = "0" if cell.n_datasets == 0 else str(cell.n_datasets)
            dark = cell.best_grade in ("strong", "moderate")
            parts.append(
                f'<text x="{x + _CELL_WIDTH / 2:.1f}" '
                f'y="{y + _CELL_HEIGHT / 2 + 3.5:.1f}" font-family="Helvetica" '
                f'font-size="10" text-anchor="middle" '
                f'fill="{"#ffffff" if dark else "#1a1a1a"}">{label}</text>'
            )

    legend_y = height - _MARGIN - 10
    x = _MARGIN
    for grade, colour in _GRADE_COLOURS:
        parts.append(
            f'<rect x="{x:.1f}" y="{legend_y - 9:.1f}" width="11" height="11" '
            f'fill="{colour}" stroke="#b7c0cb"/>'
        )
        parts.append(
            f'<text x="{x + 15:.1f}" y="{legend_y:.1f}" font-family="Helvetica" '
            f'font-size="9" fill="#44505e">{grade}</text>'
        )
        x += 15 + len(grade) * 5.2 + 12
    parts.append(
        f'<rect x="{x:.1f}" y="{legend_y - 9:.1f}" width="11" height="11" '
        'fill="url(#empty)" stroke="#b7c0cb"/>'
    )
    parts.append(
        f'<text x="{x + 15:.1f}" y="{legend_y:.1f}" font-family="Helvetica" '
        'font-size="9" fill="#44505e">no dataset</text>'
    )
    parts.append("</svg>")
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def write_png(grid: Grid, path: Path, *, title: str = "") -> Path | None:
    """The same matrix as a bitmap. Returns None if Pillow is missing."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    from ..framework.figures import _font

    scale = 2
    width, height = _size(grid)
    image = Image.new("RGB", (int(width * scale), int(height * scale)), "#ffffff")
    draw = ImageDraw.Draw(image)
    top = (_MARGIN + _COLUMN_LABEL) * scale
    small = _font(int(9 * scale))
    medium = _font(int(10 * scale))

    if title:
        draw.text(
            (_MARGIN * scale, _MARGIN * scale), title,
            fill="#1a1a1a", font=_font(int(12 * scale)),
        )

    for index, column in enumerate(grid.columns):
        x = (_MARGIN + _ROW_LABEL + index * _CELL_WIDTH + _CELL_WIDTH / 2) * scale
        draw.text(
            (x, top - 8 * scale), column[:18], fill="#44505e", font=small,
            anchor="md",
        )

    for row_index, row in enumerate(grid.rows):
        y = top + row_index * _CELL_HEIGHT * scale
        draw.text(
            ((_MARGIN + _ROW_LABEL - 6) * scale, y + _CELL_HEIGHT * scale / 2),
            row[:26], fill="#1a1a1a", font=medium, anchor="rm",
        )
        for column_index, column in enumerate(grid.columns):
            cell = grid.cell(row, column)
            x = (_MARGIN + _ROW_LABEL + column_index * _CELL_WIDTH) * scale
            box = [
                (x, y),
                (x + _CELL_WIDTH * scale, y + _CELL_HEIGHT * scale),
            ]
            draw.rectangle(box, fill=cell.colour, outline="#ffffff", width=scale)
            if cell.n_datasets == 0:
                # Hatch, so an empty cell is not just a pale one. Drawn on a
                # tile of the cell's own size and pasted, because drawing the
                # diagonals straight onto the figure let them run past the
                # cell's edge and into the neighbour, which made a populated
                # cell look partly empty.
                image.paste(
                    _hatch_tile(
                        int(_CELL_WIDTH * scale), int(_CELL_HEIGHT * scale), scale
                    ),
                    (int(x), int(y)),
                )
                draw.rectangle(box, outline="#ffffff", width=scale)
            dark = cell.best_grade in ("strong", "moderate")
            draw.text(
                (x + _CELL_WIDTH * scale / 2, y + _CELL_HEIGHT * scale / 2),
                str(cell.n_datasets),
                fill="#ffffff" if dark else "#1a1a1a",
                font=medium, anchor="mm",
            )

    # The key. A map without one is unreadable, and the PNG is the copy that
    # goes into a manuscript.
    legend_y = int(height * scale) - int(_MARGIN * scale) - 11 * scale
    x = _MARGIN * scale
    for grade, colour in _GRADE_COLOURS:
        draw.rectangle(
            [(x, legend_y), (x + 11 * scale, legend_y + 11 * scale)],
            fill=colour, outline="#b7c0cb",
        )
        draw.text(
            (x + 15 * scale, legend_y + 5 * scale), grade,
            fill="#44505e", font=small, anchor="lm",
        )
        x += int((15 + len(grade) * 5.2 + 12) * scale)
    draw.rectangle(
        [(x, legend_y), (x + 11 * scale, legend_y + 11 * scale)],
        fill="#ffffff", outline="#b7c0cb",
    )
    image.paste(_hatch_tile(11 * scale, 11 * scale, scale), (int(x), legend_y))
    draw.rectangle(
        [(x, legend_y), (x + 11 * scale, legend_y + 11 * scale)],
        outline="#b7c0cb",
    )
    draw.text(
        (x + 15 * scale, legend_y + 5 * scale), "no dataset",
        fill="#44505e", font=small, anchor="lm",
    )

    image.save(path, "PNG")
    return path


def _hatch_tile(width: int, height: int, scale: int):
    """A hatched tile the size of one cell, clipped to itself by construction."""
    from PIL import Image, ImageDraw

    tile = Image.new("RGB", (max(width, 1), max(height, 1)), "#ffffff")
    pen = ImageDraw.Draw(tile)
    step = 6 * scale
    for offset in range(0, width + height + 1, step):
        pen.line(
            [(offset, 0), (offset - height, height)],
            fill=_EMPTY_HATCH, width=max(1, scale),
        )
    return tile


def write_excel(grid: Grid, path: Path, *, title: str = "") -> Path:
    """The matrix as a sheet, with a second sheet listing each cell's studies."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Gap map"

    sheet.cell(row=1, column=1, value=title or f"Constructs by {grid.dimension}")
    sheet.cell(row=1, column=1).font = Font(bold=True)
    if grid.searched_to:
        sheet.cell(
            row=2, column=1,
            value=f"Within the included corpus, searched to {grid.searched_to}. "
                  "Counts are datasets.",
        )

    header = 4
    for index, column in enumerate(grid.columns, start=2):
        cell = sheet.cell(row=header, column=index, value=column)
        cell.font = Font(bold=True)
        cell.alignment = Alignment(textRotation=60, horizontal="center")
    for row_index, row in enumerate(grid.rows, start=header + 1):
        sheet.cell(row=row_index, column=1, value=row).font = Font(bold=True)
        for column_index, column in enumerate(grid.columns, start=2):
            source = grid.cell(row, sheet.cell(row=header, column=column_index).value)
            target = sheet.cell(row=row_index, column=column_index,
                                value=source.n_datasets)
            target.alignment = Alignment(horizontal="center")
            colour = (
                source.colour if source.n_datasets else _EMPTY_HATCH
            ).lstrip("#")
            target.fill = PatternFill("solid", fgColor=colour)

    sheet.column_dimensions["A"].width = 32
    sheet.freeze_panes = "B5"

    studies = workbook.create_sheet("Studies per cell")
    studies.append(["Construct", grid.dimension, "Datasets", "Best grade",
                    "Record ids"])
    for cell in studies[1]:
        cell.font = Font(bold=True)
    for row in grid.rows:
        for column in grid.columns:
            source = grid.cell(row, column)
            studies.append(
                [
                    row,
                    column,
                    source.n_datasets,
                    source.best_grade or "-",
                    ", ".join(str(r) for r in source.record_ids),
                ]
            )
    studies.column_dimensions["A"].width = 30
    studies.column_dimensions["B"].width = 24
    studies.column_dimensions["E"].width = 40
    studies.freeze_panes = "A2"

    workbook.save(path)
    return path


def write_pdf(grid: Grid, path: Path, *, title: str = "") -> Path | None:
    """The gap map as a PDF, drawn from the same grid as the other formats."""
    try:
        from reportlab.lib.colors import HexColor
        from reportlab.pdfgen import canvas as pdfcanvas
    except Exception:
        return None

    width, height = _size(grid)
    page = pdfcanvas.Canvas(str(path), pagesize=(width, height))
    page.setTitle(title or "Evidence gap map")
    top = _MARGIN + _COLUMN_LABEL

    def y_of(value: float) -> float:
        return height - value

    if title:
        page.setFont("Helvetica-Bold", 12)
        page.drawString(_MARGIN, y_of(_MARGIN + 10), title)

    page.setFont("Helvetica", 9)
    for index, column in enumerate(grid.columns):
        x = _MARGIN + _ROW_LABEL + index * _CELL_WIDTH + _CELL_WIDTH / 2
        page.saveState()
        page.translate(x, y_of(top - 6))
        page.rotate(45)
        page.setFillColor(HexColor("#44505e"))
        page.drawRightString(0, 0, column[:28])
        page.restoreState()

    for row_index, row in enumerate(grid.rows):
        y = top + row_index * _CELL_HEIGHT
        page.setFillColor(HexColor("#1a1a1a"))
        page.setFont("Helvetica", 10)
        page.drawRightString(
            _MARGIN + _ROW_LABEL - 6, y_of(y + _CELL_HEIGHT / 2 + 3.5), row[:30]
        )
        for column_index, column in enumerate(grid.columns):
            cell = grid.cell(row, column)
            x = _MARGIN + _ROW_LABEL + column_index * _CELL_WIDTH
            page.setFillColor(HexColor(cell.colour))
            page.setStrokeColor(HexColor("#ffffff"))
            page.rect(x, y_of(y + _CELL_HEIGHT), _CELL_WIDTH, _CELL_HEIGHT,
                      stroke=1, fill=1)
            if cell.n_datasets == 0:
                # Hatch an empty cell, clipped to itself so it cannot run
                # into the neighbour.
                page.saveState()
                clip = page.beginPath()
                clip.rect(x, y_of(y + _CELL_HEIGHT), _CELL_WIDTH, _CELL_HEIGHT)
                page.clipPath(clip, stroke=0, fill=0)
                page.setStrokeColor(HexColor(_EMPTY_HATCH))
                page.setLineWidth(0.8)
                offset = 0.0
                while offset <= _CELL_WIDTH + _CELL_HEIGHT:
                    page.line(
                        x + offset, y_of(y),
                        x + offset - _CELL_HEIGHT, y_of(y + _CELL_HEIGHT),
                    )
                    offset += 6.0
                page.restoreState()
            dark = cell.best_grade in ("strong", "moderate")
            page.setFillColor(HexColor("#ffffff" if dark else "#1a1a1a"))
            page.setFont("Helvetica", 10)
            page.drawCentredString(
                x + _CELL_WIDTH / 2,
                y_of(y + _CELL_HEIGHT / 2 + 3.5),
                str(cell.n_datasets),
            )

    legend_y = height - _MARGIN - 10
    x = _MARGIN
    page.setFont("Helvetica", 9)
    for grade, colour in _GRADE_COLOURS:
        page.setFillColor(HexColor(colour))
        page.setStrokeColor(HexColor("#b7c0cb"))
        page.rect(x, height - legend_y - 9, 11, 11, stroke=1, fill=1)
        page.setFillColor(HexColor("#44505e"))
        page.drawString(x + 15, height - legend_y - 6, grade)
        x += 15 + len(grade) * 5.2 + 12
    page.setFillColor(HexColor("#ffffff"))
    page.rect(x, height - legend_y - 9, 11, 11, stroke=1, fill=1)
    page.setFillColor(HexColor("#44505e"))
    page.drawString(x + 15, height - legend_y - 6, "no dataset")

    page.showPage()
    page.save()
    return path
