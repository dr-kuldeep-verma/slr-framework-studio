"""What the Gap Finder hands over: the register, the agenda, the draft.

Every number in all three is read from the database. The two that a reviewer
pastes straight into a manuscript -- the agenda and the Future Research draft
-- repeat the corpus qualifier at the top as well as inside each statement,
because a table lifted out of its document loses its context first.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.db import utc_now

__all__ = [
    "write_future_research_draft",
    "write_gap_register",
    "write_research_agenda",
]

_HEADERS = [
    "Gap id",
    "Type",
    "Statement",
    "Supporting evidence (counts)",
    "Records cited",
    "Freshness",
    "Freshness detail",
    "Priority",
    "Centrality",
    "Size",
    "Relevance",
    "Feasibility",
    "Scores overridden",
    "Override reason",
    "Status",
    "Status reason",
    "Research question",
    "Hypothesis",
    "Design",
    "Measures",
    "Population",
    "Setting",
    "Searched to",
]

#: How the seven types read in a heading.
_TYPE_LABELS = {
    "evidence": "Evidence gaps",
    "theoretical": "Theoretical gaps",
    "contradiction": "Contradictions left unresolved",
    "methodological": "Methodological gaps",
    "population_context": "Population and context gaps",
    "information": "Information and intervention gaps",
    "author_stated": "Gaps the authors state themselves",
}


def _gaps(project) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in project.db.query(
            "SELECT * FROM v_gap_register ORDER BY priority DESC, gap_type, id"
        )
    ]


def _records_for(project, gap_id: int) -> str:
    rows = project.db.query(
        "SELECT record_id, label FROM gap_evidence WHERE gap_id = ? "
        "ORDER BY id",
        (gap_id,),
    )
    parts = [
        str(row["record_id"]) if row["record_id"] else row["label"]
        for row in rows
        if row["record_id"] or row["label"]
    ]
    return ", ".join(parts)


def write_gap_register(project, path: str | Path) -> Path:
    """The register as a sheet, one row per gap."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Gap register"
    sheet.append(_HEADERS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top", wrap_text=True)

    for gap in _gaps(project):
        counts = gap["supporting_counts"] or "{}"
        try:
            readable = ", ".join(
                f"{k}={v}" for k, v in sorted(json.loads(counts).items())
            )
        except (TypeError, ValueError):
            readable = str(counts)
        detail = gap["freshness_detail"] or "{}"
        try:
            parsed = json.loads(detail)
            candidates = "; ".join(
                f"{c.get('title', '')} ({c.get('year')})"
                for c in parsed.get("candidates", [])
            )
            freshness_detail = candidates or parsed.get("detail", "")
        except (TypeError, ValueError):
            freshness_detail = str(detail)

        sheet.append(
            [
                gap["id"],
                gap["gap_type"],
                gap["statement"],
                readable,
                _records_for(project, gap["id"]),
                gap["freshness_status"],
                freshness_detail,
                gap["priority"],
                gap["score_centrality"],
                gap["score_size"],
                gap["score_relevance"],
                gap["score_feasibility"],
                gap["score_overrides"] if gap["score_overrides"] not in ("", "{}")
                else "",
                gap["override_reason"],
                gap["status"],
                gap["status_reason"],
                gap["suggested_question"],
                gap["suggested_hypothesis"],
                gap["suggested_design"],
                gap["suggested_measures"],
                gap["suggested_population"],
                gap["suggested_setting"],
                gap["corpus_searched_to"],
            ]
        )

    widths = {
        "A": 8, "B": 20, "C": 72, "D": 44, "E": 20, "F": 20, "G": 44,
        "H": 10, "Q": 54, "R": 54, "S": 40, "T": 30, "U": 30, "V": 24,
    }
    for letter, width in widths.items():
        sheet.column_dimensions[letter].width = width
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = (
        f"A1:{get_column_letter(len(_HEADERS))}{max(sheet.max_row, 1)}"
    )

    workbook.save(path)
    return path


def write_research_agenda(project, path: str | Path) -> Path:
    """The agenda as a Word table, one row per gap with a drafted study."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    gaps = [g for g in _gaps(project) if g["suggested_question"]]
    searched_to = (project.info().get("searched_to") or "").strip()

    document = Document()
    document.add_heading("Research agenda", level=1)
    document.add_paragraph(
        f"Drafted from the gap register of {project.info()['title']}. Every "
        "gap below is an absence within the included corpus, searched to "
        f"{searched_to or 'a date this project does not record'}; none is a "
        "claim about the literature as a whole. Each row was proposed by the "
        "application from the counts in the register and must be checked "
        "before use."
    )

    table = document.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    for index, heading in enumerate(
        ["Priority", "Gap", "Research question", "Hypothesis", "Design",
         "Population and setting"]
    ):
        cell = table.rows[0].cells[index]
        cell.text = heading
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True

    for gap in gaps:
        cells = table.add_row().cells
        cells[0].text = f"{gap['priority']:.2f}" if gap["priority"] else "-"
        cells[1].text = gap["statement"]
        cells[2].text = gap["suggested_question"]
        cells[3].text = gap["suggested_hypothesis"]
        cells[4].text = gap["suggested_design"]
        setting = gap["suggested_setting"]
        cells[5].text = (
            f"{gap['suggested_population']}"
            + (f"; {setting}" if setting else "")
        )

    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(9)

    document.add_paragraph()
    document.add_paragraph(
        f"Generated {utc_now()} from {len(gaps)} gap(s) with a drafted study."
    )
    document.save(str(path))
    return path


def write_future_research_draft(project, path: str | Path) -> Path:
    """A draft of the Future Research section, grouped by gap type."""
    from ..gaps import GapError

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    gaps = _gaps(project)
    if not gaps:
        raise GapError(
            "There are no gaps to write about. Run the gap analysis first; "
            "a Future Research section drafted from nothing would be a "
            "section of assertions."
        )

    searched_to = (project.info().get("searched_to") or "").strip()
    lines = [
        "# Future research (draft)",
        "",
        "This is a **draft** assembled from the gap register, not a finished "
        "section. Every statement below is quoted from the register with the "
        "counts that support it; nothing here was written by a language model "
        "except where a suggested study is marked as such.",
        "",
        f"Each absence is an absence *within the included corpus, searched to "
        f"{searched_to or '(no search date recorded)'}*. None of it is a claim "
        "about the literature as a whole, and it should not be rewritten into "
        "one.",
        "",
    ]

    by_type: dict[str, list[dict[str, Any]]] = {}
    for gap in gaps:
        by_type.setdefault(gap["gap_type"], []).append(gap)

    for gap_type, label in _TYPE_LABELS.items():
        found = by_type.get(gap_type)
        if not found:
            continue
        lines.append(f"## {label}")
        lines.append("")
        for gap in found[:12]:
            lines.append(f"- {gap['statement']}")
            if gap["freshness_status"] == "possibly_addressed":
                lines.append(
                    "  - Work published after the search date may already "
                    "address this; check before reporting it."
                )
            if gap["suggested_question"]:
                lines.append(
                    f"  - *Suggested study (drafted, unchecked):* "
                    f"{gap['suggested_question']}"
                )
        if len(found) > 12:
            lines.append(
                f"- …and {len(found) - 12} more of this kind in the register."
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"Drafted {utc_now()} from {len(gaps)} gap(s) in the register."
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
