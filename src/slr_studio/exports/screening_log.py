"""The Excel screening log.

One row per record and one column group per reviewer, which is the shape a
journal or a supervisor asks for when they want to check how screening was
done. Four sheets:

* **Screening log** — every record, every reviewer's decision, code, reason
  and quotation, and the settled outcome.
* **Agreement** — percentage agreement, Cohen's κ and PABAK for each
  comparison that was run.
* **Exclusions** — counts per exclusion code, matching the PRISMA box.
* **AI use** — which model answered what, how often, and what it cost. Read
  straight from `ai_call`, so it cannot be switched off (integrity rule 4).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

__all__ = ["write_screening_log"]

_HEADER_FILL = PatternFill("solid", fgColor="E5EDFF")
_HEADER_FONT = Font(bold=True)
_WRAP = Alignment(vertical="top", wrap_text=True)
_TOP = Alignment(vertical="top")

_ROLES = (
    ("pilot_human", "Pilot (human)"),
    ("screener_a", "Screener A"),
    ("screener_b", "Screener B"),
    ("adjudicator", "Adjudicator"),
    ("qc", "Quality control"),
    ("human", "Reviewer"),
)


def _style_header(sheet, row: int = 1) -> None:
    for cell in sheet[row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _WRAP
    # A coordinate string, not sheet.cell(): asking openpyxl for a cell
    # creates it, which pushes the next append() down and leaves a blank row.
    sheet.freeze_panes = f"A{row + 1}"


def _widths(sheet, widths: dict[int, int]) -> None:
    for index, width in widths.items():
        sheet.column_dimensions[get_column_letter(index)].width = width


def _screening_sheet(sheet, project) -> None:
    db = project.db
    headers = [
        "Record", "Title", "Authors", "Year", "Journal", "DOI", "Type",
        "Source database(s)", "Has abstract",
    ]
    for _, label in _ROLES:
        headers += [f"{label}: decision", f"{label}: code", f"{label}: reason",
                    f"{label}: quote"]
    headers += [
        "Final decision", "Final code", "Decided how", "Decided by",
        "Decided at", "Needs review", "Review reason",
    ]
    sheet.append(headers)
    _style_header(sheet)

    rows = db.query(
        "SELECT r.id, r.title, r.authors, r.year, r.journal, r.doi, r.doc_type, "
        "       r.has_abstract, "
        "       s.decision, s.exclusion_code, s.source, s.decided_by, s.decided_at, "
        "       s.needs_review, s.review_reason "
        "FROM record r LEFT JOIN screening_status s ON s.record_id = r.id "
        "WHERE r.active = 1 ORDER BY r.id"
    )
    decisions: dict[int, dict[str, Any]] = {}
    for row in db.query(
        "SELECT record_id, reviewer_role, decision, exclusion_code, reason, quote, "
        "       quote_verified FROM screening_decision ORDER BY id"
    ):
        decisions.setdefault(row["record_id"], {})[row["reviewer_role"]] = dict(row)

    sources: dict[int, list[str]] = {}
    for row in db.query(
        "SELECT record_id, source_database FROM record_source ORDER BY record_id"
    ):
        sources.setdefault(row["record_id"], []).append(row["source_database"])

    for record in rows:
        try:
            authors = json.loads(record["authors"] or "[]")
        except ValueError:
            authors = []
        line: list[Any] = [
            record["id"],
            record["title"],
            "; ".join(authors),
            record["year"],
            record["journal"],
            record["doi"] or "",
            record["doc_type"],
            "; ".join(sorted(set(sources.get(record["id"], [])))),
            "yes" if record["has_abstract"] else "no",
        ]
        by_role = decisions.get(record["id"], {})
        for role, _ in _ROLES:
            answer = by_role.get(role)
            if answer is None:
                line += ["", "", "", ""]
                continue
            quote = answer["quote"] or ""
            if quote and not answer["quote_verified"]:
                # Saying so in the cell matters more than a tidy column: an
                # unverified quotation is the signal that a decision may rest
                # on words the record does not contain.
                quote = f"{quote}  [NOT FOUND IN RECORD]"
            line += [answer["decision"], answer["exclusion_code"],
                     answer["reason"], quote]
        line += [
            record["decision"] or "not screened",
            record["exclusion_code"] or "",
            record["source"] or "",
            record["decided_by"] or "",
            (record["decided_at"] or "").replace("T", " ").replace("Z", ""),
            "yes" if record["needs_review"] else "",
            record["review_reason"] or "",
        ]
        sheet.append(line)

    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = _TOP
    _widths(sheet, {1: 8, 2: 52, 3: 28, 4: 7, 5: 26, 6: 24, 7: 16, 8: 22, 9: 12})
    for index in range(10, 10 + len(_ROLES) * 4):
        _widths(sheet, {index: 16 if index % 4 in (2, 3) else 40})


def _agreement_sheet(sheet, project) -> None:
    sheet.append([
        "Comparison", "Records", "Agreed", "Percentage agreement",
        "Cohen's κ", "PABAK", "Observed", "Expected by chance", "When", "Note",
    ])
    _style_header(sheet)
    for row in project.db.query("SELECT * FROM agreement_report ORDER BY id"):
        detail = json.loads(row["detail"] or "{}")
        sheet.append([
            row["scope"],
            row["n"],
            row["n_agree"],
            row["percent_agreement"],
            row["kappa"],
            row["pabak"],
            row["p_observed"],
            row["p_expected"],
            (row["created_at"] or "").replace("T", " ").replace("Z", ""),
            detail.get("note", ""),
        ])
    for row in sheet.iter_rows(min_row=2, min_col=4, max_col=8):
        for cell in row:
            cell.number_format = "0.000"
    _widths(sheet, {1: 26, 2: 10, 3: 10, 4: 20, 5: 12, 6: 12, 7: 12, 8: 18, 9: 20, 10: 60})


def _exclusions_sheet(sheet, project) -> None:
    sheet.append(["Code", "Criterion", "Records excluded"])
    _style_header(sheet)
    for row in project.db.query(
        "SELECT * FROM v_exclusion_reasons ORDER BY n DESC, exclusion_code"
    ):
        sheet.append([row["exclusion_code"], row["label"], row["n"]])
    counts = project.db.query_one("SELECT * FROM v_prisma_screening")
    if counts:
        sheet.append([])
        sheet.append(["Records screened", "", counts["n_to_screen"]])
        sheet.append(["Included", "", counts["n_included"]])
        sheet.append(["Excluded", "", counts["n_excluded"]])
        sheet.append(["Unclear", "", counts["n_unclear"]])
        sheet.append(["Not yet screened", "", counts["n_not_yet_screened"]])
        sheet.append(["Awaiting human review", "", counts["n_awaiting_review"]])
    _widths(sheet, {1: 14, 2: 46, 3: 18})


def _ai_sheet(sheet, project) -> None:
    sheet.append([
        "Stage", "Task", "Provider", "Model", "Temperature", "Calls",
        "From cache", "Prompt tokens", "Completion tokens", "Cost (USD)",
        "First used", "Last used",
    ])
    _style_header(sheet)
    for row in project.db.query(
        "SELECT stage, task, provider, model, temperature, COUNT(*) AS n, "
        "       SUM(cached) AS n_cached, SUM(prompt_tokens) AS prompt_tokens, "
        "       SUM(completion_tokens) AS completion_tokens, "
        "       SUM(cost_usd) AS cost_usd, MIN(created_at) AS first_used, "
        "       MAX(created_at) AS last_used "
        "FROM ai_call GROUP BY stage, task, provider, model, temperature "
        "ORDER BY first_used"
    ):
        sheet.append([
            row["stage"], row["task"], row["provider"], row["model"],
            row["temperature"], row["n"], row["n_cached"] or 0,
            row["prompt_tokens"] or 0, row["completion_tokens"] or 0,
            round(row["cost_usd"] or 0.0, 4),
            (row["first_used"] or "").replace("T", " ").replace("Z", ""),
            (row["last_used"] or "").replace("T", " ").replace("Z", ""),
        ])
    overrides = project.db.scalar(
        "SELECT COUNT(*) FROM human_override WHERE stage = 'screen'"
    )
    sheet.append([])
    sheet.append(["Human overrides of an AI screening decision", "", overrides or 0])
    _widths(sheet, {1: 12, 2: 20, 3: 14, 4: 26, 5: 12, 6: 8, 7: 12, 8: 14, 9: 18,
                    10: 12, 11: 20, 12: 20})


def write_screening_log(project, path: str | Path | None = None) -> Path:
    """Write the log and return where it went."""
    path = Path(path) if path else project.exports_dir / "screening-log.xlsx"
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "Screening log"
    _screening_sheet(sheet, project)
    _agreement_sheet(workbook.create_sheet("Agreement"), project)
    _exclusions_sheet(workbook.create_sheet("Exclusions"), project)
    _ai_sheet(workbook.create_sheet("AI use"), project)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    project.audit.log(
        "screen",
        "screening_log_exported",
        detail={
            "path": path.name,
            "n_records": int(
                project.db.scalar("SELECT COUNT(*) FROM record WHERE active = 1") or 0
            ),
        },
    )
    return path
