"""A benchmark for the stages after title and abstract screening.

The screening benchmark (``benchmark.py``) asks one question of one stage.
This one carries a published review's records through full-text
eligibility, extraction, synthesis and the framework, and scores each AI
first pass against what the review's authors decided and extracted.

**A script plays the reviewer.** The app stops for a person at several
points. Here one rule, fixed before the run, decides for them: accept every
AI proposal unchanged and say so in the logged reason, except where the
gold standard answers the question directly -- full-text eligibility, which
is settled by the review's final inclusion, and shared datasets, confirmed
only when both reports belong to the same study. Each stage therefore starts
from correct inputs and its own errors are measured without the previous
stage's mixed in. A real reviewer might miss some of what the script
corrects, which is why this is reported as a limitation, not a strength.

**It does not fetch papers.** Full texts are read from a folder, one PDF
per record named ``<source id>.pdf``. A record without one is marked not
retrieved, with the reason given.

**It resumes.** Every completed step is recorded in the project, and every
answer already bought is in the cache, so running the same command again
carries on where a run stopped.
"""

from __future__ import annotations

import csv
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from ..ai.client import AIClient, CostEstimate
from ..core.db import utc_now
from ..core.project import PROJECT_DB_NAME, Project
from .metrics import _interval as wilson_interval

__all__ = [
    "PipelineRecord",
    "PipelineError",
    "PipelineCancelled",
    "ARM_PATTERNS",
    "OUTCOME_PATTERNS",
    "DESIGN_FAMILY_FOR",
    "load_records",
    "load_gold",
    "find_arms",
    "find_outcomes",
    "score_fulltext",
    "score_extraction",
    "score_codebook",
    "score_dataset_links",
    "match_findings",
    "run_pipeline_benchmark",
    "render_pipeline_report",
]

ProgressFn = Callable[[str, int, int], None]

#: How a treatment arm is recognised in an extracted label. Lower-cased text
#: is searched; the lists are fixed before the run and published with it.
ARM_PATTERNS: dict[str, str] = {
    "dpen": r"penicillamin|\bd-?pen\b|\bdpa\b",
    "trientine": r"trient|triethylene",
    "zinc": r"zinc|\bzn\b",
    "ttm": r"tetrathiomolybdat|\bttm\b",
    "succimer": r"succimer|\bdmsa\b|dimercaptosuccin",
    "no_treatment": (
        r"no treatment|untreated|without treatment|no therapy|not treated|"
        r"no specific treatment|natural (history|course)|no (dpen|d-penicillamine)"
    ),
}

OUTCOME_PATTERNS: dict[str, str] = {
    "mortality": r"mortal|death|died|surviv|fatal",
    "improved": (
        r"improv|asymptomatic|remission|response|recover|normali[sz]|"
        r"stabili[sz]|efficacy|effective|clinical (course|outcome|status)"
    ),
    "side_effects": (
        r"side[- ]?effect|adverse|toxic|intoleran|nephrotox|proteinuria|lupus|"
        r"leukopen|thrombocytopen|rash|hypersensitiv"
    ),
    "discontinuation": r"discontinu|switch|withdraw|stopp|cessation|change of (therapy|treatment)",
    "neuro_deterioration": (
        r"neuro\w*.{0,40}(deteriorat|worsen|aggravat)|"
        r"(deteriorat|worsen|aggravat).{0,40}neuro"
    ),
    "olt": r"transplant|\bolt\b|\blt\b",
    "fibrosis": r"fibros|cirrho|histolog|biops",
    "autoimmune": r"autoimmun|immune|lupus|antinuclear",
    "survival_15y": r"surviv|mortal|death",
}

#: The design family a correct extraction gives each design in the gold file.
DESIGN_FAMILY_FOR: dict[str, str] = {
    "randomised_controlled_trial": "randomised_experiment",
    "non_randomised_controlled_trial": "quasi_experiment",
    "retrospective_observational": "longitudinal",
    "prospective_observational": "longitudinal",
}

SCRIPT_ACTOR = "human:benchmark-script"
ACCEPTED_UNREAD = (
    "Benchmark script: the AI proposal was accepted unread, by the rule "
    "fixed before the run."
)


class PipelineError(RuntimeError):
    pass


class PipelineCancelled(PipelineError):
    pass


@dataclass
class PipelineRecord:
    source_id: int
    title: str
    abstract: str
    doi: str
    year: int | None
    label: int


# -- inputs ------------------------------------------------------------------


def load_records(sought_csv: str | Path, corpus_csv: str | Path | None = None) -> list[PipelineRecord]:
    """Read the records sought at full text.

    *sought_csv* has ``record_id``, ``openalex_id``, ``doi``, ``title`` and
    ``label`` columns. *corpus_csv*, when given, supplies abstracts and years
    by OpenAlex id, since the full-text stage shows the model both.
    """
    extra: dict[str, dict[str, str]] = {}
    if corpus_csv:
        with open(corpus_csv, encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                extra[(row.get("openalex_id") or "").lower()] = row
    records: list[PipelineRecord] = []
    with open(sought_csv, encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            more = extra.get((row.get("openalex_id") or "").lower(), {})
            abstract = (
                more.get("abstract_original") or more.get("abstract") or ""
            ).strip()
            year_text = (more.get("publication_year") or more.get("year") or "").strip()
            records.append(
                PipelineRecord(
                    source_id=int(row["record_id"]),
                    title=(row.get("title") or "").strip(),
                    abstract=abstract,
                    doi=(row.get("doi") or "").replace("https://doi.org/", "").strip(),
                    year=int(year_text) if year_text.isdigit() else None,
                    label=int(row["label"]),
                )
            )
    if not records:
        raise PipelineError(f"No records in {sought_csv}.")
    return records


def load_gold(path: str | Path) -> dict[str, Any]:
    gold = json.loads(Path(path).read_text(encoding="utf-8"))
    if not gold.get("studies"):
        raise PipelineError(f"{path} lists no studies.")
    return gold


def study_of(gold: dict[str, Any]) -> dict[int, str]:
    """Source record id -> the study (body of fieldwork) it reports.

    ``same_data`` lists groups of records that report one data collection
    even though the gold file scores them as separate articles.
    """
    out = {rid: s["study"] for s in gold["studies"] for rid in s["records"]}
    for group in gold.get("same_data", []):
        name = "+".join(str(r) for r in group)
        for rid in group:
            out[rid] = name
    return out


# -- the S-O-B-C scoring ---------------------------------------------------------

SOBC_CODES = (
    [f"S{i}" for i in range(1, 8)] + [f"O{i}" for i in range(1, 8)]
    + [f"B{i}" for i in range(1, 7)] + [f"C{i}" for i in range(1, 7)]
)

#: How the app's design family is compared with a review's broad design label.
#: A case study is counted as qualitative, the commonest use in this field.
SOBC_DESIGN_FOR: dict[str, str] = {
    "randomised_experiment": "Quantitative",
    "quasi_experiment": "Quantitative",
    "cross_sectional": "Quantitative",
    "longitudinal": "Quantitative",
    "case_study": "Qualitative",
    "qualitative": "Qualitative",
    "mixed_methods": "Mixed methods",
    "conceptual": "Conceptual",
    "review": "Conceptual",
}

#: MMAT criteria met -> the appraisal concern a matching appraisal gives.
def mmat_band(score: int | None) -> str | None:
    if score is None:
        return None
    return "low" if score >= 4 else "moderate" if score >= 2 else "high"


def construct_code(name: str) -> str | None:
    """The S-O-B-C code a construct's name starts with, if any."""
    m = re.match(r"\s*([SOBC][1-7])\b", name or "")
    return m.group(1) if m else None


def _kappa(a: list[bool], b: list[bool]) -> float | None:
    n = len(a)
    if n == 0:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def score_sobc(
    gold: dict[str, Any],
    extractions: dict[int, dict[str, Any]],
    relationships: dict[int, list[dict[str, Any]]],
    coded: dict[int, set[str]],
    *,
    verified_only: bool | None = None,
) -> dict[str, Any]:
    """Score S-O-B-C coding, design and appraisal against a review's coding.

    *coded* maps source record id to the codes the app's synthesis reached
    for it (the constructs its relationships were mapped to). Only included
    studies with an extraction count. Results are given for every such
    study and, separately, for those whose gold coding a person verified.
    """
    def block(studies: list[dict[str, Any]]) -> dict[str, Any]:
        pairs_tp = pairs_fp = pairs_fn = 0
        per_code = {}
        for code in SOBC_CODES:
            g = [code in s["codes"] for s in studies]
            a = [code in s["ai_codes"] for s in studies]
            per_code[code] = {
                "gold": sum(g), "app": sum(a),
                "both": sum(x and y for x, y in zip(g, a)),
                "kappa": _kappa(g, a),
            }
        for s in studies:
            g, a = set(s["codes"]), s["ai_codes"]
            pairs_tp += len(g & a); pairs_fp += len(a - g); pairs_fn += len(g - a)
        by_group = {}
        for p in "SOBC":
            tp = sum(len({c for c in s["codes"] if c[0] == p} & s["ai_codes"]) for s in studies)
            fn = sum(len({c for c in s["codes"] if c[0] == p} - s["ai_codes"]) for s in studies)
            fp = sum(len({c for c in s["ai_codes"] if c[0] == p} - set(s["codes"])) for s in studies)
            by_group[p] = {"recall": _rate(tp, tp + fn), "precision": _rate(tp, tp + fp)}
        design = [s for s in studies if s.get("design")]
        appraised = [s for s in studies if mmat_band(s.get("mmat"))]
        return {
            "n": len(studies),
            "recall": _rate(pairs_tp, pairs_tp + pairs_fn),
            "precision": _rate(pairs_tp, pairs_tp + pairs_fp),
            "by_group": by_group,
            "per_code": per_code,
            "design": _rate(sum(s["ai_design"] == s["design"] for s in design), len(design)),
            "design_confusion": _confusion([(s["design"], s["ai_design"]) for s in design]),
            "appraisal": _rate(
                sum(s["ai_concern"] == mmat_band(s["mmat"]) for s in appraised), len(appraised)
            ),
            "mmat_used": _rate(sum(s["ai_instrument"] == "MMAT" for s in studies), len(studies)),
        }

    rows = []
    for s in gold["studies"]:
        if not s.get("included", 1):
            continue
        reports = [rid for rid in s["records"] if rid in extractions]
        if not reports:
            continue
        ex = extractions[reports[0]]
        ai_codes: set[str] = set()
        for rid in reports:
            ai_codes |= coded.get(rid, set())
        rows.append({
            **s,
            "ai_codes": ai_codes,
            "ai_design": SOBC_DESIGN_FOR.get(ex.get("design_family") or "", "Unclear"),
            "ai_concern": ex.get("appraisal_concern"),
            "ai_instrument": ex.get("appraisal_instrument"),
            "n_relationships": sum(len(relationships.get(rid, [])) for rid in reports),
        })
    rels = [rel for rs in relationships.values() for rel in rs]
    quoted = [r for r in rels if r.get("quote")]
    checked = [r for r in rels if r.get("verify_agrees") is not None]
    verified = [r for r in rows if r.get("verified")]
    return {
        "kind": "sobc",
        "studies_covered": len(rows),
        "studies_total": sum(1 for s in gold["studies"] if s.get("included", 1)),
        "all": block(rows),
        "verified": block(verified) if verified else None,
        "relationships": len(rels),
        "relationship_quotes": {
            "quoted": len(quoted),
            "cited_page": sum(r.get("quote_found_on") == "cited" for r in quoted),
            "elsewhere": sum(r.get("quote_found_on") == "elsewhere" for r in quoted),
            "nowhere": sum(r.get("quote_found_on") == "nowhere" for r in quoted),
        },
        "direction_check": _rate(sum(bool(r["verify_agrees"]) for r in checked), len(checked)),
        "relationships_queued": _rate(sum(bool(r.get("needs_review")) for r in rels), len(rels)),
        "reports_queued": _rate(
            sum(bool(e.get("needs_review")) for e in extractions.values()), len(extractions)
        ),
        "per_study": [
            {"study": r["study"], "gold": sorted(r["codes"]), "app": sorted(r["ai_codes"]),
             "design": [r["design"], r["ai_design"]], "mmat": r.get("mmat"),
             "concern": r["ai_concern"], "verified": bool(r.get("verified")),
             "n_relationships": r["n_relationships"]}
            for r in rows
        ],
    }


def _confusion(pairs: list[tuple[str, str]]) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for g, a in pairs:
        out.setdefault(g, {}).setdefault(a, 0)
        out[g][a] += 1
    return out


# -- matching ------------------------------------------------------------------


def find_arms(text: str) -> set[str]:
    low = (text or "").lower()
    return {arm for arm, pattern in ARM_PATTERNS.items() if re.search(pattern, low)}


def find_outcomes(text: str) -> set[str]:
    low = (text or "").lower()
    return {o for o, pattern in OUTCOME_PATTERNS.items() if re.search(pattern, low)}


def _rate(k: int, n: int) -> dict[str, Any]:
    if n == 0:
        return {"k": 0, "n": 0, "rate": None, "ci": None}
    low, high = wilson_interval(k, n)
    return {"k": k, "n": n, "rate": k / n, "ci": [low, high]}


# -- scoring -----------------------------------------------------------------


def score_fulltext(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Score the AI full-text decisions.

    Each row: ``source_id``, ``label``, ``retrieved`` (bool), ``ai_decision``
    (include/exclude/unclear or None), ``quote_found_on`` (cited/elsewhere/
    nowhere/none), ``queued`` (bool).
    """
    retrieved = [r for r in rows if r["retrieved"]]
    assessed = [r for r in retrieved if r.get("ai_decision")]
    relevant = [r for r in assessed if r["label"] == 1]
    irrelevant = [r for r in assessed if r["label"] == 0]
    kept = lambda r: r["ai_decision"] in ("include", "unclear")  # noqa: E731
    quotes = [r for r in assessed if r.get("quote_found_on") not in (None, "", "none")]
    return {
        "sought": len(rows),
        "sought_relevant": sum(r["label"] for r in rows),
        "retrieved": len(retrieved),
        "retrieved_relevant": sum(r["label"] for r in retrieved),
        "assessed": len(assessed),
        "recall": _rate(sum(kept(r) for r in relevant), len(relevant)),
        "strict_recall": _rate(
            sum(r["ai_decision"] == "include" for r in relevant), len(relevant)
        ),
        "specificity": _rate(
            sum(r["ai_decision"] == "exclude" for r in irrelevant), len(irrelevant)
        ),
        "lost": [r["source_id"] for r in relevant if not kept(r)],
        "decisions": {
            d: sum(r["ai_decision"] == d for r in assessed)
            for d in ("include", "unclear", "exclude")
        },
        "quotes": {
            "checked": len(quotes),
            "cited_page": sum(r["quote_found_on"] == "cited" for r in quotes),
            "elsewhere": sum(r["quote_found_on"] == "elsewhere" for r in quotes),
            "nowhere": sum(r["quote_found_on"] == "nowhere" for r in quotes),
        },
        "queued": _rate(sum(bool(r.get("queued")) for r in assessed), len(assessed)),
    }


def score_extraction(
    gold: dict[str, Any],
    extractions: dict[int, dict[str, Any]],
    relationships: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Score extractions against the gold study table.

    *extractions* maps source record id to its report-level extraction;
    *relationships* maps source record id to its extracted relationships.
    A study counts as covered when any of its reports was extracted, and a
    gold item counts as found when any of its reports found it.
    """
    per_study: list[dict[str, Any]] = []
    for study in gold["studies"]:
        reports = [rid for rid in study["records"] if rid in extractions]
        if not reports:
            continue
        families = {extractions[rid].get("design_family") or "unclear" for rid in reports}
        sizes = {extractions[rid].get("sample_size") for rid in reports} - {None}
        arm_text = " ".join(
            " ".join(
                [
                    extractions[rid].get("design") or "",
                    extractions[rid].get("design_note") or "",
                    extractions[rid].get("key_findings") or "",
                    extractions[rid].get("population") or "",
                ]
                + [
                    f"{rel.get('predictor_label', '')} {rel.get('outcome_label', '')}"
                    for rel in relationships.get(rid, [])
                ]
            )
            for rid in reports
        )
        outcome_text = " ".join(
            rel.get("outcome_label", "")
            for rid in reports
            for rel in relationships.get(rid, [])
        )
        arms_found = find_arms(arm_text) & set(study["arms"])
        outcomes_found = find_outcomes(outcome_text) & set(study["outcomes"])
        n_rel = sum(len(relationships.get(rid, [])) for rid in reports)
        per_study.append(
            {
                "study": study["study"],
                "reports": reports,
                "design_gold": study["design"],
                "design_family": sorted(families),
                "design_correct": DESIGN_FAMILY_FOR[study["design"]] in families,
                "sample_size": sorted(sizes),
                "n_gold": [study["n"], study["n_original"]],
                "size_correct": bool(sizes & {study["n"], study["n_original"]}),
                "arms_gold": study["arms"],
                "arms_found": sorted(arms_found),
                "outcomes_gold": study["outcomes"],
                "outcomes_found": sorted(outcomes_found),
                "n_relationships": n_rel,
            }
        )
    rels = [rel for rows in relationships.values() for rel in rows]
    quoted = [r for r in rels if r.get("quote")]
    checked = [r for r in rels if r.get("verify_agrees") is not None]
    n_arms = sum(len(s["arms_gold"]) for s in per_study)
    n_out = sum(len(s["outcomes_gold"]) for s in per_study)
    return {
        "studies_covered": len(per_study),
        "studies_total": len(gold["studies"]),
        "design": _rate(sum(s["design_correct"] for s in per_study), len(per_study)),
        "sample_size": _rate(sum(s["size_correct"] for s in per_study), len(per_study)),
        "arms": _rate(sum(len(s["arms_found"]) for s in per_study), n_arms),
        "outcomes": _rate(sum(len(s["outcomes_found"]) for s in per_study), n_out),
        "relationships": len(rels),
        "relationship_quotes": {
            "quoted": len(quoted),
            "cited_page": sum(r.get("quote_found_on") == "cited" for r in quoted),
            "elsewhere": sum(r.get("quote_found_on") == "elsewhere" for r in quoted),
            "nowhere": sum(r.get("quote_found_on") == "nowhere" for r in quoted),
        },
        "direction_check": _rate(sum(bool(r["verify_agrees"]) for r in checked), len(checked)),
        "relationships_queued": _rate(sum(bool(r.get("needs_review")) for r in rels), len(rels)),
        "reports_queued": _rate(
            sum(bool(e.get("needs_review")) for e in extractions.values()), len(extractions)
        ),
        "per_study": per_study,
    }


def score_codebook(gold: dict[str, Any], constructs: list[dict[str, Any]], covered: Iterable[str]) -> dict[str, Any]:
    """Which gold arms and outcomes have a construct that names them.

    Only arms and outcomes of studies that were extracted (*covered*) count:
    a construct cannot be proposed for something no report was read for.
    """
    studies = [s for s in gold["studies"] if s["study"] in set(covered)]
    arms = sorted({a for s in studies for a in s["arms"]})
    outcomes = sorted({o for s in studies for o in s["outcomes"]})
    texts = [f"{c.get('name', '')} {c.get('definition', '')}" for c in constructs]
    arm_hits = {a: [c["name"] for c, t in zip(constructs, texts) if a in find_arms(t)] for a in arms}
    out_hits = {o: [c["name"] for c, t in zip(constructs, texts) if o in find_outcomes(t)] for o in outcomes}
    return {
        "constructs": [c["name"] for c in constructs],
        "arms": _rate(sum(bool(v) for v in arm_hits.values()), len(arms)),
        "outcomes": _rate(sum(bool(v) for v in out_hits.values()), len(outcomes)),
        "arm_matches": arm_hits,
        "outcome_matches": out_hits,
    }


def score_dataset_links(
    gold: dict[str, Any], proposed: Iterable[tuple[int, int]], extracted: Iterable[int]
) -> dict[str, Any]:
    """AI-proposed shared-dataset pairs against same-study pairs.

    Only pairs of extracted reports can be proposed, so only those count.
    """
    extracted = set(extracted)
    study = study_of(gold)
    truth = {
        tuple(sorted((a, b)))
        for a in extracted for b in extracted
        if a < b and study.get(a) and study.get(a) == study.get(b)
    }
    found = {tuple(sorted(p)) for p in proposed}
    hits = truth & found
    return {
        "true_pairs": sorted(truth),
        "proposed_pairs": sorted(found),
        "precision": _rate(len(hits), len(found)),
        "recall": _rate(len(hits), len(truth)),
    }


def match_findings(gold: dict[str, Any], inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each pooled finding, the graded links that could express it.

    A link matches when one side names a treatment in the comparison and the
    other names the outcome. Every candidate is listed with its direction
    and grade; judging whether it says what the review found is left to the
    reader, because a construct pair holds one treatment and no comparator.
    """
    out = []
    for finding in gold.get("synthesis", {}).get("findings", []):
        treatments = {
            t.strip() for t in finding["comparison"].split(" vs ")
        } - {"no_treatment"}
        candidates = []
        for link in inventory:
            p_arms = find_arms(link.get("predictor", ""))
            o_outs = find_outcomes(link.get("outcome", ""))
            if p_arms & treatments and finding["outcome"] in o_outs:
                candidates.append(
                    {
                        "predictor": link.get("predictor"),
                        "outcome": link.get("outcome"),
                        "direction": link.get("direction"),
                        "grade": link.get("grade"),
                        "n_datasets": link.get("n_datasets"),
                    }
                )
        out.append({**finding, "links": candidates})
    return out


# -- the run -----------------------------------------------------------------


def _done(project: Project, step: str) -> bool:
    return project.db.get_meta(f"pipeline.step.{step}") == "done"


def _mark(project: Project, step: str) -> None:
    with project.db.transaction():
        project.db.set_meta(f"pipeline.step.{step}", "done")


def _spent(project: Project) -> float:
    row = project.db.query_one(
        "SELECT IFNULL(SUM(cost_usd), 0) AS c FROM ai_call WHERE cached = 0"
    )
    return float(row["c"]) if row else 0.0


def _fingerprint(records: list[PipelineRecord], protocol: str, gold: dict[str, Any]) -> str:
    import hashlib

    h = hashlib.sha256(protocol.strip().encode())
    for r in records:
        h.update(f"|{r.source_id}|{r.title}|{r.label}".encode())
    h.update(json.dumps(gold["studies"], sort_keys=True).encode())
    return h.hexdigest()


def _load(project: Project, records: list[PipelineRecord]) -> dict[int, int]:
    file_id = project.db.insert(
        "import_file",
        {
            "filename": "pipeline.csv",
            "original_path": "pipeline benchmark",
            "file_sha256": "pipeline-benchmark",
            "format": "csv",
            "dialect": "benchmark",
            "source_database": "Benchmark",
            "imported_at": utc_now(),
        },
    )
    ids: dict[int, int] = {}
    with project.db.transaction():
        for position, r in enumerate(records, start=1):
            ids[r.source_id] = project.db.insert(
                "record",
                {
                    "import_file_id": file_id,
                    "position_in_file": position,
                    "raw": json.dumps({"title": r.title, "abstract": r.abstract}),
                    "raw_format": "csv",
                    "record_hash": f"pipeline-{r.source_id}",
                    "created_at": utc_now(),
                    "title": r.title,
                    "abstract": r.abstract,
                    "year": r.year,
                    "doi": r.doi or None,
                    "accession": str(r.source_id),
                    "keywords": "[]",
                    "source_database": "Benchmark",
                },
            )
    return ids


def _id_maps(project: Project) -> tuple[dict[int, int], dict[int, int]]:
    to_local: dict[int, int] = {}
    for row in project.db.query("SELECT id, accession FROM record"):
        if row["accession"] and str(row["accession"]).isdigit():
            to_local[int(row["accession"])] = row["id"]
    return to_local, {v: k for k, v in to_local.items()}


def run_pipeline_benchmark(
    records: list[PipelineRecord],
    *,
    gold: dict[str, Any],
    pdf_dir: str | Path,
    protocol: str,
    client_factory: Callable[[Project, str], AIClient],
    workspace: str | Path,
    budget_usd: float = 10.0,
    max_tokens: dict[str, int] | None = None,
    project_info: dict[str, Any] | None = None,
    confirm: Callable[[CostEstimate, str], bool] | None = None,
    on_progress: ProgressFn | None = None,
) -> dict[str, Any]:
    """Carry *records* from full text to framework and score every stage.

    *client_factory(project, stage)* builds the client for each stage.
    *confirm(estimate, note)* is shown the estimate before the first model
    call; the run goes ahead only if it returns true.

    Returns the report as a dictionary. ``complete`` is false when a stage
    stopped early (a budget, a provider error); run again to carry on.
    """
    from ..extraction import ExtractionEngine
    from ..extraction.engine import ExtractionSettings
    from ..framework import FrameworkEngine
    from ..framework.engine import FrameworkSettings
    from ..fulltext import EligibilityEngine, RetrievalService
    from ..fulltext.engine import EligibilitySettings
    from ..fulltext.prompts import render_report
    from ..extraction.prompts import extraction_user
    from ..extraction.schemas import ExtractionAnswer
    from ..screening import ScreeningEngine, ScreeningSettings
    from ..synthesis import SynthesisEngine
    from ..synthesis.engine import SynthesisSettings

    if not protocol.strip():
        raise PipelineError(
            "A pipeline benchmark needs the published review's own eligibility "
            "criteria and aims. It will not write them."
        )
    tokens = {"fulltext": 2000, "extract": 6000, "synthesise": 6000, "framework": 8000}
    tokens.update(max_tokens or {})
    say = on_progress or (lambda stage, done, total: None)
    pdf_dir = Path(pdf_dir)
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    fingerprint = _fingerprint(
        records, protocol + json.dumps(project_info or {}, sort_keys=True), gold
    )
    clock = time.monotonic()
    labels = {r.source_id: r.label for r in records}
    stops: list[str] = []

    # A run with no full texts would mark every record "not retrieved", go
    # through every later stage with nothing to read, and write a report of
    # zeros. Refuse before anything is written.
    with_pdf = [r for r in records if (pdf_dir / f"{r.source_id}.pdf").exists()]
    if not any(r.label == 1 for r in with_pdf):
        raise PipelineError(
            f"{pdf_dir} holds no full text for any of the "
            f"{sum(r.label for r in records)} relevant records "
            f"({len(with_pdf)} PDFs in all). Collect the full texts first "
            "(for the Wilson benchmark, run fetch_oa.py), then run this again."
        )

    if (workspace / PROJECT_DB_NAME).exists():
        project = Project.open(workspace, actor=SCRIPT_ACTOR)
        if project.db.get_meta("pipeline.fingerprint") != fingerprint:
            project.close()
            raise PipelineError(
                f"The workspace {workspace} holds a different pipeline run. "
                "Give this one its own --workspace."
            )
    else:
        project = Project.create(workspace, "Pipeline benchmark", actor=SCRIPT_ACTOR)
        project.update_info(
            objectives=protocol.strip(),
            inclusion_criteria=protocol.strip(),
            searched_to=utc_now()[:10],
            **(project_info or {}),
        )
        project.protocol.lock(actor=SCRIPT_ACTOR)
        _load(project, records)
        with project.db.transaction():
            project.db.set_meta("pipeline.fingerprint", fingerprint)
            project.db.set_meta("pipeline.started_at", utc_now())

    try:
        to_local, to_source = _id_maps(project)
        model_name = None

        def client(stage: str) -> AIClient:
            nonlocal model_name
            c = client_factory(project, stage)
            model = getattr(c.provider, "model", "unknown")
            recorded = project.db.get_meta("pipeline.model")
            if recorded is None:
                with project.db.transaction():
                    project.db.set_meta("pipeline.model", model)
            elif recorded != model:
                raise PipelineError(
                    f"This run was started with {recorded!r}; it cannot carry on "
                    f"with {model!r}. Give the new model its own --workspace."
                )
            model_name = model
            c.set_budget(max(0.0, budget_usd - _spent(project)))
            return c

        # 1. Every sought record reached full text, by design.
        if not _done(project, "screen"):
            screen = ScreeningEngine(project, client("screen"), ScreeningSettings())
            if not screen.pilot_waived():
                screen.waive_pilot(
                    "Pipeline benchmark: the records were chosen by an earlier "
                    "title/abstract benchmark; this run starts at full text.",
                    actor=SCRIPT_ACTOR,
                )
            for local in to_local.values():
                screen.resolve(
                    local, "include",
                    reason="Pipeline benchmark: sent to full text by design.",
                    actor=SCRIPT_ACTOR,
                )
            _mark(project, "screen")

        # 2. Attach the open-access copies; mark the rest not retrieved.
        if not _done(project, "retrieve"):
            retrieval = RetrievalService(project)
            for source_id, local in to_local.items():
                path = pdf_dir / f"{source_id}.pdf"
                if path.exists():
                    outcome = retrieval.attach_pdf(path, record_id=local, actor=SCRIPT_ACTOR)
                    if not outcome.matched:
                        retrieval.set_status(
                            local, "not_retrieved",
                            reason=f"The PDF could not be read: {outcome.message}",
                            actor=SCRIPT_ACTOR,
                        )
                else:
                    retrieval.set_status(
                        local, "not_retrieved",
                        reason="No open-access copy found (OpenAlex, Europe PMC).",
                        actor=SCRIPT_ACTOR,
                    )
            _mark(project, "retrieve")

        retrieved = {
            to_source[r["record_id"]]
            for r in project.db.query("SELECT record_id FROM report WHERE status = 'retrieved'")
        }

        # 3. Cost estimate for the two bulk stages, before any call.
        ft_client = client("fulltext")
        eligibility = EligibilityEngine(
            project, ft_client,
            EligibilitySettings(max_tokens=tokens["fulltext"], budget_usd=None),
        )
        if not project.db.get_meta("pipeline.confirmed"):
            estimate = eligibility.estimate()
            ex_client = client("extract")
            extractor = ExtractionEngine(project, ex_client, ExtractionSettings())
            system = extractor._system()
            prompts = []
            for sid in sorted(retrieved):
                if labels.get(sid) != 1:
                    continue
                pages = extractor._pages(to_local[sid])
                text, _ = render_report(pages, max_chars=70_000, head_pages=8)
                record = dict(project.db.query_one("SELECT * FROM record WHERE id = ?", (to_local[sid],)))
                prompts.append((system, extraction_user(record, text)))
            ex_est = ex_client.estimate(prompts, schema=ExtractionAnswer, expected_completion_tokens=1500)
            total = CostEstimate(
                n_calls=estimate.n_calls + ex_est.n_calls * 4,
                prompt_tokens=estimate.prompt_tokens + ex_est.prompt_tokens + ex_est.n_calls * 3000,
                completion_tokens=estimate.completion_tokens + ex_est.completion_tokens + ex_est.n_calls * 600,
                cost_usd=round(estimate.cost_usd + ex_est.cost_usd * 1.4, 4),
                price_known=estimate.price_known and ex_est.price_known,
                model=estimate.model,
                n_cached=estimate.n_cached + ex_est.n_cached,
            )
            note = (
                f"Full text: {estimate.n_calls} reports, about US${estimate.cost_usd:.2f}. "
                f"Extraction: {ex_est.n_calls} included reports, about "
                f"US${ex_est.cost_usd:.2f} plus the direction checks. Synthesis and "
                f"framework add a few dozen calls. Limit: US${budget_usd:.2f} in all."
            )
            if confirm is not None and not confirm(total, note):
                raise PipelineCancelled("The cost estimate was declined. No model call was made.")
            with project.db.transaction():
                project.db.set_meta("pipeline.confirmed", utc_now())

        # 4. Full-text eligibility: the AI first pass.
        if not _done(project, "fulltext"):
            ft_client.set_budget(max(0.0, budget_usd - _spent(project)))
            outcome = eligibility.run(
                actor=SCRIPT_ACTOR,
                on_progress=lambda d, n, m: say("full text", d, n),
            )
            if outcome.status == "paused" or eligibility.pending_records():
                stops.extend(outcome.messages or ["Full-text assessment stopped early."])
                raise _Stop()
            _mark(project, "fulltext")

        ai_ft = _latest_ai_fulltext(project)
        queued_ft = {
            r["record_id"]
            for r in project.db.query("SELECT DISTINCT record_id FROM review_queue WHERE stage = 'fulltext'")
        }

        # 5. The script settles full text by the review's final inclusion.
        if not _done(project, "fulltext_settled"):
            first_code = (project.exclusion_codes() or [{"code": "E1"}])[0]["code"]
            for sid in sorted(retrieved):
                local = to_local[sid]
                ai = ai_ft.get(local) or {}
                if labels[sid] == 1:
                    eligibility.resolve(
                        local, "include", actor=SCRIPT_ACTOR,
                        reason="Benchmark script: included by the original review team (gold standard).",
                    )
                else:
                    eligibility.resolve(
                        local, "exclude", actor=SCRIPT_ACTOR,
                        exclusion_code=(ai.get("exclusion_code") if ai.get("decision") == "exclude" else "") or first_code,
                        reason="Benchmark script: not included by the original review team (gold standard).",
                    )
            _mark(project, "fulltext_settled")

        # 6. Extraction: the AI first pass, then accepted unread.
        extractor = ExtractionEngine(
            project, client("extract"),
            ExtractionSettings(max_tokens=tokens["extract"], second_coder_fraction=0.0, second_coder_minimum=0),
        )
        if not _done(project, "extract"):
            outcome = extractor.run(
                actor=SCRIPT_ACTOR, on_progress=lambda d, n, m: say("extraction", d, n)
            )
            if outcome.status == "paused" or extractor.pending_records():
                stops.extend(outcome.messages or ["Extraction stopped early."])
                raise _Stop()
            _mark(project, "extract")
        if not _done(project, "extract_settled"):
            for item in extractor.review_queue(limit=5000):
                extractor.resolve(item["record_id"], ACCEPTED_UNREAD, actor=SCRIPT_ACTOR)
            _mark(project, "extract_settled")

        # 7. Synthesis.
        synth = SynthesisEngine(
            project, client("synthesise"), SynthesisSettings(max_tokens=tokens["synthesise"])
        )
        steps: list[tuple[str, Callable[[], Any]]] = [
            ("codebook", lambda: synth.propose_codebook(actor=SCRIPT_ACTOR)),
            ("codebook_approved", lambda: synth.approve_codebook(actor=SCRIPT_ACTOR, note=ACCEPTED_UNREAD)),
            ("mapping", lambda: synth.map_labels(actor=SCRIPT_ACTOR)),
            ("mapping_settled", lambda: [
                synth.confirm_mapping(m["id"], reason=ACCEPTED_UNREAD, actor=SCRIPT_ACTOR)
                for m in synth.contestable_mappings()
            ]),
            ("orientation", lambda: [
                synth.set_orientation(c["id"], "aligned", actor=SCRIPT_ACTOR)
                for c in synth.constructs_without_orientation()
            ]),
            ("harmonise", lambda: synth.harmonise(actor=SCRIPT_ACTOR)),
            ("links", lambda: synth.find_dataset_links(actor=SCRIPT_ACTOR)),
            ("links_settled", lambda: _settle_links(synth, gold, to_source)),
            ("datasets", lambda: synth.build_datasets(actor=SCRIPT_ACTOR)),
            ("grade", lambda: synth.grade(actor=SCRIPT_ACTOR)),
            ("sensitivity", lambda: synth.run_sensitivity(actor=SCRIPT_ACTOR)),
            ("contradictions", lambda: synth.explain_contradictions(actor=SCRIPT_ACTOR)),
        ]
        # 8. Framework.
        frame = FrameworkEngine(
            project, client("framework"), FrameworkSettings(max_tokens=tokens["framework"])
        )
        steps += [
            ("themes", lambda: frame.propose_themes(actor=SCRIPT_ACTOR)),
            ("themes_settled", lambda: [
                frame.decide_theme(t["id"], approved=True, reason=ACCEPTED_UNREAD, actor=SCRIPT_ACTOR)
                for t in frame.themes() if t.get("status") == "proposed"
            ]),
            ("propositions", lambda: frame.draft_propositions(actor=SCRIPT_ACTOR)),
            ("propositions_settled", lambda: [
                frame.decide_proposition(p["id"], accepted=True, reason=ACCEPTED_UNREAD, actor=SCRIPT_ACTOR)
                for p in frame.propositions() if p.get("status") == "proposed"
            ]),
            ("evidence_maps", lambda: frame.draw_evidence_maps(actor=SCRIPT_ACTOR)),
            ("diagram", lambda: frame.draw_integrated_diagram(actor=SCRIPT_ACTOR)),
        ]
        failures: dict[str, str] = json.loads(project.db.get_meta("pipeline.failures", "{}") or "{}")
        for name, step in steps:
            if _done(project, name) or name in failures:
                continue
            say(name, 0, 1)
            try:
                step()
            except Exception as exc:  # a stage the evidence cannot support is a finding
                from ..ai.client import BudgetExceeded
                from ..ai.base import ProviderError

                if isinstance(exc, (BudgetExceeded, ProviderError)):
                    stops.append(f"{name}: {exc}")
                    raise _Stop() from exc
                failures[name] = f"{type(exc).__name__}: {exc}"
                with project.db.transaction():
                    project.db.set_meta("pipeline.failures", json.dumps(failures))
                continue
            _mark(project, name)
        complete = True
    except _Stop:
        complete = False

    seconds = float(project.db.get_meta("pipeline.seconds", "0") or 0) + (time.monotonic() - clock)
    with project.db.transaction():
        project.db.set_meta("pipeline.seconds", f"{seconds:.1f}")
    report = _report(project, gold, labels, to_local, to_source)
    report.update(
        complete=complete,
        stopped=stops,
        failures=json.loads(project.db.get_meta("pipeline.failures", "{}") or "{}"),
        model=project.db.get_meta("pipeline.model"),
        seconds=round(seconds, 1),
        budget_usd=budget_usd,
        protocol=protocol.strip(),
    )
    project.close()
    return report


class _Stop(Exception):
    pass


def _settle_links(synth, gold: dict[str, Any], to_source: dict[int, int]) -> None:
    study = study_of(gold)
    for link in synth.proposed_links():
        a = study.get(to_source.get(link["record_a"], -1))
        b = study.get(to_source.get(link["record_b"], -1))
        same = a is not None and a == b
        synth.decide_link(
            link["id"], confirmed=same, actor=SCRIPT_ACTOR,
            reason=(
                "Benchmark script: both reports belong to the same study in the "
                "review's Table 1 (gold standard)."
                if same else
                "Benchmark script: the review treats these as different studies (gold standard)."
            ),
        )


def _latest_ai_fulltext(project: Project) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in project.db.query(
        "SELECT * FROM fulltext_decision WHERE reviewer_role != 'human' ORDER BY id"
    ):
        out[row["record_id"]] = dict(row)
    return out


def _found_on(row: dict[str, Any]) -> str:
    if not row.get("quote"):
        return "none"
    # quote_verified: found on the page the answer cited. quote_found_on: the
    # page it was found on at all, or nothing when it is not in the report.
    if row.get("quote_verified"):
        return "cited"
    if row.get("quote_found_on") is not None:
        return "elsewhere"
    return "nowhere"


def _report(project, gold, labels, to_local, to_source) -> dict[str, Any]:
    db = project.db
    retrieved = {
        to_source[r["record_id"]]
        for r in db.query("SELECT record_id FROM report WHERE status = 'retrieved'")
    }
    ai_ft = _latest_ai_fulltext(project)
    queued = {
        r["record_id"]
        for r in db.query("SELECT DISTINCT record_id FROM review_queue WHERE stage = 'fulltext'")
    }
    rows = []
    for sid, local in to_local.items():
        ai = ai_ft.get(local)
        rows.append(
            {
                "source_id": sid,
                "label": labels[sid],
                "retrieved": sid in retrieved,
                "ai_decision": ai["decision"] if ai else None,
                "exclusion_code": ai["exclusion_code"] if ai else None,
                "reason": ai["reason"] if ai else None,
                "quote_found_on": _found_on(ai) if ai else None,
                "queued": local in queued,
            }
        )
    titles = {to_source[r["id"]]: r["title"] for r in db.query("SELECT id, title FROM record")}
    fulltext = score_fulltext(rows)
    fulltext["lost_detail"] = [
        {"source_id": sid, "title": titles.get(sid, ""),
         "code": next(r["exclusion_code"] for r in rows if r["source_id"] == sid),
         "reason": next(r["reason"] for r in rows if r["source_id"] == sid)}
        for sid in fulltext["lost"]
    ]

    extractions: dict[int, dict[str, Any]] = {}
    for row in db.query("SELECT * FROM report_extraction WHERE coder_role = 'ai' ORDER BY id"):
        extractions[to_source[row["record_id"]]] = dict(row)
    relationships: dict[int, list[dict[str, Any]]] = {}
    for row in db.query("SELECT * FROM relationship WHERE coder_role = 'ai' ORDER BY id"):
        relationships.setdefault(to_source[row["record_id"]], []).append(dict(row))
    for rows_ in relationships.values():
        for rel in rows_:
            rel["quote_found_on"] = _found_on(rel)
    constructs = [dict(r) for r in db.query("SELECT * FROM construct WHERE status = 'approved' ORDER BY position, id")]
    if gold.get("kind") == "sobc":
        names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM construct")}
        coded: dict[int, set[str]] = {}
        for row in db.query(
            "SELECT record_id, predictor_construct, outcome_construct, "
            "mediator_construct, moderator_construct FROM harmonised_relationship"
        ):
            got = coded.setdefault(to_source[row["record_id"]], set())
            for col in ("predictor_construct", "outcome_construct", "mediator_construct", "moderator_construct"):
                code = construct_code(names.get(row[col], "")) if row[col] else None
                if code:
                    got.add(code)
        extraction = score_sobc(gold, extractions, relationships, coded)
        codebook = {"constructs": [c["name"] for c in constructs]}
    else:
        extraction = score_extraction(gold, extractions, relationships)
        covered = [s["study"] for s in extraction["per_study"]]
        codebook = score_codebook(gold, constructs, covered)
    proposed = [
        (to_source[r["record_a"]], to_source[r["record_b"]])
        for r in db.query("SELECT record_a, record_b FROM dataset_link")
    ]
    links = score_dataset_links(gold, proposed, extractions.keys())
    inventory = [dict(r) for r in db.query("SELECT * FROM v_graded_inventory")]
    findings = match_findings(gold, inventory)

    props = [dict(r) for r in db.query("SELECT * FROM proposition ORDER BY number, id")]
    prop_links = db.query(
        "SELECT pl.proposition_id, COUNT(*) AS n FROM proposition_link pl GROUP BY pl.proposition_id"
    )
    n_cited = {r["proposition_id"]: r["n"] for r in prop_links}
    themes = [dict(r) for r in db.query("SELECT * FROM theme ORDER BY position, id")]
    calls = db.query_one(
        "SELECT COUNT(*) AS n, IFNULL(SUM(prompt_tokens),0) AS tin, "
        "IFNULL(SUM(completion_tokens),0) AS tout, IFNULL(SUM(cost_usd),0) AS cost "
        "FROM ai_call WHERE cached = 0"
    )
    by_stage = [
        dict(r) for r in db.query(
            "SELECT stage, COUNT(*) AS n, ROUND(SUM(cost_usd), 4) AS cost "
            "FROM ai_call WHERE cached = 0 GROUP BY stage ORDER BY MIN(id)"
        )
    ]
    return {
        "fulltext": fulltext,
        "extraction": extraction,
        "codebook": codebook,
        "dataset_links": links,
        "inventory": [
            {k: g.get(k) for k in ("predictor", "outcome", "direction", "grade", "n_datasets", "n_reports")}
            for g in inventory
        ],
        "findings": findings,
        "framework": {
            "themes": [{"name": t["name"], "status": t["status"]} for t in themes],
            "propositions": [
                {"number": p["number"], "statement": p["statement"], "status": p["status"],
                 "n_links": n_cited.get(p["id"], 0)}
                for p in props
            ],
        },
        "calls": dict(calls) if calls else {},
        "calls_by_stage": by_stage,
    }


# -- the report ------------------------------------------------------------------


def _pct(rate: dict[str, Any] | None) -> str:
    if not rate or rate.get("rate") is None:
        return "—"
    low, high = rate["ci"]
    return f"{rate['rate'] * 100:.1f}% ({rate['k']}/{rate['n']}; 95% CI {low * 100:.1f}–{high * 100:.1f})"


def render_pipeline_report(report: dict[str, Any]) -> str:
    ft, ex, cb = report["fulltext"], report["extraction"], report["codebook"]
    lines = [
        "# Pipeline benchmark: full text to framework",
        "",
        f"Model: {report.get('model')}. Complete: {'yes' if report.get('complete') else 'no'}. "
        f"Time: {report.get('seconds', 0) / 60:.0f} min. "
        f"Calls: {report['calls'].get('n', 0)}, cost US${report['calls'].get('cost', 0):.2f} "
        f"(limit US${report.get('budget_usd', 0):.2f}).",
        "",
        "A script played the reviewer: it accepted every AI proposal unread, except "
        "full-text eligibility and shared datasets, which it settled from the gold standard.",
        "",
    ]
    if report.get("stopped"):
        lines += ["**Stopped early:** " + "; ".join(report["stopped"]), ""]
    if report.get("failures"):
        lines += ["**Steps that failed:**", ""]
        lines += [f"- {k}: {v}" for k, v in report["failures"].items()] + [""]
    lines += [
        "## Full-text eligibility",
        "",
        f"Sought {ft['sought']} ({ft['sought_relevant']} relevant); retrieved "
        f"{ft['retrieved']} ({ft['retrieved_relevant']} relevant); assessed {ft['assessed']}.",
        "",
        f"- Recall (include or unclear): {_pct(ft['recall'])}",
        f"- Strict recall (include): {_pct(ft['strict_recall'])}",
        f"- Specificity: {_pct(ft['specificity'])}",
        f"- Sent to a human: {_pct(ft['queued'])}",
        f"- Decisions: {ft['decisions']}",
        f"- Quotations: {ft['quotes']}",
        "",
    ]
    if ft.get("lost_detail"):
        lines += ["Relevant reports the AI excluded:", ""]
        lines += [f"- [{d['source_id']}] {d['title']} — {d['code']}: {d['reason']}" for d in ft["lost_detail"]]
        lines += [""]
    if ex.get("kind") == "sobc":
        lines += _sobc_lines(ex)
    else:
        lines += [
            "## Extraction",
            "",
            f"Studies with an extracted report: {ex['studies_covered']} of {ex['studies_total']}.",
            "",
            f"- Design family correct: {_pct(ex['design'])}",
            f"- Sample size correct: {_pct(ex['sample_size'])}",
            f"- Treatment arms found: {_pct(ex['arms'])}",
            f"- Outcomes found: {_pct(ex['outcomes'])}",
            f"- Relationships: {ex['relationships']}; quotations {ex['relationship_quotes']}",
            f"- Direction re-check agreed: {_pct(ex['direction_check'])}",
            f"- Relationships queued: {_pct(ex['relationships_queued'])}; reports queued: {_pct(ex['reports_queued'])}",
            "",
            "| Study | Design (gold → AI) | n (gold) → AI | Arms found | Outcomes found | Relationships |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for s in ex["per_study"]:
            lines.append(
                f"| {s['study']} | {s['design_gold']} → {', '.join(s['design_family'])} | "
                f"{s['n_gold'][0]}/{s['n_gold'][1]} → {', '.join(map(str, s['sample_size'])) or '—'} | "
                f"{len(s['arms_found'])}/{len(s['arms_gold'])} | "
                f"{len(s['outcomes_found'])}/{len(s['outcomes_gold'])} | {s['n_relationships']} |"
            )
    lines += [
        "",
        "## Synthesis",
        "",
        f"Codebook: {len(cb['constructs'])} constructs."
        + (f" Arms covered {_pct(cb['arms'])}; outcomes covered {_pct(cb['outcomes'])}."
           if "arms" in cb else ""),
        "",
        "Constructs: " + "; ".join(cb["constructs"]),
        "",
        f"Shared datasets: precision {_pct(report['dataset_links']['precision'])}, "
        f"recall {_pct(report['dataset_links']['recall'])}.",
        "",
        "Graded links:",
        "",
        "| Predictor | Outcome | Direction | Grade | Datasets |",
        "| --- | --- | --- | --- | --- |",
    ]
    for g in report["inventory"]:
        lines.append(f"| {g['predictor']} | {g['outcome']} | {g['direction']} | {g['grade']} | {g['n_datasets']} |")
    lines += ["", "Pooled findings of the review and the graded links that could express them:", ""]
    for f in report["findings"]:
        found = "; ".join(
            f"{l['predictor']} → {l['outcome']} ({l['direction']}, {l['grade']})" for l in f["links"]
        ) or "no matching link"
        lines.append(f"- {f['comparison']}, {f['outcome']}: review {f['verdict']}. App: {found}")
    fw = report["framework"]
    lines += [
        "",
        "## Framework",
        "",
        f"Themes: {', '.join(t['name'] for t in fw['themes']) or 'none'}.",
        "",
    ]
    for p in fw["propositions"]:
        lines.append(f"- P{p['number']} ({p['n_links']} links, {p['status']}): {p['statement']}")
    lines += ["", "## Cost by stage", ""]
    lines += [f"- {s['stage']}: {s['n']} calls, US${s['cost'] or 0:.2f}" for s in report["calls_by_stage"]]
    return "\n".join(lines) + "\n"


def _sobc_lines(ex: dict[str, Any]) -> list[str]:
    lines = [
        "## Extraction and S-O-B-C coding",
        "",
        f"Included studies with an extracted report: {ex['studies_covered']} of {ex['studies_total']}.",
        f"Relationships: {ex['relationships']}; quotations {ex['relationship_quotes']}.",
        f"Direction re-check agreed: {_pct(ex['direction_check'])}. "
        f"Relationships queued: {_pct(ex['relationships_queued'])}; reports queued: {_pct(ex['reports_queued'])}.",
        "",
    ]
    for label, b in (("All covered studies (gold coded with AI help)", ex["all"]),
                     ("Studies whose gold coding the author verified", ex.get("verified"))):
        if not b:
            continue
        lines += [
            f"### {label} (n = {b['n']})",
            "",
            f"- Codes recalled: {_pct(b['recall'])}",
            f"- Codes correct (precision): {_pct(b['precision'])}",
        ]
        for g, r in b["by_group"].items():
            lines.append(f"- {g}: recall {_pct(r['recall'])}; precision {_pct(r['precision'])}")
        lines += [
            f"- Broad design correct: {_pct(b['design'])}",
            f"- Appraisal concern matches the MMAT band: {_pct(b['appraisal'])}",
            f"- MMAT chosen as the instrument: {_pct(b['mmat_used'])}",
            "",
            "| Code | Gold | App | Both | kappa |",
            "| --- | --- | --- | --- | --- |",
        ]
        for code, c in b["per_code"].items():
            k = "—" if c["kappa"] is None else f"{c['kappa']:.2f}"
            lines.append(f"| {code} | {c['gold']} | {c['app']} | {c['both']} | {k} |")
        lines.append("")
    return lines
