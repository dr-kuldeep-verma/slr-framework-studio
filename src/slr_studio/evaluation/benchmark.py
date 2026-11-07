"""Run this application's screening against a labelled benchmark.

What this does: builds a throwaway project, loads a labelled corpus into
it, screens it with the real :class:`ScreeningEngine` and the real prompts,
then compares what the application decided against what the human
reviewers decided.

What it deliberately does not do:

**It does not write the protocol.** Screening quality depends more on the
eligibility criteria than on almost anything else, so a harness that
composed its own protocol would be tuning the benchmark until the tool
passed it. The protocol has to be supplied, taken from the published
review the dataset came from, and it is stored with the result so that a
reader can see what the model was actually asked.

**It does not import through the importers, or deduplicate.** The gold
labels are aligned to rows in a file; deduplication would remove records
and silently break that alignment, and the question here is how well the
*screener* performs, not the parser. Records go straight into the table.

**It does not run the pilot.** The pilot exists so a human can check the
AI against their own labels before spending money on the full corpus. Here
the entire gold standard plays that role, so the pilot is waived with that
recorded as the reason.

Nothing in this module runs during the test suite against a real provider.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..ai.client import AIClient, CostEstimate
from ..core.hashing import sha256_text
from ..core.project import PROJECT_DB_NAME, Project
from ..core.db import utc_now
from ..screening.engine import CATEGORIES, ScreeningEngine, ScreeningSettings
from ..screening.stats import agreement as _agreement
from .datasets import BenchmarkDataset
from .metrics import ScreeningScore, score_screening

__all__ = ["BenchmarkResult", "run_benchmark", "BenchmarkError", "BenchmarkCancelled"]

ProgressFn = Callable[[int, int, str], None]


class BenchmarkError(RuntimeError):
    pass


class BenchmarkCancelled(BenchmarkError):
    """The cost estimate was shown and declined. Nothing was spent."""


@dataclass
class BenchmarkResult:
    """A score, and everything needed to reproduce or dispute it."""

    dataset: dict[str, Any]
    score: ScreeningScore
    protocol: str
    model: str
    provider: str
    started_at: str
    finished_at: str
    seconds: float
    n_ai_calls: int
    tokens_in: int
    tokens_out: int
    cost: float
    #: Relevant records the screener threw away, titles and all. The whole
    #: point of the exercise: "92% recall" is a number, and these are the
    #: papers that number cost.
    lost_records: list[dict[str, Any]] = field(default_factory=list)
    agreement: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    #: False when the run stopped before every record was screened (a
    #: budget, a rate limit). An incomplete result is not a result: run the
    #: benchmark again on the same workspace to finish it.
    complete: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "complete": self.complete,
            "dataset": self.dataset,
            "score": self.score.as_dict(),
            "protocol": self.protocol,
            "model": self.model,
            "provider": self.provider,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "seconds": round(self.seconds, 1),
            "n_ai_calls": self.n_ai_calls,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost": round(self.cost, 4),
            "lost_records": self.lost_records,
            "agreement": self.agreement,
            "notes": self.notes,
        }


def _fingerprint(dataset: BenchmarkDataset, protocol: str) -> str:
    """What a resumed run must match: the records, their labels, the protocol."""
    parts = [protocol.strip()]
    parts += [f"{r.title}\x1f{r.abstract}\x1f{r.relevant}" for r in dataset.records]
    return sha256_text("\x1e".join(parts))


def _screener_agreement(project: Project) -> dict[str, Any] | None:
    """Screener A against screener B over every record both judged.

    Computed from the stored decisions rather than taken from the last run,
    so a benchmark finished over several days reports agreement over all of
    it, not over the final day's share.
    """
    latest: dict[tuple[int, str], str] = {}
    for row in project.db.query(
        "SELECT record_id, reviewer_role, decision FROM screening_decision "
        "WHERE reviewer_role IN ('screener_a', 'screener_b') ORDER BY id"
    ):
        latest[(row["record_id"], row["reviewer_role"])] = row["decision"]
    ids = sorted({rid for rid, _ in latest})
    pairs = [
        (latest[(rid, "screener_a")], latest[(rid, "screener_b")])
        for rid in ids
        if (rid, "screener_a") in latest and (rid, "screener_b") in latest
    ]
    if not pairs:
        return None
    a, b = zip(*pairs)
    return _agreement(list(a), list(b), categories=list(CATEGORIES)).as_dict()


def _load_records(project: Project, dataset: BenchmarkDataset) -> list[int]:
    """Put the corpus into the project, in order. Returns the record ids."""
    file_id = project.db.insert(
        "import_file",
        {
            "filename": f"{dataset.name}.csv",
            "original_path": dataset.source or dataset.name,
            "file_sha256": f"benchmark-{dataset.name}",
            "format": "csv",
            "dialect": "benchmark",
            "source_database": "Benchmark",
            "imported_at": utc_now(),
        },
    )
    ids: list[int] = []
    with project.db.transaction():
        for position, record in enumerate(dataset.records, start=1):
            ids.append(
                project.db.insert(
                    "record",
                    {
                        "import_file_id": file_id,
                        "position_in_file": position,
                        "raw": json.dumps(
                            {"title": record.title, "abstract": record.abstract}
                        ),
                        "raw_format": "csv",
                        "record_hash": f"benchmark-{dataset.name}-{record.source_row}",
                        "created_at": utc_now(),
                        "title": record.title,
                        "abstract": record.abstract,
                        "year": record.year,
                        "doi": record.doi or None,
                        "keywords": json.dumps(
                            [k.strip() for k in record.keywords.split(";") if k.strip()]
                        ),
                        "source_database": "Benchmark",
                    },
                )
            )
    return ids


def run_benchmark(
    dataset: BenchmarkDataset,
    *,
    protocol: str,
    client_factory: Callable[[Project], AIClient],
    workspace: str | Path,
    inclusion_criteria: str = "",
    settings: ScreeningSettings | None = None,
    on_progress: ProgressFn | None = None,
    actor: str = "human:benchmark",
    confirm: Callable[[CostEstimate], bool] | None = None,
) -> BenchmarkResult:
    """Screen *dataset* and score the result against its labels.

    *protocol* is the eligibility wording, taken from the published review
    the dataset came from. It is required: see the module docstring.

    *client_factory* builds the :class:`AIClient` for a project, so the
    caller chooses the provider. Passing a mock provider gives a run that
    costs nothing and tests the harness; passing a real one gives a result
    worth reporting.

    *confirm*, when given, is shown the estimate for the whole run in
    tokens and money before the first call, and the run goes ahead only if
    it returns true. Otherwise :class:`BenchmarkCancelled` is raised and
    nothing has been spent.
    """
    if not protocol.strip():
        raise BenchmarkError(
            "A benchmark needs the protocol the records should be judged "
            "against, taken from the published review the dataset came "
            "from. This harness will not write one: the eligibility wording "
            "drives the result more than almost anything else, and a "
            "harness that composed its own would be tuning the benchmark "
            "until the tool passed it."
        )

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    fingerprint = _fingerprint(dataset, protocol)
    clock = time.monotonic()

    resumed = (workspace / PROJECT_DB_NAME).exists()
    if resumed:
        # Carry on a run that stopped (a daily quota, a budget, a closed
        # window). Everything already decided is kept, and every answer
        # already bought is in the cache.
        project = Project.open(workspace, actor=actor)
        if project.db.get_meta("benchmark.fingerprint") != fingerprint:
            project.close()
            raise BenchmarkError(
                f"The workspace {workspace} holds a benchmark of a different "
                "dataset or protocol. Resume it with the same files, or give "
                "this run a new workspace."
            )
        started = datetime.fromisoformat(
            project.db.get_meta("benchmark.started_at") or utc_now()
        )
        prior_seconds = float(project.db.get_meta("benchmark.seconds", "0") or 0)
    else:
        project = Project.create(
            workspace, f"Benchmark: {dataset.name}", actor=actor
        )
        started = datetime.now(timezone.utc)
        prior_seconds = 0.0

    try:
        if not resumed:
            project.update_info(
                objectives=protocol.strip(),
                inclusion_criteria=(inclusion_criteria or protocol).strip(),
                searched_to=started.date().isoformat(),
            )
            project.protocol.lock(actor=actor)
            _load_records(project, dataset)
            with project.db.transaction():
                project.db.set_meta("benchmark.fingerprint", fingerprint)
                project.db.set_meta(
                    "benchmark.started_at", started.isoformat(timespec="seconds")
                )

        client = client_factory(project)
        model = getattr(client.provider, "model", "unknown")
        recorded = project.db.get_meta("benchmark.model")
        if recorded is None:
            with project.db.transaction():
                project.db.set_meta("benchmark.model", model)
        elif recorded != model:
            raise BenchmarkError(
                f"This run was started with {recorded!r} and cannot be "
                f"resumed with {model!r}: its score would mix two models' "
                "decisions and report them under one name. Give the new model "
                "its own --workspace."
            )
        engine = ScreeningEngine(
            project, client, settings or ScreeningSettings()
        )
        if confirm is not None and not confirm(engine.estimate_full_run()):
            raise BenchmarkCancelled(
                "The cost estimate was declined. No model call was made."
            )
        if not engine.pilot_waived():
            engine.waive_pilot(
                "Benchmark run: the dataset's own human decisions are the gold "
                "standard this run is scored against, so a separate pilot "
                "would check the AI against the same labels twice.",
                actor=actor,
            )
        outcome = engine.run_full(actor=actor, on_progress=on_progress)

        # Read the decisions back in the order the records went in, so they
        # line up with the gold labels by position.
        rows = project.db.query(
            "SELECT r.id, r.position_in_file, s.decision "
            "FROM record r LEFT JOIN screening_status s ON s.record_id = r.id "
            "ORDER BY r.position_in_file"
        )
        if len(rows) != len(dataset.records):
            raise BenchmarkError(
                f"{len(rows)} records came back for {len(dataset.records)} "
                "in the dataset. Scoring a misaligned set would produce a "
                "number that looks right and is not."
            )

        decisions: list[str] = []
        undecided = 0
        for row in rows:
            decision = row["decision"]
            if decision is None:
                # A paused or budget-stopped run leaves records unscreened.
                # Counted as 'unclear': not screened is not excluded, and
                # calling it so would invent recall the run did not earn.
                undecided += 1
                decision = "unclear"
            decisions.append(decision)

        gold = [r.relevant for r in dataset.records]
        score = score_screening(gold=gold, decisions=decisions)

        lost = [
            {
                "title": dataset.records[i].title,
                "year": dataset.records[i].year,
                "doi": dataset.records[i].doi,
                "had_abstract": dataset.records[i].has_abstract,
                "source_row": dataset.records[i].source_row,
            }
            for i in score.lost_indices
        ]

        notes = list(dataset.notes)
        if undecided:
            notes.append(
                f"{undecided} record(s) were never screened -- the run "
                "stopped early. They are scored as 'unclear', which is the "
                "cautious reading; the figures below are not a complete run."
            )
        if outcome.status != "finished":
            notes.append(f"Run status: {outcome.status}. {' '.join(outcome.messages)}")

        usage = project.db.query_one(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(prompt_tokens), 0) AS tin, "
            "COALESCE(SUM(completion_tokens), 0) AS tout, "
            "COALESCE(SUM(cost_usd), 0) AS cost FROM ai_call"
        )
        finished = datetime.now(timezone.utc)
        seconds = prior_seconds + (time.monotonic() - clock)
        with project.db.transaction():
            project.db.set_meta("benchmark.seconds", f"{seconds:.1f}")

        return BenchmarkResult(
            dataset=dataset.characteristics(),
            score=score,
            protocol=protocol.strip(),
            model=getattr(client.provider, "model", "unknown"),
            provider=getattr(client.provider, "name", "") or type(client.provider).__name__,
            started_at=started.isoformat(timespec="seconds"),
            finished_at=finished.isoformat(timespec="seconds"),
            seconds=seconds,
            n_ai_calls=int(usage["n"] or 0),
            tokens_in=int(usage["tin"] or 0),
            tokens_out=int(usage["tout"] or 0),
            cost=float(usage["cost"] or 0.0),
            lost_records=lost,
            agreement=_screener_agreement(project),
            notes=notes,
            complete=not undecided and outcome.status == "finished",
        )
    finally:
        project.close()
