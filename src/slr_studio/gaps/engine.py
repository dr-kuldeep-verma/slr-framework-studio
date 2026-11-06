"""The Gap Finder over a project: read the corpus, find the gaps, rank them.

The detectors, the statements, the scoring and the freshness check are all
elsewhere and all free of the database. This module reads a project into the
snapshot they expect, stores what comes back, and keeps two promises that the
pure code cannot keep on its own:

**A reviewer's decision outlives a re-run.** Finding the gaps again replaces
the register, which is right -- the corpus has changed -- but a gap the
reviewer rejected, rescored or drafted a study for comes back with those
decisions intact, matched on its type and cell. Losing a judgement because
the analysis was re-run would teach a reviewer never to re-run it.

**A statement is never stored without its evidence.** `write_statement`
refuses to produce the prose, and this module lets that refusal through
rather than catching it and storing a bare claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Sequence

from ..ai.client import AIClient, BudgetExceeded, CostEstimate
from ..core.db import utc_now
from .detect import (
    Corpus,
    Dataset,
    GapCandidate,
    Link,
    Report,
    Statement,
    detect_all,
)
from .freshness import FreshnessChecker
from .gapmap import Grid, build_grid, write_excel, write_pdf, write_png, write_svg
from .priority import Scores, Weights, rank, score
from .scan import Document, cluster, co_occurrence, trend, vectorise
from .statements import UnsupportedGap, write_statement

__all__ = ["GapEngine", "GapError", "GapSettings", "StepOutcome"]


class GapError(RuntimeError):
    """Raised when the Gap Finder is asked for something it cannot ground."""


@dataclass
class GapSettings:
    max_tokens: int = 3000
    budget_usd: float | None = None
    #: How many topics the quick scan looks for. None lets it choose.
    n_topics: int | None = None
    #: Keyword pairs below this count are left out of the co-occurrence list.
    min_co_occurrence: int = 2
    weights: Weights = field(default_factory=Weights)
    #: Cells with at most this many datasets count as thin.
    thin_cell: int = 2

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "budget_usd": self.budget_usd,
            "n_topics": self.n_topics,
            "min_co_occurrence": self.min_co_occurrence,
            "weights": self.weights.as_dict(),
            "thin_cell": self.thin_cell,
        }


@dataclass
class StepOutcome:
    step: str
    status: str = "finished"
    counts: dict[str, int] = field(default_factory=dict)
    n_calls: int = 0
    n_cached: int = 0
    cost_usd: float = 0.0
    messages: list[str] = field(default_factory=list)

    _PHRASES: ClassVar[dict[str, dict[str, str]]] = {
        "quick_scan": {
            "n_records": "{} record(s)",
            "n_topics": "{} topic(s)",
            "n_pairs": "{} keyword pair(s)",
        },
        "gaps": {
            "n_candidates": "{} candidate(s)",
            "n_gaps": "{} gap(s) recorded",
            "n_unsupported": "{} refused for want of evidence",
            "n_decisions_kept": "{} reviewer decision(s) carried over",
        },
        "freshness": {
            "n_checked": "{} gap(s) checked",
            "n_possibly_addressed": "{} possibly being addressed",
            "n_failed": "{} check(s) failed",
        },
        "studies": {"n_drafted": "{} study(ies) drafted"},
    }

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "status": self.status,
            "counts": dict(self.counts),
            "n_calls": self.n_calls,
            "n_cached": self.n_cached,
            "cost_usd": round(self.cost_usd, 6),
            "messages": list(self.messages),
        }

    def describe(self) -> str:
        head = self.step.replace("_", " ").capitalize()
        if self.status != "finished":
            head += f" ({self.status})"
        phrases = self._PHRASES.get(self.step, {})
        parts = [
            phrase.format(self.counts[key])
            for key, phrase in phrases.items()
            if key in self.counts
        ]
        lines = [f"{head}: " + (", ".join(parts) if parts else "nothing to do") + "."]
        if self.n_calls or self.cost_usd:
            cached = f", {self.n_cached} from cache" if self.n_cached else ""
            lines.append(f"  {self.n_calls} call(s){cached}, ${self.cost_usd:.4f}.")
        lines.extend(f"  {message}" for message in self.messages)
        return "\n".join(lines)


class GapEngine:
    def __init__(self, project, client: AIClient, settings=None) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.client = client
        self.settings = settings or GapSettings()

    # -- reading the project ------------------------------------------------

    def counts(self) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM v_gap_summary")
        total = sum(row["n_gaps"] for row in rows)
        return {
            "n_gaps": total,
            "n_topics": self.db.scalar("SELECT COUNT(*) FROM topic") or 0,
            "n_possibly_addressed": sum(
                row["n_possibly_addressed"] for row in rows
            ),
            "n_with_study": sum(row["n_with_study"] for row in rows),
            "by_type": {row["gap_type"]: row["n_gaps"] for row in rows},
        }

    def _searched_to(self) -> str:
        searched_to = (self.project.info().get("searched_to") or "").strip()
        if not searched_to:
            raise GapError(
                "The project does not record the date the searches were run. "
                "Every gap this stage reports is an absence within the "
                "included corpus as searched to a date, and without that date "
                "the statement would claim an absence in the literature. Set "
                "the search date on the Project page."
            )
        return searched_to

    def documents(self) -> list[Document]:
        """Every active record, as the quick scan reads them."""
        return [
            Document(
                record_id=row["record_id"],
                title=row["title"] or "",
                abstract=row["abstract"] or "",
                keywords=[
                    k.strip()
                    for k in (row["keywords"] or "").split(";")
                    if k.strip()
                ],
                year=row["year"],
                attributes={"source_database": row["source_database"]},
            )
            for row in self.db.query("SELECT * FROM v_quick_scan_input")
        ]

    def corpus(self) -> Corpus:
        """The project as the detectors need it."""
        searched_to = self._searched_to()
        info = self.project.info()

        constructs = [
            row["name"]
            for row in self.db.query(
                "SELECT name FROM construct WHERE status = 'approved' "
                "AND is_residual = 0 ORDER BY position, id"
            )
        ]

        resolved = {
            row["link_key"]
            for row in self.db.query(
                "SELECT DISTINCT link_key FROM contradiction "
                "WHERE status = 'accepted'"
            )
        }
        links = []
        for row in self.db.query(
            "SELECT g.*, p.name AS predictor_name, o.name AS outcome_name "
            "FROM graded_link g "
            "JOIN construct p ON p.id = g.predictor_construct "
            "JOIN construct o ON o.id = g.outcome_construct"
        ):
            record_ids = tuple(
                r["record_id"]
                for r in self.db.query(
                    "SELECT DISTINCT m.record_id FROM link_vote v "
                    "JOIN dataset_member m ON m.dataset_id = v.dataset_id "
                    "WHERE v.predictor_construct = ? AND v.outcome_construct = ?",
                    (row["predictor_construct"], row["outcome_construct"]),
                )
            )
            links.append(
                Link(
                    link_id=row["id"],
                    predictor=row["predictor_name"],
                    outcome=row["outcome_name"],
                    grade=row["grade"],
                    direction=row["direction"],
                    n_datasets=row["n_datasets"],
                    record_ids=record_ids,
                    design_families=tuple(
                        f.strip()
                        for f in (row["design_families"] or "").split(",")
                        if f.strip()
                    ),
                    contested_resolved=row["link_key"] in resolved,
                )
            )

        reports = [
            Report(
                record_id=row["record_id"],
                title=row["title"] or "",
                country=row["country"] or "",
                setting=row["setting"] or "",
                population=row["population"] or "",
                design_family=row["design_family"] or "",
                sample_size=row["sample_size"],
                theories=tuple(_names(row["theories"])),
                framework_stages=tuple(_names(row["framework_stages"])),
            )
            for row in self.db.query(
                "SELECT e.*, r.title FROM report_extraction e "
                "JOIN record r ON r.id = e.record_id "
                "WHERE e.coder_role = 'ai'"
            )
        ]

        datasets = []
        for row in self.db.query("SELECT * FROM dataset"):
            members = tuple(
                m["record_id"]
                for m in self.db.query(
                    "SELECT record_id FROM dataset_member WHERE dataset_id = ?",
                    (row["id"],),
                )
            )
            first = next(
                (r for r in reports if r.record_id in members), None
            )
            datasets.append(
                Dataset(
                    dataset_id=row["id"],
                    record_ids=members,
                    country=(row["country"] if "country" in row.keys() else "")
                    or (first.country if first else ""),
                    setting=first.setting if first else "",
                    population=first.population if first else "",
                    design_family=first.design_family if first else "",
                )
            )

        propositions = tuple(
            (row["predictor_construct"], row["outcome_construct"])
            for row in self.db.query(
                "SELECT e.source_label AS predictor_construct, "
                "       e.target_label AS outcome_construct "
                "FROM framework_element e WHERE e.kind = 'evidenced_link'"
            )
        )
        untested = tuple(
            (row["source_label"], row["target_label"])
            for row in self.db.query(
                "SELECT source_label, target_label FROM framework_element "
                "WHERE kind IN ('untested_pathway','proposed_mechanism')"
            )
        )

        future = tuple(
            Statement(
                record_id=row["record_id"],
                statement=row["statement"],
                page=row["quote_page"],
                kind=row["kind"],
            )
            for row in self.db.query(
                "SELECT * FROM future_research ORDER BY record_id, id"
            )
        )

        return Corpus(
            searched_to=searched_to,
            constructs=tuple(constructs),
            links=tuple(links),
            datasets=tuple(datasets),
            reports=tuple(reports),
            propositions=propositions,
            untested_pathways=untested,
            theories=tuple(_lines(info.get("theories"))),
            stimulus_attributes=tuple(_lines(info.get("stimulus_attributes"))),
            future_research=future,
            framework_type=info.get("framework_type") or "PCC",
            framework_stage_order=self.project.framework_stages(),
        )

    @staticmethod
    def stage_transition_note(stages, framework: str) -> str:
        """What to tell the reviewer when a framework has no stages.

        An empty string when there is nothing to say. A gap register with
        no stage-transition gaps in it reads the same whether none were
        found or none were looked for, and those are opposite findings: the
        first says the evidence covers the framework, the second says the
        application never checked. Only one of them is a reason to relax.
        """
        if stages:
            return ""
        return (
            f"Stage-transition gaps were not computed: the framework "
            f"{framework!r} has no stages defined, so there were no "
            "transitions to look for. This is not a finding that the "
            "evidence covers the framework. Define the framework's stages "
            "on the Project page and run the analysis again."
        )

    # -- the quick scan ------------------------------------------------------

    def quick_scan(self, *, actor: str | None = None) -> StepOutcome:
        """Cluster the imported records, before the review proper.

        Needs no protocol, no screening and no inventory: it exists to help a
        reviewer refine their objectives, which happens before all three.
        """
        actor = actor or self.audit.actor
        documents = self.documents()
        outcome = StepOutcome(step="quick_scan")
        outcome.counts["n_records"] = len(documents)
        if not documents:
            outcome.messages.append(
                "There are no records to scan. Import some first."
            )
            return outcome

        vectors = vectorise(documents)
        topics = cluster(documents, vectors, n_topics=self.settings.n_topics)
        pairs = co_occurrence(documents, minimum=self.settings.min_co_occurrence)

        run_id = self.db.insert(
            "gap_run",
            {
                "mode": "quick",
                "status": "finished",
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "settings": json.dumps(self.settings.as_dict()),
                "n_records": len(documents),
                "n_topics": len(topics),
            },
        )
        with self.db.transaction():
            self.db.execute("DELETE FROM topic_member")
            self.db.execute("DELETE FROM topic")
            for topic in topics:
                years = [
                    d.year
                    for d in documents
                    if d.record_id in topic.record_ids and d.year
                ]
                topic_id = self.db.insert(
                    "topic",
                    {
                        "run_id": run_id,
                        "label": topic.label,
                        "terms": json.dumps(list(topic.terms)),
                        "n_records": len(topic.record_ids),
                        "year_first": min(years) if years else None,
                        "year_last": max(years) if years else None,
                        "created_at": utc_now(),
                    },
                )
                for record_id in topic.record_ids:
                    self.db.insert(
                        "topic_member",
                        {"topic_id": topic_id, "record_id": record_id},
                    )

        outcome.counts["n_topics"] = len(topics)
        outcome.counts["n_pairs"] = len(pairs)
        outcome.messages.append(
            "This is a scoping view for refining the objectives, not a "
            "finding. Nothing downstream cites it."
        )
        self.audit.log(
            "gaps", "quick_scan", entity="gap_run", entity_id=run_id,
            detail={"n_records": len(documents), "n_topics": len(topics)},
            actor=actor,
        )
        return outcome

    def scan_results(self) -> dict[str, Any]:
        """The last quick scan, for the screen."""
        documents = self.documents()
        topics = []
        for row in self.db.query("SELECT * FROM topic ORDER BY n_records DESC, id"):
            record_ids = [
                m["record_id"]
                for m in self.db.query(
                    "SELECT record_id FROM topic_member WHERE topic_id = ?",
                    (row["id"],),
                )
            ]
            topics.append(
                {
                    "id": row["id"],
                    "label": row["label"],
                    "terms": json.loads(row["terms"] or "[]"),
                    "n_records": row["n_records"],
                    "record_ids": record_ids,
                    "trend": trend(documents, record_ids),
                }
            )
        pairs = co_occurrence(documents, minimum=self.settings.min_co_occurrence)
        return {
            "topics": topics,
            "co_occurrence": [
                {"first": a, "second": b, "n_records": n}
                for (a, b), n in sorted(pairs.items(), key=lambda kv: -kv[1])
            ],
        }

    # -- the full analysis ---------------------------------------------------

    def find_gaps(self, *, actor: str | None = None) -> StepOutcome:
        """Build the register from the coded evidence."""
        actor = actor or self.audit.actor
        corpus = self.corpus()
        if not corpus.links:
            raise GapError(
                "There is no graded inventory yet. The full gap analysis is "
                "built from graded links, so the synthesis has to be run "
                "first. The quick scan runs without one."
            )

        candidates = detect_all(corpus)
        outcome = StepOutcome(step="gaps")
        outcome.counts["n_candidates"] = len(candidates)

        remembered = self._remembered_decisions()
        run_id = self.db.insert(
            "gap_run",
            {
                "mode": "full",
                "status": "finished",
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "corpus_searched_to": corpus.searched_to,
                "settings": json.dumps(self.settings.as_dict()),
                "n_records": len(corpus.reports),
            },
        )

        author_themes = {
            c.counts.get("theme", "")
            for c in candidates
            if c.gap_type == "author_stated"
        }
        n_stored = n_unsupported = n_kept = 0
        with self.db.transaction():
            self.db.execute("DELETE FROM gap_evidence")
            self.db.execute("DELETE FROM gap_register")
            for candidate in candidates:
                try:
                    statement = write_statement(
                        candidate, searched_to=corpus.searched_to
                    )
                except UnsupportedGap as exc:
                    # The spec's rule, enforced rather than reported: a gap
                    # with no evidence behind it does not go in the register.
                    n_unsupported += 1
                    outcome.messages.append(str(exc))
                    continue

                scores = self._score(candidate, corpus, author_themes)
                previous = remembered.get(
                    (candidate.gap_type, candidate.cell_key)
                )
                if previous and previous["score_overrides"] not in ("", "{}"):
                    scores = scores.with_overrides(
                        {
                            k: float(v)
                            for k, v in json.loads(
                                previous["score_overrides"]
                            ).items()
                        }
                    )
                if previous:
                    n_kept += 1

                gap_id = self.db.insert(
                    "gap_register",
                    {
                        "gap_type": candidate.gap_type,
                        "statement": statement,
                        "corpus_searched_to": corpus.searched_to,
                        "supporting_counts": json.dumps(
                            candidate.counts, sort_keys=True, default=str
                        ),
                        "cell_key": candidate.cell_key,
                        "score_centrality": scores.centrality,
                        "score_size": scores.size,
                        "score_relevance": scores.relevance,
                        "score_feasibility": scores.feasibility,
                        "priority": scores.priority,
                        "score_overrides": json.dumps(scores.overrides),
                        "status": previous["status"] if previous else "proposed",
                        "status_reason": (
                            previous["status_reason"] if previous else ""
                        ),
                        "override_reason": (
                            previous["override_reason"] if previous else ""
                        ),
                        "overridden_by": (
                            previous["overridden_by"] if previous else ""
                        ),
                        "suggested_question": (
                            previous["suggested_question"] if previous else ""
                        ),
                        "suggested_hypothesis": (
                            previous["suggested_hypothesis"] if previous else ""
                        ),
                        "suggested_design": (
                            previous["suggested_design"] if previous else ""
                        ),
                        "suggested_measures": (
                            previous["suggested_measures"] if previous else ""
                        ),
                        "suggested_population": (
                            previous["suggested_population"] if previous else ""
                        ),
                        "suggested_setting": (
                            previous["suggested_setting"] if previous else ""
                        ),
                        "freshness_status": (
                            previous["freshness_status"] if previous
                            else "not_checked"
                        ),
                        "freshness_detail": (
                            previous["freshness_detail"] if previous else "{}"
                        ),
                        "run_id": run_id,
                        "created_at": utc_now(),
                        "created_by": "ai" if candidate.gap_type
                        == "author_stated" else "rules",
                    },
                )
                n_stored += 1
                self._store_evidence(gap_id, candidate)

        self.db.execute(
            "UPDATE gap_run SET n_gaps = ? WHERE id = ?", (n_stored, run_id)
        )
        outcome.counts.update(
            n_gaps=n_stored,
            n_unsupported=n_unsupported,
            n_decisions_kept=n_kept,
        )
        if n_unsupported:
            outcome.messages.append(
                f"{n_unsupported} candidate(s) were not recorded because the "
                "counts or records behind them were missing. A gap statement "
                "is never produced without its evidence."
            )
        note = self.stage_transition_note(
            corpus.framework_stage_order, corpus.framework_type
        )
        if note:
            outcome.messages.append(note)
        self.audit.log(
            "gaps", "gaps_found", entity="gap_run", entity_id=run_id,
            detail={
                "n_candidates": len(candidates),
                "n_gaps": n_stored,
                "n_unsupported": n_unsupported,
                "searched_to": corpus.searched_to,
            },
            actor=actor,
        )
        return outcome

    def _remembered_decisions(self) -> dict[tuple[str, str], dict[str, Any]]:
        """What the reviewer has already decided, keyed by gap type and cell."""
        return {
            (row["gap_type"], row["cell_key"]): dict(row)
            for row in self.db.query(
                "SELECT * FROM gap_register WHERE status <> 'proposed' "
                "OR score_overrides NOT IN ('', '{}') "
                "OR suggested_question <> '' "
                "OR freshness_status <> 'not_checked'"
            )
        }

    def _score(
        self, candidate: GapCandidate, corpus: Corpus, author_themes: set[str]
    ) -> Scores:
        depends = sum(
            1
            for predictor, outcome in corpus.propositions
            if candidate.counts.get("predictor") in (predictor, outcome)
            or candidate.counts.get("outcome") in (predictor, outcome)
        )
        words = {
            w for theme in author_themes for w in theme.replace(",", " ").split()
        }
        mentioned = any(
            str(value).casefold() in words
            for key, value in candidate.counts.items()
            if key in ("predictor", "outcome", "construct", "design", "attribute")
        )
        return score(
            candidate,
            n_propositions=depends,
            n_propositions_total=len(corpus.propositions),
            in_author_gaps=mentioned,
            weights=self.settings.weights,
        )

    def _store_evidence(self, gap_id: int, candidate: GapCandidate) -> None:
        known = {
            row["id"] for row in self.db.query("SELECT id FROM record")
        }
        for record_id in candidate.record_ids:
            if record_id not in known:
                continue
            self.db.insert(
                "gap_evidence",
                {
                    "gap_id": gap_id,
                    "kind": "record",
                    "record_id": record_id,
                    "role": "counts_toward",
                },
            )
        for link_id in candidate.link_ids:
            if not self.db.scalar(
                "SELECT COUNT(*) FROM graded_link WHERE id = ?", (link_id,)
            ):
                continue
            self.db.insert(
                "gap_evidence",
                {
                    "gap_id": gap_id,
                    "kind": "link",
                    "label": f"L{link_id}",
                    "role": "counts_toward",
                },
            )

    # -- reading the register -------------------------------------------------

    def gaps(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM v_gap_register ORDER BY gap_type, id"
            )
        ]

    def ranked(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM v_gap_register "
                "ORDER BY priority DESC, gap_type, id"
            )
        ]

    def gap_evidence(self, gap_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT e.*, r.title FROM gap_evidence e "
                "LEFT JOIN record r ON r.id = e.record_id "
                "WHERE e.gap_id = ? ORDER BY e.id",
                (gap_id,),
            )
        ]

    def decide_gap(
        self,
        gap_id: int,
        *,
        status: str,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        actor = actor or self.audit.actor
        self.db.execute(
            "UPDATE gap_register SET status = ?, status_reason = ? WHERE id = ?",
            (status, reason, gap_id),
        )
        self.audit.log(
            "gaps", f"gap_{status}", entity="gap_register", entity_id=gap_id,
            detail={"reason": reason}, actor=actor,
        )

    def override_scores(
        self,
        gap_id: int,
        overrides: dict[str, float],
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        """Put the reviewer's scores in place of the computed ones."""
        actor = actor or self.audit.actor
        row = self.db.query_one(
            "SELECT * FROM gap_register WHERE id = ?", (gap_id,)
        )
        if row is None:
            raise GapError(f"No gap {gap_id}.")
        scores = Scores(
            centrality=row["score_centrality"] or 0.0,
            size=row["score_size"] or 0.0,
            relevance=row["score_relevance"] or 0.0,
            feasibility=row["score_feasibility"] or 0.0,
            weights=self.settings.weights,
        ).with_overrides({k: float(v) for k, v in overrides.items()})
        self.db.execute(
            "UPDATE gap_register SET score_centrality = ?, score_size = ?, "
            "score_relevance = ?, score_feasibility = ?, priority = ?, "
            "score_overrides = ?, override_reason = ?, overridden_by = ?, "
            "overridden_at = ? WHERE id = ?",
            (
                scores.centrality, scores.size, scores.relevance,
                scores.feasibility, scores.priority,
                json.dumps(scores.overrides), reason, actor, utc_now(), gap_id,
            ),
        )
        self.audit.log_override(
            "gaps",
            "gap_register",
            gap_id,
            "priority_scores",
            {
                "centrality": row["score_centrality"],
                "size": row["score_size"],
                "relevance": row["score_relevance"],
                "feasibility": row["score_feasibility"],
                "priority": row["priority"],
            },
            {**scores.as_dict()},
            reason=reason,
            actor=actor,
        )

    def set_weights(
        self, weights: dict[str, float], *, actor: str | None = None
    ) -> None:
        """Change the weights and rescore every gap against them."""
        actor = actor or self.audit.actor
        self.settings.weights = Weights(**weights)
        for row in self.db.query("SELECT * FROM gap_register"):
            scores = Scores(
                centrality=row["score_centrality"] or 0.0,
                size=row["score_size"] or 0.0,
                relevance=row["score_relevance"] or 0.0,
                feasibility=row["score_feasibility"] or 0.0,
                weights=self.settings.weights,
            )
            self.db.execute(
                "UPDATE gap_register SET priority = ? WHERE id = ?",
                (scores.priority, row["id"]),
            )
        self.audit.log(
            "gaps", "weights_changed", entity="gap_register",
            detail={"weights": self.settings.weights.as_dict()}, actor=actor,
        )

    # -- freshness ------------------------------------------------------------

    def check_freshness(
        self,
        *,
        actor: str | None = None,
        top: int | None = None,
        checker: FreshnessChecker | None = None,
    ) -> StepOutcome:
        """Ask whether anything since the search date may already address each gap."""
        actor = actor or self.audit.actor
        searched_to = self._searched_to()
        checker = checker or FreshnessChecker(
            self.project.info().get("contact_email") or ""
        )
        outcome = StepOutcome(step="freshness")
        gaps = self.ranked()[: top] if top else self.ranked()

        n_addressed = n_failed = 0
        for gap in gaps:
            counts = json.loads(gap["supporting_counts"] or "{}")
            terms = [
                str(counts[key])
                for key in ("predictor", "outcome", "construct", "mechanism",
                            "attribute")
                if counts.get(key)
            ]
            if not terms:
                continue
            result = checker.check(terms, searched_to=searched_to)
            n_addressed += result.status == "possibly_addressed"
            n_failed += result.status == "check_failed"
            self.db.execute(
                "UPDATE gap_register SET freshness_status = ?, "
                "freshness_detail = ?, freshness_checked_at = ? WHERE id = ?",
                (
                    result.status,
                    json.dumps(result.as_dict()),
                    utc_now(),
                    gap["id"],
                ),
            )
        outcome.counts.update(
            n_checked=len(gaps),
            n_possibly_addressed=n_addressed,
            n_failed=n_failed,
        )
        if n_addressed:
            outcome.messages.append(
                f"{n_addressed} gap(s) may already be being addressed by work "
                "published after the search date. The gap still stands -- this "
                "corpus has the hole -- but read the papers before reporting it."
            )
        self.audit.log(
            "gaps", "freshness_checked", entity="gap_register",
            detail={"n_checked": len(gaps), "n_possibly_addressed": n_addressed},
            actor=actor,
        )
        return outcome

    # -- from gap to study ------------------------------------------------------

    def draft_studies(
        self, *, actor: str | None = None, top: int = 5
    ) -> StepOutcome:
        """Draft a question, hypothesis, design and population per top gap."""
        actor = actor or self.audit.actor
        ranked = self.ranked()
        if not ranked:
            raise GapError(
                "There are no gaps to draft studies for. Run the gap analysis "
                "first."
            )
        outcome = StepOutcome(step="studies")
        propositions = [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM v_propositions WHERE status <> 'rejected'"
            )
        ]
        baseline = (self.client.n_calls, self.client.n_cached, self.client.spent_usd)

        from .prompts import study_system, study_user
        from .schemas import StudyDraft

        n_drafted = 0
        for gap in ranked[:top]:
            try:
                result = self.client.ask(
                    "gaps:study",
                    system=study_system(propositions),
                    user=study_user(gap),
                    schema=StudyDraft,
                    entity="gap_register",
                    entity_id=gap["id"],
                    max_tokens=self.settings.max_tokens,
                )
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break
            draft = result.value
            self.db.execute(
                "UPDATE gap_register SET suggested_question = ?, "
                "suggested_hypothesis = ?, suggested_design = ?, "
                "suggested_measures = ?, suggested_population = ?, "
                "suggested_setting = ?, ai_call_id = ? WHERE id = ?",
                (
                    draft.question, draft.hypothesis, draft.design,
                    "; ".join(draft.measures), draft.population, draft.setting,
                    result.ai_call_id, gap["id"],
                ),
            )
            n_drafted += 1

        outcome.counts["n_drafted"] = n_drafted
        calls, cached, spent = baseline
        outcome.n_calls = self.client.n_calls - calls
        outcome.n_cached = self.client.n_cached - cached
        outcome.cost_usd = round(self.client.spent_usd - spent, 6)
        self.audit.log(
            "gaps", "studies_drafted", entity="gap_register",
            detail={"n_drafted": n_drafted}, actor=actor,
        )
        return outcome

    # -- the map ----------------------------------------------------------------

    def gap_map(self, *, dimension: str = "country") -> Grid:
        corpus = self.corpus()
        return build_grid(
            corpus.constructs, corpus.datasets, corpus.links,
            dimension=dimension, searched_to=corpus.searched_to,
        )

    def write_gap_map(
        self, *, actor: str | None = None, dimension: str = "country"
    ) -> list[Path]:
        actor = actor or self.audit.actor
        grid = self.gap_map(dimension=dimension)
        directory = self.project.exports_dir / "figures"
        directory.mkdir(parents=True, exist_ok=True)
        stem = f"evidence-gap-map-{dimension}"
        title = f"Evidence gap map: constructs by {dimension.replace('_', ' ')}"

        written = [write_svg(grid, directory / f"{stem}.svg", title=title)]
        png = write_png(grid, directory / f"{stem}.png", title=title)
        if png is not None:
            written.append(png)
        pdf = write_pdf(grid, directory / f"{stem}.pdf", title=title)
        if pdf is not None:
            written.append(pdf)
        written.append(
            write_excel(grid, directory / f"{stem}.xlsx", title=title)
        )
        self.audit.log(
            "gaps", "gap_map_written", entity="figure",
            detail={"dimension": dimension, "files": [p.name for p in written]},
            actor=actor,
        )
        return written

    def estimate(self, step: str) -> CostEstimate:
        if step == "studies":
            return self.client.estimate(5, self.settings.max_tokens)
        return self.client.estimate(0, 0)


def _names(payload: str | None) -> list[str]:
    try:
        entries = json.loads(payload or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else str(entry)
        if name and str(name).strip():
            out.append(str(name).strip())
    return out


def _lines(text: str | None) -> list[str]:
    if not text:
        return []
    import re

    return [part.strip() for part in re.split(r"[;\n]", text) if part.strip()]
