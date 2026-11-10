"""What the reviewer should do next, worked out from the review itself.

A review has eleven stages and, inside them, more than thirty actions that
must happen in order. A new user stalls between them: "the pilot compares
your own judgements with the AI's", but where are the records to judge?
This module reads the project's state and returns the one next step, the
page it happens on, and how to do it in a few plain sentences.

It only reads. It never decides anything and never calls a model, so it is
cheap enough to run after every change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = ["NextStep", "next_step", "rapid_unclear_count"]

#: A human decision made this soon after the previous one was not read.
RAPID_SECONDS = 6
#: Default pilot size (ScreeningSettings.pilot_size).
PILOT_SIZE = 60


@dataclass
class NextStep:
    page: str  # navigator title, e.g. "Screen"
    headline: str  # one line, starting with a verb
    why: str = ""  # one sentence on why this comes now
    steps: list[str] = field(default_factory=list)
    progress: str = ""  # e.g. "24 of 60 labelled"
    done: bool = False  # the review is complete
    focus: str = ""  # which part of the page to open, e.g. "pilot", "revisit"

    def as_text(self) -> str:
        lines = [self.headline]
        if self.progress:
            lines.append(f"({self.progress})")
        if self.why:
            lines += ["", self.why]
        if self.steps:
            lines.append("")
            lines += [f"{i}. {s}" for i, s in enumerate(self.steps, 1)]
        return "\n".join(lines)


def _one(db, sql: str, params: tuple = ()) -> int:
    try:
        return int(db.scalar(sql, params) or 0)
    except Exception:
        return 0


def _row(db, view: str) -> dict[str, Any]:
    try:
        row = db.query_one(f"SELECT * FROM {view}")
        return dict(row) if row else {}
    except Exception:
        return {}


def rapid_unclear_count(db) -> int:
    """Records the reviewer left "unclear" seconds after the previous decision.

    Their current screening status is still that human "unclear", so they
    have not been decided again since.
    """
    sql = f"""
        WITH h AS (
            SELECT record_id, decision, created_at,
                   LAG(created_at) OVER (ORDER BY created_at, id) AS prev
            FROM screening_decision WHERE reviewer_role = 'human'
        )
        SELECT COUNT(*) FROM h
        JOIN screening_status s ON s.record_id = h.record_id
        WHERE h.decision = 'unclear' AND s.source = 'human' AND s.decision = 'unclear'
          AND h.prev IS NOT NULL
          AND (julianday(h.created_at) - julianday(h.prev)) * 86400 < {RAPID_SECONDS}
    """
    return _one(db, sql)


def next_step(project) -> NextStep:
    """The single next thing to do on this review."""
    db = project.db

    # -- protocol ----------------------------------------------------------
    if not project.protocol.is_locked():
        return NextStep(
            "Project",
            "Lock the protocol",
            "Screening and every later decision are checked against a fixed "
            "protocol, so it is locked before any record is screened.",
            [
                "Fill in the review's title, aims, research questions, "
                "framework and eligibility criteria on this page.",
                "Press Save protocol fields.",
                "Press Draft and lock protocol, read the draft, and confirm. "
                "Later changes are recorded as amendments.",
            ],
        )

    # -- records -----------------------------------------------------------
    ident = _row(db, "v_prisma_identification")
    if not ident.get("n_identified"):
        return NextStep(
            "Import",
            "Import your search results",
            "Nothing has been imported yet.",
            [
                "Export the search results from each database as RIS, "
                "BibTeX, CSV or PubMed/MEDLINE text, with abstracts.",
                "Drag the export files onto the Import page. Each file is "
                "archived unchanged and every record keeps its source.",
            ],
        )

    # -- duplicates --------------------------------------------------------
    if not _one(db, "SELECT COUNT(*) FROM dedup_run"):
        return NextStep(
            "Deduplicate",
            "Remove duplicate records",
            "The same study often comes from more than one database.",
            ["Press Run deduplication.",
             "Decide any pairs it is unsure about: same study, or different."],
        )
    n_uncertain = _one(db, "SELECT COUNT(*) FROM dedup_pair WHERE status = 'uncertain'")
    screening_started = _one(db, "SELECT COUNT(*) FROM screening_status")
    # Possible duplicates come first only before screening: once it has
    # started, an undecided pair simply stays as two records.
    if n_uncertain and not screening_started:
        return NextStep(
            "Deduplicate",
            "Decide the possible duplicates",
            progress=f"{n_uncertain} pair{'s' if n_uncertain != 1 else ''} left",
            steps=["Open the Needs review tab and pick a pair.",
                   "Press Same study — merge, or Different studies — keep both."],
        )

    # -- screening -----------------------------------------------------------
    n_to_screen = int(ident.get("n_screened") or 0)
    pilot_done = _one(db, "SELECT COUNT(*) FROM screening_run WHERE kind = 'pilot' "
                          "AND status = 'finished'")
    pilot_waived = _one(db, "SELECT COUNT(*) FROM screening_waiver")
    if not pilot_done and not pilot_waived:
        target = min(PILOT_SIZE, n_to_screen) or PILOT_SIZE
        labelled = _one(db, "SELECT COUNT(*) FROM screening_decision "
                            "WHERE reviewer_role = 'pilot_human'")
        if labelled < target:
            return NextStep(
                "Screen",
                "Label the pilot records yourself",
                "The pilot checks the AI against your own judgement before it "
                "screens everything.",
                [
                    "On the Screen page, open the Pilot tab.",
                    "Click a record in the list on the left and read its title "
                    "and abstract.",
                    "Under Your judgement, press Include, Exclude (then pick "
                    "the first criterion it fails) or Unclear. The next record "
                    "opens by itself.",
                    "When all are labelled, press Run pilot at the top.",
                ],
                progress=f"{labelled} of {target} labelled",
                focus="pilot",
            )
        return NextStep(
            "Screen",
            "Run the pilot",
            "Your labels are in. The AI now screens the same records and the "
            "app compares its answers with yours.",
            ["Press Run pilot at the top of the Screen page.",
             "Read the agreement it reports before screening everything."],
        )
    n_unscreened = _one(db, "SELECT COUNT(*) FROM record r WHERE r.active = 1 "
                            "AND NOT EXISTS (SELECT 1 FROM screening_status s "
                            "WHERE s.record_id = r.id)")
    if n_unscreened:
        return NextStep(
            "Screen",
            "Screen all records",
            "Two AI screeners read every record independently; you decide "
            "only what they cannot settle.",
            ["Press Screen all records and accept the estimate.",
             "If it pauses (a spending or plan limit, or a busy server), "
             "press it again later: it carries on where it stopped."],
            progress=f"{n_unscreened} not yet screened",
        )
    # Leftover screening work comes first only until full-text work has
    # begun; after that it would hide the step the reviewer is actually on.
    fulltext_started = (
        _one(db, "SELECT COUNT(*) FROM report")
        or _one(db, "SELECT COUNT(*) FROM fulltext_status")
    )
    n_queue = _one(db, "SELECT COUNT(*) FROM review_queue WHERE resolved_at IS NULL")
    if n_queue and not fulltext_started:
        return NextStep(
            "Screen",
            "Decide the records waiting for you",
            "These are conflicts, records the screeners were unsure about, "
            "and a random check of decisions they agreed on.",
            [
                "Open the Needs your decision tab and click the first record.",
                "Read the abstract, then What each reviewer said below it.",
                "Press Include; or choose an exclusion code (check them in "
                "order), type a few words of reason and press Exclude.",
                "Keep a record unclear only when the abstract cannot settle "
                "it: each one kept is a PDF to fetch and read.",
            ],
            progress=f"{n_queue} waiting",
            focus="queue",
        )
    n_rapid = 0 if fulltext_started else rapid_unclear_count(db)
    if n_rapid:
        return NextStep(
            "Screen",
            "Settle the records left unclear",
            "These were marked unclear within a few seconds of the previous "
            "decision. Each one left unclear is a PDF to fetch and read.",
            [
                "Press Let the AI settle unclear records… at the top of the "
                "Screen page. The AI reads each abstract again and includes "
                "or excludes it where it can; the rest go to full text. The "
                "audit log records that you handed these to the AI.",
                "Or decide them yourself: on Needs your decision, set Show to "
                "My 'still unclear' decisions.",
            ],
            progress=f"{n_rapid} left unclear in under {RAPID_SECONDS} seconds",
            focus="revisit",
        )

    # -- full texts ------------------------------------------------------------
    extraction_started = _one(db, "SELECT COUNT(*) FROM report_extraction")
    synthesis_started = _one(db, "SELECT COUNT(*) FROM construct")
    ft = _row(db, "v_prisma_fulltext")
    sought = int(ft.get("n_sought") or 0)
    accounted = sum(int(ft.get(k) or 0) for k in ("n_retrieved", "n_not_retrieved",
                                                  "n_abstract_only"))
    if sought and accounted < sought and not extraction_started:
        return NextStep(
            "Retrieve",
            "Get the full texts",
            "Every record kept at screening needs its full text, or a "
            "recorded reason why it could not be obtained.",
            [
                "Press Find open-access copies to look up free versions.",
                "For the rest, download the PDFs through your library and drop "
                "them on the Retrieve page; they are matched to records by DOI "
                "and title.",
                "Match any leftover files by hand on Files to match by hand.",
                "For a report you cannot get, select it and press Mark as not "
                "retrieved… with the reason.",
            ],
            progress=f"{accounted} of {sought} accounted for",
        )
    if int(ft.get("n_unmatched_pdfs") or 0) and not extraction_started:
        return NextStep(
            "Retrieve",
            "Match the leftover PDFs by hand",
            progress=f"{ft.get('n_unmatched_pdfs')} files unmatched",
            steps=["Open Files to match by hand, pick a file, choose the record "
                   "it belongs to, and press Attach to this record."],
        )
    retrieved = int(ft.get("n_retrieved") or 0)
    assessed = int(ft.get("n_assessed") or 0)
    if retrieved and assessed < retrieved and not extraction_started:
        return NextStep(
            "Full-text",
            "Assess the full texts against the criteria",
            steps=["Press Assess retrieved reports and accept the estimate.",
                   "Then decide anything it sends to you."],
            progress=f"{assessed} of {retrieved} assessed",
        )
    if int(ft.get("n_awaiting_review") or 0) and not extraction_started:
        return NextStep(
            "Full-text",
            "Decide the full texts waiting for you",
            progress=f"{ft.get('n_awaiting_review')} waiting",
            steps=["Open Needs your decision, read the AI's reasons and quoted "
                   "passages, and press Include in the review, Exclude or "
                   "Still unclear."],
        )

    # -- extraction -------------------------------------------------------------
    ex = _row(db, "v_extraction_progress")
    included = int(ex.get("n_included") or 0)
    if not included:
        return NextStep(
            "Full-text", "No study is included yet",
            "Extraction starts once at least one report is included at full text.",
        )
    if int(ex.get("n_extracted") or 0) < included and not synthesis_started:
        return NextStep(
            "Extract",
            "Extract the included studies",
            steps=["Press Extract from included reports and accept the estimate."],
            progress=f"{ex.get('n_extracted') or 0} of {included} extracted",
        )
    if int(ex.get("n_awaiting_review") or 0) and not synthesis_started:
        return NextStep(
            "Extract",
            "Check the extractions waiting for you",
            progress=f"{ex.get('n_awaiting_review')} waiting",
            steps=["Open Needs your decision. For each report, check the "
                   "relationships against the quoted passages, change or "
                   "withdraw any that are wrong, then press Checked — accept "
                   "this report."],
        )

    # -- synthesis --------------------------------------------------------------
    sy = _row(db, "v_synthesis_progress")
    seq = [
        (not sy.get("n_constructs"), "1. Codebook", "Propose a codebook",
         "Press Propose a codebook, then edit, add or reject constructs."),
        (not sy.get("n_approvals"), "1. Codebook", "Approve the codebook",
         "When the constructs and definitions are right, press Approve the codebook."),
        (not sy.get("n_mapped"), "2. Mapping", "Map the extracted labels to constructs",
         "Press Map the labels, then move any label that landed in the wrong construct."),
        (not sy.get("n_harmonised"), "3. Orientation", "Harmonise the directions",
         "Check each construct's orientation, then press Harmonise the directions."),
        (not sy.get("n_datasets"), "4. Datasets", "Find studies that share a dataset",
         "Press Look for shared datasets, confirm or reject each suggestion, "
         "then press Build the datasets."),
        (not sy.get("n_graded_links"), "5. Graded inventory", "Count and grade the evidence",
         "Press Count and grade, and change any grade you disagree with."),
        (not _one(db, "SELECT COUNT(*) FROM sensitivity_result"), "6. Sensitivity",
         "Run the sensitivity analyses", "Press Run the sensitivity analyses."),
    ]
    for missing, tab, headline, how in seq:
        if missing:
            return NextStep("Synthesise", headline, steps=[f"Open the {tab} tab.", how])

    # -- framework -----------------------------------------------------------------
    fw = _row(db, "v_framework_progress")
    seq = [
        (not fw.get("n_themes"), "1. Themes", "Propose themes",
         "Press Propose themes, then approve, rename or reject each one."),
        (not fw.get("n_themes_approved"), "1. Themes", "Approve the themes",
         "Select each theme and press Approve, or Reject… with a reason."),
        (not fw.get("n_propositions"), "2. Propositions", "Draft the propositions",
         "Press Draft propositions."),
        (not (fw.get("n_accepted") or fw.get("n_rejected")), "2. Propositions",
         "Decide the propositions",
         "Accept, edit or reject each proposition; every citation must "
         "point to a graded link."),
        (not fw.get("n_figures"), "3. Figures", "Draw the framework",
         "Press Draw the evidence maps and Draw the integrated diagram."),
    ]
    for missing, tab, headline, how in seq:
        if missing:
            return NextStep("Framework", headline, steps=[f"Open the {tab} tab.", how])

    # -- gaps and export --------------------------------------------------------------
    if not _one(db, "SELECT COUNT(*) FROM gap_run"):
        return NextStep(
            "Gap Finder", "Find the research gaps",
            steps=["Press Find the gaps, then accept or reject each one.",
                   "Press Export register and agenda."],
        )
    exports = project.root / "exports"
    made = [p for p in exports.glob("*") if p.is_file()] if exports.exists() else []
    if not any(p.suffix in {".docx", ".xlsx"} for p in made):
        return NextStep(
            "Export", "Export the review",
            steps=["Press Export everything: PRISMA diagram, tables, methods "
                   "text and the AI-use statement."],
        )
    return NextStep(
        "Export", "The review is complete",
        "Everything is exported. Check the AI-use statement before submitting.",
        done=True,
    )
