"""The synthesis tables in Word, beside the workbook.

A reviewer writing in Word should not have to paste a spreadsheet into their
manuscript. These are the same three tables the workbook holds -- the graded
inventory, the sensitivity analyses and the contradictions -- read from the
same views, so the Word file and the Excel file cannot disagree about a
number.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from ..core.db import utc_now

__all__ = ["write_synthesis_tables"]


def _add_table(document, headings: Sequence[str], rows: Sequence[Sequence[Any]]):
    from docx.shared import Pt

    table = document.add_table(rows=1, cols=len(headings))
    table.style = "Table Grid"
    for index, heading in enumerate(headings):
        cell = table.rows[0].cells[index]
        cell.text = heading
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True
    for row in rows:
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = "" if value is None else str(value)
    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(8.5)
    return table


def _interval(row: dict[str, Any]) -> str:
    if row["ci_low"] is None or row["ci_high"] is None:
        return "-"
    return f"{row['ci_low'] * 100:.0f}–{row['ci_high'] * 100:.0f}%"


def write_synthesis_tables(project, path: str | Path) -> Path:
    """The graded inventory, sensitivity analyses and contradictions."""
    from docx import Document

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = project.db

    document = Document()
    document.add_heading("Synthesis tables", level=1)
    document.add_paragraph(project.info()["title"])

    links = [
        dict(row)
        for row in db.query(
            "SELECT g.*, p.name AS predictor_name, o.name AS outcome_name "
            "FROM graded_link g "
            "JOIN construct p ON p.id = g.predictor_construct "
            "JOIN construct o ON o.id = g.outcome_construct "
            "ORDER BY g.grade, g.id"
        )
    ]
    if not links:
        document.add_paragraph(
            "There is no synthesis in this project yet, so there are no "
            "tables to write. Run the Synthesise stage first."
        )
        document.save(str(path))
        return path

    document.add_heading("Graded evidence inventory", level=2)
    document.add_paragraph(
        "One row per link. The interval is a Wilson 95% interval on the share "
        "of datasets agreeing, and p is a two-sided exact sign test."
    )
    _add_table(
        document,
        ["Link", "Predictor", "Outcome", "Direction", "Grade", "Datasets",
         "Agree", "Oppose", "95% interval", "p", "Countries"],
        [
            [
                f"L{row['id']}",
                row["predictor_name"],
                row["outcome_name"],
                row["direction"],
                row["grade"],
                row["n_datasets"],
                row["n_agree"],
                row["n_oppose"],
                _interval(row),
                "-" if row["sign_p"] is None
                else f"{row['sign_p']:.3f}",
                row["n_countries"],
            ]
            for row in links
        ],
    )

    document.add_heading("Sensitivity analyses", level=2)
    sensitivity = [
        dict(row)
        for row in db.query(
            "SELECT * FROM sensitivity_result ORDER BY analysis, link_key"
        )
    ]
    if sensitivity:
        changed = [r for r in sensitivity if r["changed_grade"] or r["changed_sign"]]
        document.add_paragraph(
            f"{len(sensitivity)} link-and-analysis combination(s) were "
            f"examined; {len(changed)} changed sign or grade. Only those are "
            "listed."
            if changed
            else f"{len(sensitivity)} link-and-analysis combination(s) were "
                 "examined. None changed sign or grade."
        )
        if changed:
            _add_table(
                document,
                ["Analysis", "Link", "Grade before", "Grade after",
                 "Direction before", "Direction after"],
                [
                    [
                        row["analysis"].replace("_", " "),
                        row["link_key"],
                        row["base_grade"],
                        row["new_grade"],
                        row["base_direction"],
                        row["new_direction"],
                    ]
                    for row in changed
                ],
            )
    else:
        document.add_paragraph("No sensitivity analysis has been run.")

    document.add_heading("Contradiction table", level=2)
    contradictions = [
        dict(row)
        for row in db.query(
            "SELECT * FROM contradiction ORDER BY link_key, id"
        )
    ]
    if contradictions:
        document.add_paragraph(
            "Each contested link, with the explanation offered for the "
            "disagreement and what the reviewer decided."
        )
        _add_table(
            document,
            ["Link", "Explained by", "Explanation", "Suggested moderator",
             "Status"],
            [
                [
                    row["link_key"],
                    row["explanation_type"],
                    row["explanation"],
                    row["suggested_moderator"],
                    row["status"],
                ]
                for row in contradictions
            ],
        )
    else:
        document.add_paragraph(
            "No link in this review is contested, so there is no "
            "contradiction table."
        )

    document.add_paragraph()
    document.add_paragraph(
        f"Generated {utc_now()} from the project database. Every number here "
        "is read from the same views as the workbook."
    )
    document.save(str(path))
    return path
