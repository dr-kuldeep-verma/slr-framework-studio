"""The Excel extraction matrix.

What a supervisor or a journal asks for when they want to see the data behind
a synthesis. Four sheets:

* **Studies** — one row per included report: where, who, what design, how
  many, which appraisal instrument and what concern.
* **Relationships** — one row per relationship, with the authors' own labels,
  the direction, the quotation, the page, whether the quotation was found
  there, and what the blind direction check made of it.
* **Checks** — everything a human still has to look at, and why.
* **Inter-coder agreement** — field by field, when a second coder has coded
  anything.

The labels are the authors' own throughout. Mapping them onto shared
constructs is the next stage's job, and a reviewer reading this sheet should
see what the papers said, not what the software decided they meant.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

__all__ = ["write_extraction_matrix"]

_HEADER_FILL = PatternFill("solid", fgColor="E5EDFF")
_FLAG_FILL = PatternFill("solid", fgColor="FFF2CC")
_HEADER_FONT = Font(bold=True)
_WRAP = Alignment(vertical="top", wrap_text=True)
_TOP = Alignment(vertical="top")


def _style_header(sheet) -> None:
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _WRAP
    sheet.freeze_panes = "A2"


def _widths(sheet, widths: dict[int, int]) -> None:
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width


def _studies_sheet(sheet, project) -> None:
    sheet.append([
        "Record", "Title", "Year", "Journal", "DOI", "Country", "Setting",
        "Population", "Design (as described)", "Design family",
        "Manipulated treatment", "Control group", "Design downgraded",
        "Sampling", "Sample size", "Time horizon", "Theories",
        "Framework stages", "Key findings", "Limitations",
        "Appraisal instrument", "Instrument fits design", "Concern",
        "Appraisal note", "Relationships", "Needs review",
    ])
    _style_header(sheet)

    rows = project.db.query(
        "SELECT e.*, r.title, r.year, r.journal, r.doi, "
        "       (SELECT COUNT(*) FROM relationship rel "
        "        WHERE rel.record_id = e.record_id AND rel.coder_role = 'ai') AS n_rel "
        "FROM report_extraction e JOIN record r ON r.id = e.record_id "
        "WHERE e.coder_role = 'ai' ORDER BY e.record_id"
    )
    for row in rows:
        try:
            theories = json.loads(row["theories"] or "[]")
        except ValueError:
            theories = []
        try:
            stages = json.loads(row["framework_stages"] or "[]")
        except ValueError:
            stages = []
        sheet.append([
            row["record_id"], row["title"], row["year"], row["journal"],
            row["doi"] or "", row["country"], row["setting"], row["population"],
            row["design"], row["design_family"],
            "yes" if row["manipulated_treatment"] else "no",
            "yes" if row["control_group"] else "no",
            "yes" if row["design_downgraded"] else "",
            row["sampling"],
            row["sample_size"] if row["sample_size"] is not None
            else row["sample_size_text"],
            row["time_horizon"],
            "; ".join(
                f"{t.get('name', '')}" + (f" ({t['role']})" if t.get("role") else "")
                for t in theories
            ),
            "; ".join(str(s) for s in stages),
            row["key_findings"], row["limitations"],
            row["appraisal_instrument"],
            "yes" if row["instrument_matches_design"] else "NO",
            row["appraisal_concern"], row["appraisal_note"],
            row["n_rel"],
            row["review_reason"] if row["needs_review"] else "",
        ])
        if row["design_downgraded"] or not row["instrument_matches_design"]:
            for cell in sheet[sheet.max_row]:
                cell.fill = _FLAG_FILL

    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = _TOP
    _widths(sheet, {
        1: 8, 2: 46, 3: 7, 4: 26, 5: 22, 6: 14, 7: 18, 8: 20, 9: 26, 10: 20,
        11: 18, 12: 14, 13: 16, 14: 20, 15: 12, 16: 16, 17: 26, 18: 22,
        19: 48, 20: 40, 21: 18, 22: 18, 23: 12, 24: 36, 25: 12, 26: 40,
    })


def _relationships_sheet(sheet, project) -> None:
    sheet.append([
        "Relationship", "Record", "Study", "Year", "Design family",
        "Predictor (authors' label)", "Outcome (authors' label)",
        "Mediator", "Moderator", "Type", "Direction", "Strength", "Statistic",
        "Significance", "Evidence basis", "Quotation", "Page",
        "Quotation found", "Direction re-derived", "Check agrees",
        "Check note", "Needs review",
    ])
    _style_header(sheet)

    rows = project.db.query(
        "SELECT rel.*, r.title, r.year, e.design_family "
        "FROM relationship rel JOIN record r ON r.id = rel.record_id "
        "LEFT JOIN report_extraction e ON e.record_id = rel.record_id "
        "  AND e.coder_role = 'ai' "
        "WHERE rel.coder_role = 'ai' ORDER BY rel.record_id, rel.position, rel.id"
    )
    for row in rows:
        if not row["quote"]:
            found = "no quotation"
        elif row["quote_verified"]:
            found = "on the cited page"
        elif row["quote_found_on"]:
            found = f"on page {row['quote_found_on']} instead"
        else:
            found = "NOT IN THE REPORT"
        agrees = (
            "" if row["verify_agrees"] is None
            else ("yes" if row["verify_agrees"] else "NO")
        )
        sheet.append([
            row["id"], row["record_id"], row["title"], row["year"],
            row["design_family"] or "",
            row["predictor_label"], row["outcome_label"],
            row["mediator_label"], row["moderator_label"],
            row["relationship_type"], row["direction"], row["strength"],
            row["statistic"], row["significance"], row["evidence_basis"],
            row["quote"], row["quote_page"] if row["quote_page"] else "",
            found, row["verified_direction"] or "", agrees,
            row["verify_note"],
            row["review_reason"] if row["needs_review"] else "",
        ])
        if row["needs_review"]:
            for cell in sheet[sheet.max_row]:
                cell.fill = _FLAG_FILL

    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = _TOP
    _widths(sheet, {
        1: 12, 2: 8, 3: 40, 4: 7, 5: 18, 6: 34, 7: 34, 8: 20, 9: 20, 10: 16,
        11: 12, 12: 14, 13: 14, 14: 16, 15: 15, 16: 52, 17: 7, 18: 20,
        19: 18, 20: 12, 21: 34, 22: 40,
    })


def _checks_sheet(sheet, project) -> None:
    sheet.append(["Record", "Study", "Why", "Detail", "Raised", "Resolved", "Outcome"])
    _style_header(sheet)
    labels = {
        "direction_mismatch": "The check read the direction differently",
        "quote_failed": "A quotation did not check out",
        "design_rule": "The design or appraisal needs a look",
        "nothing_extracted": "No relationship was extracted",
    }
    for row in project.db.query(
        "SELECT q.*, r.title FROM review_queue q JOIN record r ON r.id = q.record_id "
        "WHERE q.stage = 'extract' ORDER BY q.resolved_at IS NOT NULL, q.id"
    ):
        sheet.append([
            row["record_id"], row["title"],
            labels.get(row["reason"], row["reason"]), row["detail"],
            (row["created_at"] or "").replace("T", " ").replace("Z", ""),
            (row["resolved_at"] or "").replace("T", " ").replace("Z", ""),
            row["resolution"],
        ])
        if not row["resolved_at"]:
            for cell in sheet[sheet.max_row]:
                cell.fill = _FLAG_FILL
    for line in sheet.iter_rows(min_row=2):
        for cell in line:
            cell.alignment = _TOP
    _widths(sheet, {1: 8, 2: 44, 3: 34, 4: 70, 5: 20, 6: 20, 7: 18})


def _agreement_sheet(sheet, project) -> None:
    sheet.append([
        "Field", "Reports or relationships compared", "Agreed",
        "Percentage agreement", "Cohen's κ", "PABAK", "When",
    ])
    _style_header(sheet)
    rows = project.db.query("SELECT * FROM coder_agreement ORDER BY id")
    for row in rows:
        sheet.append([
            row["scope"], row["n"], row["n_agree"], row["percent_agreement"],
            row["kappa"], row["pabak"],
            (row["created_at"] or "").replace("T", " ").replace("Z", ""),
        ])
    for line in sheet.iter_rows(min_row=2, min_col=4, max_col=6):
        for cell in line:
            cell.number_format = "0.000"
    if not rows:
        sheet.append([
            "No report has been coded by a second coder yet, so no agreement "
            "can be reported.",
        ])
    _widths(sheet, {1: 34, 2: 30, 3: 10, 4: 20, 5: 12, 6: 12, 7: 20})


def write_extraction_matrix(project, path: str | Path | None = None) -> Path:
    """Write the matrix and return where it went."""
    path = Path(path) if path else project.exports_dir / "extraction-matrix.xlsx"
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "Studies"
    _studies_sheet(sheet, project)
    _relationships_sheet(workbook.create_sheet("Relationships"), project)
    _checks_sheet(workbook.create_sheet("Checks"), project)
    _agreement_sheet(workbook.create_sheet("Inter-coder agreement"), project)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    project.audit.log(
        "extract",
        "extraction_matrix_exported",
        detail={
            "path": path.name,
            "n_studies": int(
                project.db.scalar(
                    "SELECT COUNT(*) FROM report_extraction WHERE coder_role = 'ai'"
                ) or 0
            ),
            "n_relationships": int(
                project.db.scalar(
                    "SELECT COUNT(*) FROM relationship WHERE coder_role = 'ai'"
                ) or 0
            ),
        },
    )
    return path
