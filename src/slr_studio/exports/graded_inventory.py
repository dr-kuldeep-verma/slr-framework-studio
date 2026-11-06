"""The Excel graded inventory.

The synthesis's output, in the form a supervisor, a co-author or a journal
can recount by hand. Six sheets:

* **Inventory** — one row per link: grade, direction, counts, the Wilson
  interval, the sign test, and the sentence saying why it was graded that
  way.
* **Votes** — the dataset-level votes behind every link, so a reader can
  verify the counts rather than take them.
* **Codebook** — the constructs, their definitions, their boundaries, their
  reference orientations and who approved them.
* **Mapping** — which author label went to which construct, what the mapping
  took it to measure, and whether a human moved it.
* **Sensitivity** — every link that changes grade or sign under one of the
  five analyses.
* **Contradictions** — the contested links and the explanation offered for
  each.

The second sheet is the point of the file. A grade is only as good as the
votes under it, and a reader who cannot see them has to take the number on
trust.
"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

__all__ = ["write_graded_inventory"]

_HEADER_FILL = PatternFill("solid", fgColor="E5EDFF")
_HEADER_FONT = Font(bold=True)
_WRAP = Alignment(vertical="top", wrap_text=True)

#: A quiet colour per grade. Strong and contested are the two a reader looks
#: for first, so they are the two that are tinted; the rest stay plain.
_GRADE_FILL = {
    "strong": PatternFill("solid", fgColor="E3F2E1"),
    "contested": PatternFill("solid", fgColor="FFF2CC"),
    "asserted": PatternFill("solid", fgColor="F2F2F2"),
}


def _style(sheet, widths: dict[int, int]) -> None:
    for cell in sheet[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _WRAP
    sheet.freeze_panes = "A2"
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width


def _percent(value) -> str:
    return "" if value is None else f"{value:.1%}"


def _inventory_sheet(sheet, project) -> None:
    sheet.append([
        "Link", "Predictor", "Outcome", "Direction", "Grade", "Overridden",
        "Original grade", "Datasets", "Reports", "Agree", "Oppose",
        "Null", "Unclear", "Countries", "Country list", "Agreement",
        "95% CI low", "95% CI high", "Sign test p", "Significant",
        "Why this grade",
    ])
    for row in project.db.query("SELECT * FROM v_graded_inventory"):
        sheet.append([
            row["link_key"], row["predictor"], row["outcome"], row["direction"],
            row["grade"], "yes" if row["grade_overridden"] else "",
            row["original_grade"], row["n_datasets"], row["n_reports"],
            row["n_agree"], row["n_oppose"], row["n_null"],
            row["n_datasets"] - row["n_agree"] - row["n_oppose"] - row["n_null"],
            row["n_countries"], "; ".join(json.loads(row["countries"] or "[]")),
            _percent(row["proportion"]), _percent(row["ci_low"]),
            _percent(row["ci_high"]),
            "" if row["sign_p"] is None else round(row["sign_p"], 6),
            "yes" if row["sign_significant"] else "no",
            row["grade_reason"],
        ])
        fill = _GRADE_FILL.get(row["grade"])
        if fill:
            for cell in sheet[sheet.max_row]:
                cell.fill = fill
    _style(sheet, {1: 34, 2: 20, 3: 20, 15: 22, 21: 70})


def _votes_sheet(sheet, project) -> None:
    sheet.append([
        "Link", "Dataset", "Reports in dataset", "Vote", "Reports disagreed",
        "Country", "Design family", "Evidence basis", "Appraisal concern",
    ])
    for row in project.db.query(
        "SELECT p.name AS predictor, o.name AS outcome, v.*, d.label AS dataset "
        "FROM link_vote v "
        "JOIN construct p ON p.id = v.predictor_construct "
        "JOIN construct o ON o.id = v.outcome_construct "
        "JOIN dataset d ON d.id = v.dataset_id "
        "ORDER BY p.name, o.name, d.id"
    ):
        sheet.append([
            f"{row['predictor']} -> {row['outcome']}", row["dataset"],
            row["n_relationships"], row["direction"],
            "yes" if row["within_dataset_conflict"] else "",
            row["country"], row["design_family"], row["evidence_basis"],
            row["appraisal_concern"],
        ])
    _style(sheet, {1: 34, 2: 44})


def _codebook_sheet(sheet, project) -> None:
    sheet.append([
        "Construct", "Status", "Definition", "Includes", "Excludes",
        "Reference orientation", "Orientation set by", "Approved by",
        "Approved at", "Source",
    ])
    for row in project.db.query(
        "SELECT * FROM construct ORDER BY is_residual, position, id"
    ):
        sheet.append([
            row["name"], row["status"], row["definition"],
            "; ".join(json.loads(row["includes"] or "[]")),
            "; ".join(json.loads(row["excludes"] or "[]")),
            row["orientation"], row["orientation_set_by"],
            row["approved_by"], row["approved_at"], row["source"],
        ])
    _style(sheet, {1: 24, 3: 50, 4: 36, 5: 36, 6: 28})


def _mapping_sheet(sheet, project) -> None:
    sheet.append([
        "Label (as the authors wrote it)", "Role", "Construct",
        "Taken to measure", "Reasoning", "Confidence", "Flagged",
        "Why flagged", "Status", "Moved from", "Reason for the move",
        "Decided by", "Uses",
    ])
    for row in project.db.query(
        "SELECT m.*, c.name AS construct_name, f.name AS from_name "
        "FROM label_mapping m "
        "LEFT JOIN construct c ON c.id = m.construct_id "
        "LEFT JOIN construct f ON f.id = m.overridden_from "
        "ORDER BY m.n_uses DESC, m.label"
    ):
        sheet.append([
            row["label"], row["role"], row["construct_name"] or "",
            row["measured_as"], row["rationale"],
            "" if row["confidence"] is None else round(row["confidence"], 2),
            "yes" if row["contestable"] else "", row["contest_reason"],
            row["status"], row["from_name"] or "", row["override_reason"],
            row["decided_by"], row["n_uses"],
        ])
    _style(sheet, {1: 40, 4: 40, 5: 40, 8: 40, 11: 36})


def _sensitivity_sheet(sheet, project) -> None:
    sheet.append([
        "Analysis", "What it does", "Link", "Grade before", "Grade after",
        "Direction before", "Direction after", "Grade changed",
        "Sign changed", "Datasets before", "Datasets after",
    ])
    for row in project.db.query(
        "SELECT * FROM sensitivity_result ORDER BY analysis, link_key"
    ):
        sheet.append([
            row["analysis"].replace("_", " "), row["description"],
            row["link_key"], row["base_grade"], row["new_grade"],
            row["base_direction"], row["new_direction"],
            "yes" if row["changed_grade"] else "",
            "yes" if row["changed_sign"] else "",
            row["base_n_datasets"], row["n_datasets"],
        ])
    if sheet.max_row == 1:
        sheet.append([
            "—", "No link changed grade or sign under any analysis.",
        ])
    _style(sheet, {1: 24, 2: 46, 3: 34})


def _contradictions_sheet(sheet, project) -> None:
    sheet.append([
        "Link", "Explained by", "Explanation", "Suggests",
        "Suggested moderator", "Suggested split", "Positive datasets",
        "Negative datasets", "Null datasets", "Status", "Decided by",
        "Reason",
    ])
    for row in project.db.query("SELECT * FROM contradiction ORDER BY link_key"):
        supporting = json.loads(row["supporting"] or "{}")
        sheet.append([
            row["link_key"], row["explanation_type"], row["explanation"],
            row["suggestion_type"], row["suggested_moderator"],
            "; ".join(json.loads(row["suggested_split"] or "[]")),
            supporting.get("positive", ""), supporting.get("negative", ""),
            supporting.get("none", ""), row["status"], row["decided_by"],
            row["decision_reason"],
        ])
    if sheet.max_row == 1:
        sheet.append(["—", "", "No link is contested."])
    _style(sheet, {1: 34, 3: 60, 12: 36})


def write_graded_inventory(project, path: str | Path) -> Path:
    """Write the synthesis workbook. Returns where it went."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    book = Workbook()
    _inventory_sheet(book.active, project)
    book.active.title = "Inventory"
    _votes_sheet(book.create_sheet("Votes"), project)
    _codebook_sheet(book.create_sheet("Codebook"), project)
    _mapping_sheet(book.create_sheet("Mapping"), project)
    _sensitivity_sheet(book.create_sheet("Sensitivity"), project)
    _contradictions_sheet(book.create_sheet("Contradictions"), project)
    book.save(path)
    return path
