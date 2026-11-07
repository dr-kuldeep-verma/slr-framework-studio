"""The Methods draft, the AI-use statement and the audit log.

Two rules from Section 13 shape all three.

**The AI-use statement is generated from the log and cannot be switched off.**
There is no argument to omit it and no setting that hides it. It is assembled
from the `ai_call` rows -- provider, model, version, dates, tasks, counts --
so it cannot drift from what actually happened, and a reviewer who wants it
shorter has to use fewer models rather than fewer words.

**The Methods section describes the procedure as run.** Not the procedure the
application is capable of: a review that skipped the pilot gets a draft saying
the pilot was waived, and a review that never reached synthesis gets no
synthesis paragraph. Describing the intended procedure instead of the actual
one is how a Methods section becomes false, and it is the kind of falsehood
nobody notices until the data is checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..core.db import utc_now
from .prisma import read_counts

__all__ = ["ai_use_statement", "write_audit_log", "write_methods_draft"]


def _models(project) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in project.db.query(
            "SELECT provider, model, model_version, COUNT(*) AS n_calls, "
            "       SUM(cached) AS n_cached, "
            "       SUM(prompt_tokens) AS prompt_tokens, "
            "       SUM(completion_tokens) AS completion_tokens, "
            "       SUM(cost_usd) AS cost_usd, "
            "       MIN(created_at) AS first_call, "
            "       MAX(created_at) AS last_call "
            "FROM ai_call GROUP BY provider, model, model_version "
            "ORDER BY n_calls DESC"
        )
    ]


def _tasks(project) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in project.db.query(
            "SELECT task, COUNT(*) AS n_calls FROM ai_call "
            "GROUP BY task ORDER BY task"
        )
    ]


#: How each task reads in a sentence, keyed by the whole task name rather
#: than its tail: "screen:qc" and "extract:report" share nothing useful with
#: their last word, and an earlier version that split on the colon printed
#: "qc", "assess" and "screener a" into a manuscript.
#:
#: A task with no entry here falls back to its own name, which is ugly on
#: purpose: a test asserts every task the application makes has a phrase, so
#: the ugliness shows up in the suite rather than in someone's Methods
#: section.
_TASK_WORDS = {
    "screen:screener_a": "title and abstract screening",
    "screen:screener_b": "title and abstract screening",
    "screen:adjudicate": "adjudicating screening disagreements",
    "screen:qc": "quality control of screening decisions",
    "fulltext:assess": "full-text eligibility assessment",
    "extract:report": "data extraction from included reports",
    "extract:direction": "blind verification of recorded directions",
    "extract:second_coder": "independent second coding of a sample",
    "synthesise:codebook": "proposing the construct codebook",
    "synthesise:map": "mapping author labels onto constructs",
    "synthesise:linkage": "detecting reports sharing a sample",
    "synthesise:contradiction": "explaining contested links",
    "framework:themes": "grouping constructs into themes",
    "framework:propositions": "drafting propositions",
    "gaps:study": "drafting studies from the gap register",
}


def _task_phrase(task: str) -> str:
    return _TASK_WORDS.get(task, task.replace("_", " ").replace(":", " "))


def ai_use_statement(project) -> str:
    """The AI-use statement, assembled from the call log.

    Generated rather than written: it reports what the log holds, so it
    cannot claim a model that was not used or omit one that was.
    """
    models = _models(project)
    if not models:
        return (
            "No language model was used in this review. The project records "
            "no model calls of any kind, and every decision in it was made by "
            "a person."
        )

    # Counted from the structured table rather than by matching words in the
    # audit log's action names, which recorded none of them and reported
    # nought overrides for a review that had them.
    overrides = project.db.scalar("SELECT COUNT(*) FROM human_override") or 0
    total_calls = sum(m["n_calls"] for m in models)
    cost = sum(m["cost_usd"] or 0.0 for m in models)
    first = min(m["first_call"] for m in models)[:10]
    last = max(m["last_call"] for m in models)[:10]

    described = "; ".join(
        f"{m['model']}"
        + (f" (version {m['model_version']})" if m["model_version"] else "")
        + f" via {m['provider']}, {m['n_calls']} call(s)"
        for m in models
    )
    # One phrase per job, not one per task name: both screeners do the same
    # job, and listing it twice reads as though it happened twice.
    phrases: list[str] = []
    for row in _tasks(project):
        phrase = _task_phrase(row["task"])
        if phrase not in phrases:
            phrases.append(phrase)
    tasks = ", ".join(phrases)

    return (
        "Artificial intelligence was used in the conduct of this review. "
        f"The model(s) used were: {described}. "
        f"Calls were made between {first} and {last}, "
        f"{total_calls} in total, at an estimated cost of ${cost:.2f}. "
        f"The model was used for: {tasks}. "
        "Every model output was recorded with the model name and version, a "
        "hash of the prompt, a hash of the input, the output itself and a "
        "timestamp, and every one could be overridden by a reviewer; "
        f"{overrides} override(s) were recorded. "
        "The model did not select studies, extract data or draw conclusions "
        "on its own authority: each of its decisions was confirmed or "
        "overridden by a person before it counted, and the audit log exported "
        "with this review records every one."
    )


# -- the Methods draft ---------------------------------------------------------


def _what_was_run(project) -> dict[str, bool]:
    db = project.db

    def any_rows(table: str, condition: str = "1=1") -> bool:
        return bool(db.scalar(f"SELECT COUNT(*) FROM {table} WHERE {condition}"))

    return {
        "search": any_rows("import_file"),
        "dedup": any_rows("record", "duplicate_of IS NOT NULL"),
        "screening": any_rows("screening_status"),
        "retrieval": any_rows("report"),
        "fulltext": any_rows("fulltext_status"),
        "extraction": any_rows("report_extraction"),
        # Separate from "synthesis", which needs graded links: a codebook
        # exists, and has to be described, as soon as it has been built.
        "codebook": any_rows("construct", "is_residual = 0"),
        "synthesis": any_rows("graded_link"),
        "framework": any_rows("proposition"),
        "gaps": any_rows("gap_register"),
    }


def _codebook_origin(project) -> str:
    """Where the constructs came from, in a sentence a journal will want.

    A reader cannot interpret a framework without knowing whether its
    constructs were specified before the evidence was coded or derived from
    it. For a deductive review the sentence names the protocol version the
    constructs were fixed in, because "a priori" is a claim about a date.
    """
    from ..core.protocol import parse_a_priori_constructs

    info = project.info()
    if (info.get("framework_approach") or "bottom_up") != "top_down":
        return (
            "The construct codebook was derived inductively from the "
            "extracted author labels: no constructs were specified in "
            "advance."
        )
    declared = parse_a_priori_constructs(info.get("a_priori_constructs", ""))
    current = project.protocol.current()
    where = (
        f"version {current.version} of the protocol ({current.short_hash})"
        if current is not None
        else "the review protocol"
    )
    # Named where the reviewer named one. A deductive framework cannot be
    # read without knowing what it deduces from, but inventing a theory to
    # fill the sentence would be worse than leaving it general.
    theory = (info.get("a_priori_theory") or "").strip()
    citation = (info.get("a_priori_citation") or "").strip()
    if theory:
        drawn = f" drawn from {theory}" + (f" ({citation})" if citation else "")
    else:
        drawn = ""
    return (
        f"The construct codebook was specified deductively: its "
        f"{len(declared)} construct(s){drawn} were stated in {where}, which "
        "was locked before the evidence was coded, and the codebook could "
        "not be added to during coding. Author labels fitting none of them "
        "were assigned to a residual category and reported rather than "
        "placed in the nearest construct."
    )


def _paragraphs(project) -> list[tuple[str, str]]:
    """The Methods draft, as (heading, prose), for the stages that ran."""
    db = project.db
    info = project.info()
    counts = read_counts(project)
    ran = _what_was_run(project)
    out: list[tuple[str, str]] = []

    if ran["search"]:
        databases = "; ".join(
            f"{name or 'unnamed'} ({n} record(s))"
            for name, n in counts.databases
        )
        searched_to = (info.get("searched_to") or "").strip()
        out.append((
            "Search",
            f"Records were obtained from {databases}. "
            + (
                f"The searches were last run on {searched_to}. "
                if searched_to
                else "The project does not record the date the searches were "
                     "last run; state it here. "
            )
            + f"{counts.n_identified} record(s) were identified in total."
        ))

    if ran["dedup"]:
        out.append((
            "Deduplication",
            f"{counts.n_duplicates_removed} record(s) were removed as "
            "duplicates before screening, by matching on DOI, on normalised "
            "title with year and first author, and on a normalised title key. "
            f"{counts.n_screened} record(s) remained."
        ))

    if ran["screening"]:
        pilot = db.query_one(
            "SELECT * FROM screening_run WHERE kind = 'pilot' "
            "ORDER BY id DESC LIMIT 1"
        )
        waived = db.scalar(
            "SELECT COUNT(*) FROM audit_log WHERE action LIKE '%pilot_waived%'"
        )
        kappa = db.scalar(
            "SELECT kappa FROM screening_agreement ORDER BY id DESC LIMIT 1"
        ) if db.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
            "AND name = 'screening_agreement'"
        ) else None
        text = (
            f"{counts.n_screened} record(s) were screened on title and "
            "abstract by two independent screeners with differing stances, "
            "each blinded to the other's decision and to the source database. "
            "Disagreements were adjudicated by a third assessment, and every "
            "conflict, unclear record, quality-control rescue and random "
            "audit was confirmed by a reviewer before it counted. "
        )
        if pilot:
            text += (
                f"A pilot of {pilot['n_records']} record(s) was run and "
                "compared with the reviewer's own labels before the full run. "
            )
        elif waived:
            text += (
                "The pilot was waived, and the waiver is recorded in the "
                "audit log. "
            )
        else:
            text += "No pilot was run. "
        if kappa is not None:
            text += f"Agreement between the two screeners was κ = {kappa:.2f}. "
        text += (
            f"{counts.n_excluded_screening} record(s) were excluded at this "
            f"stage and {counts.n_sought} were sought for retrieval."
        )
        out.append(("Screening", text))

    if ran["retrieval"]:
        out.append((
            "Retrieval",
            f"Full texts were sought for {counts.n_sought} report(s), of "
            f"which {counts.n_retrieved} were obtained and "
            f"{counts.n_not_retrieved} were not. Open-access locations were "
            "looked up through Unpaywall; no paywalled source was accessed, "
            "no institutional credentials were used and nothing was scraped."
        ))

    if ran["fulltext"]:
        reasons = "; ".join(
            f"{reason} (n = {n})" for reason, n in counts.exclusion_reasons
        ) or "no exclusions were recorded"
        out.append((
            "Eligibility",
            f"{counts.n_assessed} report(s) were assessed against the "
            "eligibility criteria on their full text. Each judgement cites a "
            "quotation located on a numbered page, and every quotation was "
            "verified against the page it was attributed to before the "
            f"judgement was accepted. {counts.n_excluded_fulltext} report(s) "
            f"were excluded, with reasons: {reasons}. "
            f"{counts.n_included} study(ies) were included."
        ))

    if ran["extraction"]:
        n_relationships = db.scalar("SELECT COUNT(*) FROM relationship") or 0
        n_second = db.scalar(
            "SELECT COUNT(*) FROM report_extraction WHERE coder_role = "
            "'second_coder'"
        ) or 0
        out.append((
            "Data extraction",
            f"Data were extracted from each included report, yielding "
            f"{n_relationships} recorded relationship(s). Every relationship "
            "carries a quotation and the page it was found on. The direction "
            "of each relationship was re-derived from its quotation alone, by "
            "an assessment that was not shown the recorded direction, and any "
            "disagreement was referred to a reviewer. "
            + (
                f"A second coder independently coded {n_second} report(s) and "
                "agreement was computed."
                if n_second
                else "No second coder was used."
            )
        ))

    if ran["codebook"]:
        out.append(("Construct codebook", _codebook_origin(project)))

    if ran["synthesis"]:
        n_links = db.scalar("SELECT COUNT(*) FROM graded_link") or 0
        n_datasets = db.scalar("SELECT COUNT(*) FROM dataset") or 0
        n_sensitivity = db.scalar(
            "SELECT COUNT(DISTINCT analysis) FROM sensitivity_result"
        ) or 0
        out.append((
            "Synthesis",
            "Author labels were mapped onto a construct codebook which a "
            "reviewer approved before any mapping was applied. Each construct "
            "was given a reference orientation and every relationship was "
            "re-expressed against it, with each sign change logged. Reports "
            f"sharing a sample were grouped, giving {n_datasets} dataset(s), "
            "so that a sample reported more than once votes once; reviews and "
            "conceptual papers did not vote. Evidence for each of the "
            f"{n_links} link(s) was graded by vote counting at dataset level, "
            "with grades applied in a fixed precedence and a two-sided exact "
            "sign test; each link is reported with a Wilson 95% confidence "
            "interval. "
            + (
                f"{n_sensitivity} sensitivity analysis(es) were run and any "
                "link that changed sign or grade is reported."
                if n_sensitivity else ""
            )
        ))

    if ran["framework"]:
        n_props = db.scalar(
            "SELECT COUNT(*) FROM proposition WHERE status <> 'rejected'"
        ) or 0
        n_rejected = db.scalar(
            "SELECT COUNT(*) FROM proposition WHERE status = 'rejected'"
        ) or 0
        out.append((
            "Framework",
            f"{n_props} proposition(s) were drafted, each citing links from "
            "the graded inventory by identifier, with a mechanism, "
            "moderators, boundary conditions and a test that would falsify "
            "it. Any proposition citing a link that was not in the graded "
            "inventory was rejected in full rather than corrected; "
            f"{n_rejected} were rejected on that basis. Evidence maps and an "
            "integrated diagram were drawn from the graded links only."
        ))

    if ran["gaps"]:
        n_gaps = db.scalar("SELECT COUNT(*) FROM gap_register") or 0
        searched_to = (info.get("searched_to") or "").strip()
        out.append((
            "Gap analysis",
            f"{n_gaps} gap(s) were identified from the coded evidence, of "
            "seven kinds: evidence, theoretical, contradiction, "
            "methodological, population and context, information, and gaps "
            "the included authors stated themselves. Each gap carries the "
            "counts and the studies behind it and is reported as an absence "
            "within the included corpus"
            + (f", searched to {searched_to}" if searched_to else "")
            + ", not as an absence in the literature."
        ))

    return out


def write_methods_draft(project, path: str | Path) -> Path:
    """A Methods section describing the procedure as it was actually run."""
    from docx import Document
    from docx.shared import Pt

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    document = Document()
    document.add_heading("Methods (draft)", level=1)
    document.add_paragraph(project.info()["title"])
    document.add_paragraph(
        "This is a draft, assembled from what this project recorded. It "
        "describes the procedure as it was run, not the procedure the "
        "software is capable of: a stage that was not run has no paragraph "
        "here. Every number is read from the project database. Check it, "
        "rewrite it in your own voice, and add what the application cannot "
        "know."
    )

    for heading, prose in _paragraphs(project):
        document.add_heading(heading, level=2)
        document.add_paragraph(prose)

    document.add_heading("Use of artificial intelligence", level=2)
    statement = document.add_paragraph(ai_use_statement(project))
    for run in statement.runs:
        run.font.size = Pt(10)

    document.add_paragraph()
    document.add_paragraph(f"Drafted {utc_now()}.")
    document.save(str(path))
    return path


# -- the audit log --------------------------------------------------------------


def write_audit_log(project, path: str | Path) -> Path:
    """The whole hash-chained log, with its verification and the AI calls."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()

    sheet = workbook.active
    sheet.title = "Audit log"
    headers = [
        "id", "timestamp", "stage", "action", "actor", "entity", "entity id",
        "detail", "previous hash", "entry hash",
    ]
    sheet.append(headers)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for row in project.db.query("SELECT * FROM audit_log ORDER BY id"):
        sheet.append(
            [
                row["id"], row["ts"], row["stage"], row["action"], row["actor"],
                row["entity"], row["entity_id"], row["detail"],
                row["prev_hash"], row["entry_hash"],
            ]
        )
    for letter, width in {
        "B": 22, "C": 14, "D": 26, "E": 18, "F": 18, "H": 70, "I": 24, "J": 24,
    }.items():
        sheet.column_dimensions[letter].width = width
    sheet.freeze_panes = "A2"

    verification = workbook.create_sheet("Verification")
    problems = project.audit.verify()
    verification.append(["Checked at", utc_now()])
    verification.append(["Entries", project.db.scalar(
        "SELECT COUNT(*) FROM audit_log"
    )])
    verification.append(
        [
            "Result",
            "The chain is intact: every entry's hash matches its content and "
            "the entry before it."
            if not problems
            else f"The chain is broken at {len(problems)} point(s).",
        ]
    )
    for problem in problems:
        verification.append(["Problem", str(problem)])
    verification.column_dimensions["A"].width = 16
    verification.column_dimensions["B"].width = 90

    calls = workbook.create_sheet("AI calls")
    call_headers = [
        "id", "stage", "task", "provider", "model", "model version",
        "temperature", "prompt hash", "input hash", "output hash",
        "prompt tokens", "completion tokens", "cost (USD)", "cached",
        "entity", "entity id", "timestamp",
    ]
    calls.append(call_headers)
    for cell in calls[1]:
        cell.font = Font(bold=True)
    for row in project.db.query("SELECT * FROM ai_call ORDER BY id"):
        calls.append(
            [
                row["id"], row["stage"], row["task"], row["provider"],
                row["model"], row["model_version"], _temperature_label(row["temperature"]),
                row["prompt_hash"], row["input_hash"], row["output_hash"],
                row["prompt_tokens"], row["completion_tokens"], row["cost_usd"],
                "yes" if row["cached"] else "no",
                row["entity"], row["entity_id"], row["created_at"],
            ]
        )
    calls.freeze_panes = "A2"

    workbook.save(path)
    return path


def _temperature_label(value):
    """The temperature as logged, or what the reserved value means."""
    from ..ai.base import TEMPERATURE_NOT_SENT

    if value == TEMPERATURE_NOT_SENT:
        return "not sent (the model refuses one and uses its own)"
    return value
