"""The PRISMA 2020 flow diagram and checklist.

The flow diagram's numbers are read from the three PRISMA views and cannot be
supplied by a caller. There is no argument for overriding one, because a
reviewer who could edit a box could publish a diagram that disagrees with
their own data, and the diagram is the first thing a referee checks against
the text.

The checklist is a different kind of document. Of its twenty-seven items the
application can answer perhaps half from what it recorded; the rest are about
things it cannot see -- whether the protocol was registered, how certainty was
assessed, who funded the work. Those are marked as the reviewer's to complete.
A checklist that claims an item was done because the software guessed is worse
than one that leaves the item blank.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "CHECKLIST_ITEMS",
    "ChecklistItem",
    "PrismaCounts",
    "read_counts",
    "write_prisma_checklist",
    "write_prisma_diagram",
]

#: What the reviewer has to answer themselves.
_FOR_THE_REVIEWER = "For the reviewer to complete."


@dataclass(frozen=True)
class PrismaCounts:
    """Every number in the flow diagram, read from the views."""

    n_identified: int = 0
    n_duplicates_removed: int = 0
    n_screened: int = 0
    n_excluded_screening: int = 0
    n_not_yet_screened: int = 0
    n_sought: int = 0
    n_retrieved: int = 0
    n_not_retrieved: int = 0
    n_assessed: int = 0
    n_excluded_fulltext: int = 0
    n_fulltext_undecided: int = 0
    n_included: int = 0
    databases: tuple[tuple[str, int], ...] = ()
    exclusion_reasons: tuple[tuple[str, int], ...] = ()
    finished: bool = False


def read_counts(project) -> PrismaCounts:
    """The flow, as the database has it.

    `n_sought` is the records that passed screening: PRISMA counts those as
    sought for retrieval whether or not a PDF has been found yet, so it is
    derived from the screening view rather than from how many files happen to
    be on disk.
    """
    identification = project.db.query_one("SELECT * FROM v_prisma_identification")
    screening = project.db.query_one("SELECT * FROM v_prisma_screening")
    fulltext = project.db.query_one("SELECT * FROM v_prisma_fulltext")

    identification = dict(identification) if identification else {}
    screening = dict(screening) if screening else {}
    fulltext = dict(fulltext) if fulltext else {}

    databases = tuple(
        (row["source_database"], row["n"])
        for row in project.db.query(
            "SELECT source_database, COUNT(*) AS n FROM record "
            "GROUP BY source_database ORDER BY source_database"
        )
    )
    # The final decision lives in fulltext_status, which is what the PRISMA
    # view counts; reading the reasons from anywhere else could disagree with
    # the total sitting next to them.
    reasons = tuple(
        (row["reason"], row["n"])
        for row in project.db.query(
            "SELECT COALESCE(NULLIF(exclusion_code, ''), 'not stated') AS reason, "
            "COUNT(*) AS n FROM fulltext_status WHERE decision = 'exclude' "
            "GROUP BY reason ORDER BY n DESC, reason"
        )
    )

    n_screened = identification.get("n_screened", 0)
    # PRISMA counts a record as sought for retrieval once screening passed it
    # on, whether or not a file has been found yet.
    n_sought = fulltext.get("n_sought", 0)
    n_not_yet = screening.get("n_not_yet_screened", 0)
    n_assessed = fulltext.get("n_assessed", 0)
    n_included = fulltext.get("n_included", 0)
    n_excluded_fulltext = fulltext.get("n_excluded", 0)
    n_retrieved = fulltext.get("n_retrieved", 0)

    return PrismaCounts(
        n_identified=identification.get("n_identified", 0),
        n_duplicates_removed=identification.get("n_duplicates_removed", 0),
        n_screened=n_screened,
        n_excluded_screening=screening.get("n_excluded", 0),
        n_not_yet_screened=n_not_yet,
        n_sought=n_sought,
        n_retrieved=n_retrieved,
        # Derived so that the flow adds up. In an unfinished review this
        # includes reports retrieval has not been attempted for yet, which is
        # why the diagram carries a line saying the review is unfinished.
        n_not_retrieved=n_sought - n_retrieved,
        n_assessed=n_assessed,
        n_excluded_fulltext=n_excluded_fulltext,
        n_fulltext_undecided=n_assessed - n_included - n_excluded_fulltext,
        n_included=n_included,
        databases=databases,
        exclusion_reasons=reasons,
        # A review is finished when nothing is still waiting at a stage. An
        # unfinished one still gets a diagram, with a line saying so.
        finished=bool(n_screened) and not n_not_yet and n_assessed > 0,
    )


# -- the flow diagram -------------------------------------------------------


def _boxes(counts: PrismaCounts) -> list[tuple[str, list[str]]]:
    """The diagram as a column of boxes, left side then right side."""
    sources = "; ".join(
        f"{name or 'unnamed'} (n = {n})" for name, n in counts.databases
    )
    reasons = "; ".join(
        f"{reason} (n = {n})" for reason, n in counts.exclusion_reasons
    ) or "no full-text exclusions recorded"
    return [
        (
            "Identification",
            [
                f"Records identified from databases (n = {counts.n_identified})",
                f"Databases searched: {sources or 'none recorded'}",
                f"Records removed before screening: duplicates removed "
                f"(n = {counts.n_duplicates_removed})",
            ],
        ),
        (
            "Screening",
            [
                f"Records screened (n = {counts.n_screened})",
                f"Records excluded (n = {counts.n_excluded_screening})",
                f"Reports sought for retrieval (n = {counts.n_sought})",
                f"Reports not retrieved (n = {counts.n_not_retrieved})",
                f"Reports assessed for eligibility (n = {counts.n_assessed})",
                f"Reports excluded, with reasons "
                f"(n = {counts.n_excluded_fulltext}): {reasons}",
            ],
        ),
        (
            "Included",
            [
                f"Studies included in review (n = {counts.n_included})",
            ],
        ),
    ]


def _unfinished_note(counts: PrismaCounts) -> str:
    waiting = []
    if counts.n_not_yet_screened:
        waiting.append(f"{counts.n_not_yet_screened} record(s) not yet screened")
    if counts.n_fulltext_undecided:
        waiting.append(
            f"{counts.n_fulltext_undecided} report(s) with no final full-text "
            "decision"
        )
    if not waiting:
        return ""
    return (
        "This review is not finished: "
        + ", ".join(waiting)
        + ". The counts below are the flow as it stands, not a final diagram."
    )


def write_prisma_diagram(project, path: str | Path, fmt: str = "") -> Path:
    """The flow diagram, as a Word file or a PDF."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    chosen = (fmt or path.suffix.lstrip(".")).casefold()
    counts = read_counts(project)
    if chosen == "pdf":
        return _diagram_pdf(project, counts, path)
    return _diagram_docx(project, counts, path)


def _diagram_docx(project, counts: PrismaCounts, path: Path) -> Path:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()
    document.add_heading("PRISMA 2020 flow diagram", level=1)
    document.add_paragraph(project.info()["title"])
    note = _unfinished_note(counts)
    if note:
        document.add_paragraph(note)
    document.add_paragraph(
        "Every number below is read from this project's database. None of it "
        "can be typed in, so the diagram and the data cannot disagree."
    )

    for stage, lines in _boxes(counts):
        document.add_heading(stage, level=2)
        table = document.add_table(rows=0, cols=1)
        table.style = "Table Grid"
        for line in lines:
            cell = table.add_row().cells[0]
            cell.text = line
            for paragraph in cell.paragraphs:
                paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                for run in paragraph.runs:
                    run.font.size = Pt(10)

    document.save(str(path))
    return path


def _diagram_pdf(project, counts: PrismaCounts, path: Path) -> Path:
    """The same flow, drawn as boxes and arrows.

    Drawn rather than converted: there is no SVG-to-PDF converter in the
    packaged application, and a PDF made by printing a Word file would need
    Word.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as pdfcanvas

    width, height = A4
    page = pdfcanvas.Canvas(str(path), pagesize=A4)
    page.setTitle(f"PRISMA 2020 flow diagram — {project.info()['title']}")

    margin = 18 * mm
    y = height - margin
    page.setFont("Helvetica-Bold", 14)
    page.drawString(margin, y, "PRISMA 2020 flow diagram")
    y -= 7 * mm
    page.setFont("Helvetica", 9)
    page.drawString(margin, y, project.info()["title"][:110])
    y -= 6 * mm
    note = _unfinished_note(counts)
    if note:
        for line in _wrap(note, 110):
            page.drawString(margin, y, line)
            y -= 4.5 * mm
    y -= 2 * mm

    box_width = width - 2 * margin
    for stage, lines in _boxes(counts):
        page.setFont("Helvetica-Bold", 10)
        page.drawString(margin, y, stage)
        y -= 5 * mm
        page.setFont("Helvetica", 9)
        for line in lines:
            wrapped = _wrap(line, 104)
            box_height = 4.6 * mm * len(wrapped) + 3 * mm
            page.setStrokeColorRGB(0.55, 0.6, 0.66)
            page.rect(margin, y - box_height, box_width, box_height)
            text_y = y - 5 * mm
            for part in wrapped:
                page.drawString(margin + 3 * mm, text_y, part)
                text_y -= 4.6 * mm
            y -= box_height + 3 * mm
            if y < margin + 30 * mm:
                page.showPage()
                y = height - margin
                page.setFont("Helvetica", 9)
        y -= 3 * mm

    page.setFont("Helvetica-Oblique", 8)
    page.drawString(
        margin, margin,
        "Every number is read from the project database; none can be typed in.",
    )
    page.showPage()
    page.save()
    return path


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


# -- the checklist ------------------------------------------------------------


@dataclass(frozen=True)
class ChecklistItem:
    number: str
    section: str
    item: str
    #: How the project answers it, or empty when only the reviewer can.
    source: str = ""


#: PRISMA 2020's twenty-seven items. `source` names the attribute of the
#: answer dictionary that fills it in; an item with none is the reviewer's.
CHECKLIST_ITEMS: tuple[ChecklistItem, ...] = (
    ChecklistItem("1", "Title", "Identify the report as a systematic review.",
                  "title"),
    ChecklistItem("2", "Abstract", "See the PRISMA 2020 for Abstracts checklist."),
    ChecklistItem("3", "Introduction",
                  "Describe the rationale for the review."),
    ChecklistItem("4", "Introduction",
                  "Provide an explicit statement of the objective(s).",
                  "objectives"),
    ChecklistItem("5", "Methods",
                  "Specify the inclusion and exclusion criteria.",
                  "criteria"),
    ChecklistItem("6", "Methods",
                  "Specify all databases searched and the date last searched.",
                  "sources"),
    ChecklistItem("7", "Methods",
                  "Present the full search strategy for all databases.",
                  "search_strategy"),
    ChecklistItem("8", "Methods",
                  "Specify the methods used to decide whether a study met "
                  "the criteria, including how many reviewers screened.",
                  "screening_method"),
    ChecklistItem("9", "Methods",
                  "Specify the methods used to collect data from reports, "
                  "including how many reviewers.",
                  "extraction_method"),
    ChecklistItem("10", "Methods",
                  "List and define all outcomes and other variables sought.",
                  "variables"),
    ChecklistItem("11", "Methods",
                  "Specify the methods used to assess risk of bias."),
    ChecklistItem("12", "Methods",
                  "Specify for each outcome the effect measure used."),
    ChecklistItem("13", "Methods",
                  "Describe the processes used to synthesise results.",
                  "synthesis_method"),
    ChecklistItem("14", "Methods",
                  "Describe any methods to assess risk of bias due to "
                  "missing results."),
    ChecklistItem("15", "Methods",
                  "Describe any methods used to assess certainty in the "
                  "body of evidence.",
                  "certainty_method"),
    ChecklistItem("16", "Results",
                  "Describe the results of the search and selection process.",
                  "flow"),
    ChecklistItem("17", "Results",
                  "Cite studies that met the criteria but were excluded, and "
                  "explain why.",
                  "excluded_with_reasons"),
    ChecklistItem("18", "Results",
                  "Present the characteristics of each included study.",
                  "characteristics"),
    ChecklistItem("19", "Results",
                  "Present assessments of risk of bias for each study."),
    ChecklistItem("20", "Results",
                  "Present results of all syntheses.",
                  "syntheses"),
    ChecklistItem("21", "Results",
                  "Present assessments of risk of bias due to missing results."),
    ChecklistItem("22", "Results",
                  "Present assessments of certainty in the body of evidence.",
                  "certainty_results"),
    ChecklistItem("23", "Discussion",
                  "Provide a general interpretation of the results, their "
                  "limitations, and implications."),
    ChecklistItem("24", "Other",
                  "Provide registration information, or state that the review "
                  "was not registered.",
                  "registration"),
    ChecklistItem("25", "Other",
                  "Describe any support received and the role of the funders."),
    ChecklistItem("26", "Other",
                  "Declare any competing interests."),
    ChecklistItem("27", "Other",
                  "Report which of the data, code and other materials are "
                  "publicly available.",
                  "availability"),
)


def _answers(project) -> dict[str, str]:
    """What the project can say about itself, item by item."""
    info = project.info()
    counts = read_counts(project)
    db = project.db

    searched = ", ".join(name or "unnamed" for name, _ in counts.databases)
    searched_to = (info.get("searched_to") or "").strip()
    protocol = project.protocol.current()

    n_coders = db.scalar(
        "SELECT COUNT(DISTINCT coder_role) FROM report_extraction"
    ) or 0
    constructs = db.scalar("SELECT COUNT(*) FROM construct") or 0
    graded = db.scalar("SELECT COUNT(*) FROM graded_link") or 0
    sensitivity = db.scalar(
        "SELECT COUNT(DISTINCT analysis) FROM sensitivity_result"
    ) or 0

    answers = {
        "title": f"Recorded as the project title: {info['title']!r}.",
        "objectives": (info.get("objectives") or "").strip(),
        "criteria": " / ".join(
            part for part in (
                (info.get("inclusion_criteria") or "").strip(),
                (info.get("exclusion_criteria") or "").strip(),
            ) if part
        ),
        "sources": (
            f"Databases searched: {searched or 'none recorded'}."
            + (f" Last searched {searched_to}." if searched_to
               else " The project does not record the date last searched.")
        ),
        "search_strategy": (info.get("search_strategy") or "").strip(),
        "screening_method": (
            "Two independent AI screeners with differing stances, blinded to "
            "each other, with a third adjudication call on disagreement and "
            "human confirmation of every conflict, unclear record, "
            "quality-control rescue and random audit. See the screening log."
        ) if db.scalar("SELECT COUNT(*) FROM screening_decision") else "",
        "extraction_method": (
            f"Extraction by {n_coders} coder role(s), with a blind direction "
            "check on every recorded relationship and a second coder on a "
            "sample. See the extraction matrix."
        ) if n_coders else "",
        "variables": (
            f"{constructs} construct(s) in the approved codebook; see the "
            "codebook and the extraction matrix."
        ) if constructs else "",
        "synthesis_method": (
            "Vote counting at dataset level over harmonised directions, with "
            "grades applied in a fixed precedence and a two-sided exact sign "
            "test; every link reports a Wilson 95% interval. See the graded "
            "inventory."
        ) if graded else "",
        "certainty_method": (
            "Evidence graded as asserted, contested, strong, moderate, "
            "emerging or tentative by the rules recorded with the inventory."
        ) if graded else "",
        "flow": (
            f"{counts.n_identified} identified, "
            f"{counts.n_duplicates_removed} duplicates removed, "
            f"{counts.n_screened} screened, {counts.n_included} included. "
            "See the PRISMA flow diagram."
        ),
        "excluded_with_reasons": (
            "; ".join(
                f"{reason} (n = {n})" for reason, n in counts.exclusion_reasons
            )
        ),
        "characteristics": (
            "See the extraction matrix, one row per included report."
        ) if db.scalar("SELECT COUNT(*) FROM report_extraction") else "",
        "syntheses": (
            f"{graded} graded link(s); see the graded inventory."
            + (f" {sensitivity} sensitivity analysis(es) reported."
               if sensitivity else "")
        ) if graded else "",
        "certainty_results": (
            "Each link's grade, counts, interval and sign-test p-value are in "
            "the graded inventory."
        ) if graded else "",
        "registration": (
            f"Protocol locked in this project on {protocol.locked_at}, "
            f"version {protocol.version}, hash {protocol.content_hash[:12]}. "
            "This is not a public registration; record the registry and number "
            "if the review was registered."
        ) if protocol else "",
        "availability": (
            "The project database holds every record, decision, quotation and "
            "AI call; the audit log is exported with this checklist. State "
            "which of these will be shared."
        ),
    }
    return {key: value for key, value in answers.items() if value}


def write_prisma_checklist(project, path: str | Path) -> Path:
    """The 27-item checklist, pre-filled where the project can answer."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    answers = _answers(project)

    document = Document()
    document.add_heading("PRISMA 2020 checklist", level=1)
    document.add_paragraph(project.info()["title"])
    document.add_paragraph(
        "Items this project can answer are filled in from what it recorded. "
        "The rest are marked for the reviewer: the application does not know "
        "whether the review was registered, how it was funded or how risk of "
        "bias was assessed, and a checklist that claimed otherwise would be "
        "worse than a blank one."
    )

    table = document.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for index, heading in enumerate(
        ["#", "Section", "Item", "Where it is reported"]
    ):
        cell = table.rows[0].cells[index]
        cell.text = heading
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.bold = True

    for item in CHECKLIST_ITEMS:
        cells = table.add_row().cells
        cells[0].text = item.number
        cells[1].text = item.section
        cells[2].text = item.item
        cells[3].text = answers.get(item.source, "") or _FOR_THE_REVIEWER

    for row in table.rows:
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(8.5)

    document.save(str(path))
    return path
