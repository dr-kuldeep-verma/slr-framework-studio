"""Everything Section 12 lists, written in one go.

Three decisions shape this.

**A stage the review has not reached is skipped, not written empty.** An
empty graded inventory sitting in the folder reads as a finding -- as though
the synthesis ran and found nothing -- and a reviewer who sends that folder to
a supervisor has said something untrue without meaning to. The manifest says
what was skipped and why.

**One failure does not lose the rest.** Each item is written independently; a
writer that falls over costs the reviewer that one file and a line in the
manifest, not the whole export.

**The manifest is part of the export.** It lists what was written, what was
skipped, what failed, and whether the audit chain verified, so the folder
explains itself months later without this application.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..core.db import utc_now
from .documents import write_synthesis_tables
from .extraction_matrix import write_extraction_matrix
from .gap_register import (
    write_future_research_draft,
    write_gap_register,
    write_research_agenda,
)
from .graded_inventory import write_graded_inventory
from .methods import write_audit_log, write_methods_draft
from .prisma import write_prisma_checklist, write_prisma_diagram
from .refi import write_refi_project
from .ris import write_ris
from .screening_log import write_screening_log

__all__ = ["BundleOutcome", "Item", "SECTION_12", "export_everything"]


@dataclass(frozen=True)
class Item:
    """One thing on Section 12's list."""

    kind: str
    filename: str
    #: What has to exist in the project before this can be written.
    needs: str
    description: str


#: Section 12's list, as a checklist against the code. The `needs` clause is
#: a SQL condition; an item whose condition is not met is skipped.
SECTION_12: tuple[Item, ...] = (
    Item("prisma_diagram", "prisma-flow.docx",
         "SELECT COUNT(*) FROM record",
         "PRISMA 2020 flow diagram (Word)"),
    Item("prisma_diagram_pdf", "prisma-flow.pdf",
         "SELECT COUNT(*) FROM record",
         "PRISMA 2020 flow diagram (PDF)"),
    Item("prisma_checklist", "prisma-checklist.docx",
         "SELECT COUNT(*) FROM project",
         "PRISMA 2020 checklist, pre-filled where the project can answer"),
    Item("screening_log", "screening-log.xlsx",
         "SELECT COUNT(*) FROM screening_status",
         "Screening log (Excel)"),
    Item("extraction_matrix", "extraction-matrix.xlsx",
         "SELECT COUNT(*) FROM report_extraction",
         "Extraction matrix (Excel)"),
    Item("ris", "screened-in.ris",
         "SELECT COUNT(*) FROM screening_status",
         "RIS of the records that passed screening"),
    Item("ris_included", "included.ris",
         "SELECT COUNT(*) FROM fulltext_status WHERE decision = 'include'",
         "RIS of the included studies"),
    Item("refi", "project.qdpx",
         "SELECT COUNT(*) FROM fulltext_status WHERE decision = 'include'",
         "REFI-QDA project with the codings"),
    Item("graded_inventory", "graded-inventory.xlsx",
         "SELECT COUNT(*) FROM graded_link",
         "Graded inventory, sensitivity and contradictions (Excel)"),
    Item("synthesis_tables", "synthesis-tables.docx",
         "SELECT COUNT(*) FROM graded_link",
         "The same three tables (Word)"),
    Item("gap_register", "gap-register.xlsx",
         "SELECT COUNT(*) FROM gap_register",
         "Gap register (Excel)"),
    Item("research_agenda", "research-agenda.docx",
         "SELECT COUNT(*) FROM gap_register WHERE suggested_question <> ''",
         "Research agenda (Word)"),
    Item("future_research", "future-research-draft.md",
         "SELECT COUNT(*) FROM gap_register",
         "Future Research section (draft)"),
    Item("figures", "figures",
         "SELECT COUNT(*) FROM figure",
         "Figures (SVG, PNG, PPTX and PDF)"),
    Item("methods", "methods-draft.docx",
         "SELECT COUNT(*) FROM project",
         "Methods draft, with the AI-use statement"),
    Item("audit_log", "audit-log.xlsx",
         "SELECT COUNT(*) FROM audit_log",
         "The full audit log, with its verification and every AI call"),
)


@dataclass
class BundleOutcome:
    folder: Path
    written: list[Path] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def describe(self) -> str:
        lines = [
            f"{len(self.written)} file(s) written to {self.folder}."
        ]
        if self.skipped:
            lines.append(f"  {len(self.skipped)} item(s) skipped:")
            lines.extend(
                f"    {kind}: {reason}" for kind, reason in self.skipped.items()
            )
        if self.failed:
            lines.append(f"  {len(self.failed)} item(s) failed:")
            lines.extend(
                f"    {kind}: {reason}" for kind, reason in self.failed.items()
            )
        return "\n".join(lines)


def _writers(project, folder: Path) -> dict[str, Callable[[], Any]]:
    return {
        "prisma_diagram": lambda: write_prisma_diagram(
            project, folder / "prisma-flow.docx"
        ),
        "prisma_diagram_pdf": lambda: write_prisma_diagram(
            project, folder / "prisma-flow.pdf", fmt="pdf"
        ),
        "prisma_checklist": lambda: write_prisma_checklist(
            project, folder / "prisma-checklist.docx"
        ),
        "screening_log": lambda: write_screening_log(
            project, folder / "screening-log.xlsx"
        ),
        "extraction_matrix": lambda: write_extraction_matrix(
            project, folder / "extraction-matrix.xlsx"
        ),
        "ris": lambda: write_ris(
            project, folder / "screened-in.ris", stage="screened_in"
        ),
        "ris_included": lambda: write_ris(
            project, folder / "included.ris", stage="included"
        ),
        "refi": lambda: write_refi_project(project, folder / "project.qdpx"),
        "graded_inventory": lambda: write_graded_inventory(
            project, folder / "graded-inventory.xlsx"
        ),
        "synthesis_tables": lambda: write_synthesis_tables(
            project, folder / "synthesis-tables.docx"
        ),
        "gap_register": lambda: write_gap_register(
            project, folder / "gap-register.xlsx"
        ),
        "research_agenda": lambda: write_research_agenda(
            project, folder / "research-agenda.docx"
        ),
        "future_research": lambda: write_future_research_draft(
            project, folder / "future-research-draft.md"
        ),
        "figures": lambda: _existing_figures(project),
        "methods": lambda: write_methods_draft(
            project, folder / "methods-draft.docx"
        ),
        "audit_log": lambda: write_audit_log(
            project, folder / "audit-log.xlsx"
        ),
    }


def _existing_figures(project) -> list[Path]:
    """The figures the framework and gap stages already drew.

    Not redrawn here: redrawing would re-run a layout the reviewer may have
    already looked at, and the files on disk are what the `figure` table is a
    record of.
    """
    directory = project.exports_dir / "figures"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if path.is_file())


def export_everything(project, folder: str | Path | None = None) -> BundleOutcome:
    """Write every item on Section 12's list that this project can support."""
    folder = Path(folder) if folder else project.exports_dir
    folder.mkdir(parents=True, exist_ok=True)
    outcome = BundleOutcome(folder=folder)
    writers = _writers(project, folder)

    for item in SECTION_12:
        try:
            available = project.db.scalar(item.needs) or 0
        except Exception:
            available = 0
        if not available:
            outcome.skipped[item.kind] = (
                f"{item.description}: the review has not reached this stage, "
                "so nothing was written rather than an empty file."
            )
            continue
        try:
            result = writers[item.kind]()
        except Exception as exc:
            outcome.failed[item.kind] = f"{exc}"
            traceback.clear_frames(exc.__traceback__) if exc.__traceback__ else None
            continue
        if isinstance(result, list):
            outcome.written.extend(result)
        elif result is not None:
            outcome.written.append(Path(result))

    outcome.written.append(_write_manifest(project, folder, outcome))
    return outcome


def _write_manifest(project, folder: Path, outcome: BundleOutcome) -> Path:
    """A plain-text account of the export, so the folder explains itself."""
    problems = project.audit.verify()
    info = project.info()
    lines = [
        f"# Export — {info['title']}",
        "",
        f"Written {utc_now()} by SLR Framework Studio.",
        "",
        "## Audit",
        "",
        (
            "The audit chain is intact: every entry's hash matches its "
            "content and the entry before it."
            if not problems
            else f"The audit chain is BROKEN at {len(problems)} point(s). "
                 "Do not rely on this export until that is explained."
        ),
        "",
        "## Files",
        "",
    ]
    for path in sorted(outcome.written, key=lambda p: p.name):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        lines.append(f"- `{path.name}` ({size:,} bytes)")

    if outcome.skipped:
        lines.extend(["", "## Not written", ""])
        lines.extend(f"- {reason}" for reason in outcome.skipped.values())
    if outcome.failed:
        lines.extend(["", "## Failed", ""])
        lines.extend(
            f"- {kind}: {reason}" for kind, reason in outcome.failed.items()
        )

    lines.extend([
        "",
        "## What these are",
        "",
        "Every number in every file here is read from the project database. "
        "The Methods draft and the Future Research draft are drafts: check "
        "them and rewrite them in your own voice. The AI-use statement in the "
        "Methods draft is generated from the call log and cannot be switched "
        "off.",
        "",
    ])

    path = folder / "manifest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
