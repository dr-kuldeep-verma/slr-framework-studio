"""Writing a benchmark result up so it can be read and disputed.

The shape of this report is an argument about what matters. Recall comes
first and specificity second, because a missed study cannot be recovered
and a wasted minute can. The records the screener threw away are listed by
title, because "94% recall" is a number people skim past and three named
papers is not. And every figure carries its interval and the properties of
the corpus it was computed on, so that a result from 40 relevant records
cannot be quoted as though it came from 400.
"""

from __future__ import annotations

from typing import Any

from .benchmark import BenchmarkResult

__all__ = ["as_markdown"]


def _doi_url(doi: str) -> str:
    """A link, whatever form the dataset stored the DOI in.

    Real exports hold `10.1007/x`, `doi:10.1007/x` and whole
    `https://dx.doi.org/10.1007/x` URLs in the same column, and pasting
    the resolver prefix onto all three produces a broken link for two of
    them.
    """
    value = (doi or "").strip()
    for prefix in (
        "https://doi.org/", "http://doi.org/",
        "https://dx.doi.org/", "http://dx.doi.org/",
        "doi:", "DOI:",
    ):
        if value.lower().startswith(prefix.lower()):
            value = value[len(prefix):]
    return f"https://doi.org/{value.strip()}"


def _tidy_title(title: str) -> str:
    """Trim the erratum boilerplate some databases append to a title.

    One record in the first dataset tried carried 300 characters of
    "[corrected to ...]" notes, which pushed the actual title off the
    line in the list of records the screener lost -- the one list in this
    report that has to be readable.
    """
    text = " ".join((title or "").split())
    for marker in (".[Erratum appears", "[Erratum appears", ". [Erratum"):
        if marker in text:
            text = text.split(marker)[0].rstrip(". ") + " [erratum note omitted]"
            break
    if len(text) > 240:
        text = text[:237].rstrip() + "…"
    return text


def _pct(value: float | None, interval: tuple[float, float] | None = None) -> str:
    if value is None:
        return "not defined"
    text = f"{value:.1%}"
    if interval:
        text += f" (95% CI {interval[0]:.1%} to {interval[1]:.1%})"
    return text


def as_markdown(result: BenchmarkResult) -> str:
    s = result.score
    d = result.dataset
    lines: list[str] = []

    lines += [
        f"# Screening benchmark: {d['name']}",
        "",
        f"Run {result.started_at} with **{result.model}** "
        f"({result.provider}), {result.n_ai_calls} model call(s), "
        f"{result.seconds / 60:.1f} minutes, ${result.cost:.2f}.",
        "",
        "## What this measures",
        "",
        "Each record was screened by the application exactly as a reviewer "
        "would run it, and the decision compared with the one the original "
        "review's human screeners made. Two recalls are reported because "
        "the workflow has a human in it:",
        "",
        "- **Workflow recall** — relevant records that survived to a human, "
        "whether the model said `include` or `unclear`. Only a record the "
        "model excluded outright is lost.",
        "- **Strict recall** — relevant records the model called `include` "
        "on its own. What it would achieve unsupervised.",
        "",
        "## Results",
        "",
        "| | Value |",
        "|---|---|",
        f"| **Workflow recall** | {_pct(s.workflow_recall, s.workflow_recall_interval)} |",
        f"| **Relevant records lost** | **{s.n_lost}** of {s.n_relevant} |",
        f"| Strict recall | {_pct(s.strict_recall, s.strict_recall_interval)} |",
        f"| Specificity | {_pct(s.specificity, s.specificity_interval)} |",
        f"| Precision | {_pct(s.precision)} |",
        f"| Reviewer burden | {_pct(s.burden)} ({s.n_to_human} of {s.n_records} records) |",
        f"| Number needed to read | "
        + (
            f"{s.number_needed_to_read:.1f}"
            if s.number_needed_to_read is not None
            else "not defined"
        )
        + " |",
        "",
    ]

    if s.n_lost:
        lines += [
            f"## The {s.n_lost} record(s) this cost",
            "",
            "Relevant studies the screener excluded outright. Nothing "
            "downstream would ever have seen these. This is the list a "
            "reviewer should read before deciding whether the recall above "
            "is good enough for their review.",
            "",
        ]
        for record in result.lost_records:
            year = f" ({record['year']})" if record.get("year") else ""
            flag = "" if record.get("had_abstract") else "  — *title only, no abstract*"
            doi = f"  <{_doi_url(record['doi'])}>" if record.get("doi") else ""
            lines.append(f"- {_tidy_title(record['title'])}{year}{flag}{doi}")
        lines.append("")
    else:
        lines += [
            "## Records lost",
            "",
            "None. Every relevant record reached a human.",
            "",
        ]

    share = d.get("share_without_abstract", 0.0)
    lines += [
        "## The corpus these figures come from",
        "",
        "| | |",
        "|---|---|",
        f"| Dataset | {d['name']} |",
        f"| Records | {d['n_records']} |",
        f"| Relevant | {d['n_relevant']} ({d['prevalence']:.1%}) |",
        f"| Without an abstract | {d['n_without_abstract']} ({share:.1%}) |",
        f"| Gold-standard label | `{d['label_column']}` |",
        f"| Licence | {d.get('licence') or 'not recorded'} |",
        f"| Source review | {d.get('reference') or 'not recorded'} |",
        "",
    ]

    if s.n_relevant and s.n_relevant < 50:
        lines += [
            f"> Only {s.n_relevant} relevant records. The confidence "
            "intervals above are wide for a reason, and a point estimate "
            "from this benchmark should not be quoted on its own.",
            "",
        ]

    if result.agreement:
        kappa = result.agreement.get("kappa")
        if kappa is not None:
            lines += [
                "## Agreement between the two AI screeners",
                "",
                f"Cohen's κ = {kappa:.2f} over {result.agreement.get('n', 0)} "
                "record(s). This says how consistently the two screening "
                "passes agreed with each other, which is a different thing "
                "from whether either was right.",
                "",
            ]

    lines += ["## The protocol they were judged against", "", "```", result.protocol, "```", ""]

    if result.notes:
        lines += ["## Caveats", ""]
        lines += [f"- {note}" for note in result.notes]
        lines.append("")

    lines += [
        "## What this does not show",
        "",
        "- It is one corpus in one field. A screener that does well on "
        "clinical abstracts may do badly on qualitative social science, "
        "and nothing here speaks to that.",
        "- The protocol above was written by the person running the "
        "benchmark from the published review. A differently worded "
        "protocol would produce different numbers, and that variation has "
        "not been measured here.",
        "- The human labels are themselves one review team's decisions, "
        "not ground truth. Published screening decisions disagree between "
        "teams more often than is comfortable.",
        "- Nothing here measures the rest of the pipeline: extraction, "
        "synthesis, the framework or the gap register are not evaluated by "
        "this run.",
        "",
    ]
    return "\n".join(lines)
