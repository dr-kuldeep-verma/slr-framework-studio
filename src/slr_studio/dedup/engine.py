"""Running deduplication against a project database.

A run is reproducible: it rebuilds the whole duplicate picture from the
records and the stored pair decisions, so changing a threshold and re-running
cannot leave stale merges behind. Human decisions survive every re-run;
automatic ones are recomputed from scratch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.db import utc_now
from ..core.project import Project
from .rules import (
    CandidateRecord,
    DedupSettings,
    PairVerdict,
    candidate_pairs,
    choose_primary,
    cluster_pairs,
    compare,
    completeness_score,
)

__all__ = ["DedupEngine", "DedupRunResult"]

_HUMAN_STATUSES = ("human_match", "human_distinct")
_MATCH_STATUSES = ("auto_match", "human_match")

_RECORD_COLUMNS = (
    "id, title, title_norm, title_key, year, doi, first_author_norm, journal, "
    "source_database, has_abstract, abstract, authors, keywords, pages, language"
)


@dataclass
class DedupRunResult:
    run_id: int
    n_records: int = 0
    n_pairs: int = 0
    n_auto_match: int = 0
    n_uncertain: int = 0
    n_auto_distinct: int = 0
    n_human_decided: int = 0
    n_clusters: int = 0
    n_merged: int = 0
    n_remaining: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "n_records": self.n_records,
            "n_pairs": self.n_pairs,
            "n_auto_match": self.n_auto_match,
            "n_uncertain": self.n_uncertain,
            "n_auto_distinct": self.n_auto_distinct,
            "n_human_decided": self.n_human_decided,
            "n_clusters": self.n_clusters,
            "n_merged": self.n_merged,
            "n_remaining": self.n_remaining,
            "by_rule": dict(self.by_rule),
            "warnings": list(self.warnings),
        }


class DedupEngine:
    def __init__(self, project: Project, settings: DedupSettings | None = None) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.settings = settings or DedupSettings()

    # -- running -----------------------------------------------------------

    def run(self, *, apply_merges: bool = True, actor: str | None = None) -> DedupRunResult:
        started = utc_now()
        records = self._load_records()
        with self.db.transaction():
            run_id = self.db.insert(
                "dedup_run",
                {
                    "started_at": started,
                    "fuzzy_threshold": self.settings.fuzzy_threshold,
                    "auto_threshold": self.settings.auto_threshold,
                    "n_records": len(records),
                    "settings": json.dumps(self.settings.as_dict()),
                },
            )
            result = DedupRunResult(run_id=run_id, n_records=len(records))

            self._reset_merges()
            verdicts = self._evaluate(records)
            statuses = self._store_pairs(run_id, verdicts)

            for status in statuses.values():
                if status == "auto_match":
                    result.n_auto_match += 1
                elif status == "uncertain":
                    result.n_uncertain += 1
                elif status == "auto_distinct":
                    result.n_auto_distinct += 1
                else:
                    result.n_human_decided += 1
            result.n_pairs = len(statuses)
            for verdict in verdicts:
                result.by_rule[verdict.rule] = result.by_rule.get(verdict.rule, 0) + 1

            if apply_merges:
                self._apply(run_id, records, result, actor=actor)

            result.n_remaining = int(
                self.db.scalar("SELECT COUNT(*) FROM record WHERE active = 1") or 0
            )
            self.db.execute(
                "UPDATE dedup_run SET finished_at = ?, n_pairs = ?, n_auto = ?, "
                "n_uncertain = ?, n_merged = ? WHERE id = ?",
                (utc_now(), result.n_pairs, result.n_auto_match,
                 result.n_uncertain, result.n_merged, run_id),
            )
            self.audit.log(
                "dedup",
                "dedup_run",
                entity="dedup_run",
                entity_id=run_id,
                detail=result.as_dict() | {"settings": self.settings.as_dict()},
                actor=actor,
            )
        return result

    def _load_records(self) -> list[CandidateRecord]:
        rows = self.db.query(f"SELECT {_RECORD_COLUMNS} FROM record ORDER BY id")
        return [CandidateRecord.from_row(row) for row in rows]

    def _evaluate(self, records: list[CandidateRecord]) -> list[PairVerdict]:
        verdicts: list[PairVerdict] = []
        for left, right in candidate_pairs(records, self.settings):
            verdict = compare(left, right, self.settings)
            if verdict is not None:
                verdicts.append(verdict)
        return verdicts

    # -- pair storage ------------------------------------------------------

    def _store_pairs(self, run_id: int, verdicts: list[PairVerdict]) -> dict[tuple[int, int], str]:
        """Write this run's pairs, leaving human decisions untouched."""
        existing = {
            (row["record_a_id"], row["record_b_id"]): row
            for row in self.db.query("SELECT * FROM dedup_pair")
        }
        statuses: dict[tuple[int, int], str] = {}
        proposed: set[tuple[int, int]] = set()

        for verdict in verdicts:
            key = (verdict.record_a_id, verdict.record_b_id)
            proposed.add(key)
            prior = existing.get(key)
            evidence = json.dumps(verdict.evidence, ensure_ascii=False)
            if prior is not None and prior["status"] in _HUMAN_STATUSES:
                # Keep the human verdict; refresh the evidence it was given.
                self.db.execute(
                    "UPDATE dedup_pair SET run_id = ?, rule = ?, score = ?, "
                    "evidence = ? WHERE id = ?",
                    (run_id, verdict.rule, verdict.score, evidence, prior["id"]),
                )
                statuses[key] = prior["status"]
                continue
            if prior is not None:
                self.db.execute(
                    "UPDATE dedup_pair SET run_id = ?, rule = ?, score = ?, "
                    "status = ?, evidence = ? WHERE id = ?",
                    (run_id, verdict.rule, verdict.score, verdict.status,
                     evidence, prior["id"]),
                )
            else:
                self.db.insert(
                    "dedup_pair",
                    {
                        "run_id": run_id,
                        "record_a_id": verdict.record_a_id,
                        "record_b_id": verdict.record_b_id,
                        "rule": verdict.rule,
                        "score": verdict.score,
                        "status": verdict.status,
                        "evidence": evidence,
                    },
                )
            statuses[key] = verdict.status

        # Pairs the current settings no longer propose: drop the automatic
        # ones so the review queue reflects the thresholds in force, and keep
        # every human decision as part of the record.
        for key, row in existing.items():
            if key in proposed:
                continue
            if row["status"] in _HUMAN_STATUSES:
                statuses[key] = row["status"]
            else:
                self.db.execute("DELETE FROM dedup_pair WHERE id = ?", (row["id"],))
        return statuses

    # -- merging -----------------------------------------------------------

    def _reset_merges(self) -> None:
        """Undo previous merges so a run always computes from first principles."""
        self.db.execute(
            "UPDATE record SET active = 1, duplicate_of = NULL, cluster_id = NULL "
            "WHERE active = 0 OR duplicate_of IS NOT NULL OR cluster_id IS NOT NULL"
        )
        self.db.execute("DELETE FROM record_source WHERE record_id != origin_record_id")
        self.db.execute("DELETE FROM record_keyword WHERE record_id != from_record_id")
        self.db.execute(
            "UPDATE merge_log SET reverted_at = ? WHERE reverted_at IS NULL", (utc_now(),)
        )

    def _apply(
        self,
        run_id: int,
        records: list[CandidateRecord],
        result: DedupRunResult,
        *,
        actor: str | None,
    ) -> None:
        by_id = {record.id: record for record in records}
        rows = self.db.query(
            "SELECT record_a_id, record_b_id, status, rule, score FROM dedup_pair"
        )
        edges = [
            (r["record_a_id"], r["record_b_id"])
            for r in rows
            if r["status"] in _MATCH_STATUSES
        ]
        edge_meta = {
            (r["record_a_id"], r["record_b_id"]): (r["rule"], r["score"], r["status"])
            for r in rows
        }
        distinct_pairs = {
            (r["record_a_id"], r["record_b_id"])
            for r in rows
            if r["status"] == "human_distinct"
        }

        clusters = cluster_pairs(by_id.keys(), edges)
        now = utc_now()

        for root, members in sorted(clusters.items()):
            if len(members) < 2:
                self.db.execute(
                    "UPDATE record SET cluster_id = ? WHERE id = ?", (root, members[0])
                )
                continue
            result.n_clusters += 1

            # A chain of matches can pull together two records a human called
            # distinct. That is a real contradiction, not a bug: it is surfaced
            # rather than silently resolved either way.
            for a, b in distinct_pairs:
                if a in members and b in members:
                    result.warnings.append(
                        f"records {a} and {b} were marked distinct by a reviewer but "
                        f"are linked through other matches in cluster {root}"
                    )

            candidates = [by_id[m] for m in members]
            primary = choose_primary(candidates, self.settings)
            self.db.execute(
                "UPDATE record SET cluster_id = ?, duplicate_of = NULL, active = 1 "
                "WHERE id = ?",
                (root, primary.id),
            )
            for member in candidates:
                if member.id == primary.id:
                    continue
                key = (min(primary.id, member.id), max(primary.id, member.id))
                rule, score, status = edge_meta.get(key, ("transitive", 0.0, "auto_match"))
                self.db.execute(
                    "UPDATE record SET active = 0, duplicate_of = ?, cluster_id = ? "
                    "WHERE id = ?",
                    (primary.id, root, member.id),
                )
                # Carry the merged record's database provenance onto the keeper
                # so per-database counts stay truthful after deduplication.
                self.db.execute(
                    "INSERT OR IGNORE INTO record_source "
                    "(record_id, source_database, import_file_id, origin_record_id, accession) "
                    "SELECT ?, source_database, import_file_id, origin_record_id, accession "
                    "FROM record_source WHERE record_id = ?",
                    (primary.id, member.id),
                )
                # And its keywords. Databases index the same paper with
                # different vocabularies, so dropping the merged record's
                # terms would make the corpus look narrower than it is when
                # the Gap Finder clusters topics.
                self.db.execute(
                    "INSERT OR IGNORE INTO record_keyword "
                    "(record_id, keyword, keyword_norm, from_record_id, source_database) "
                    "SELECT ?, keyword, keyword_norm, from_record_id, source_database "
                    "FROM record_keyword WHERE record_id = ?",
                    (primary.id, member.id),
                )
                self.db.insert(
                    "merge_log",
                    {
                        "run_id": run_id,
                        "kept_record_id": primary.id,
                        "merged_record_id": member.id,
                        "rule": rule,
                        "score": score,
                        "decided_by": (
                            actor or self.audit.actor if status == "human_match" else "system"
                        ),
                        "merged_at": now,
                        "detail": json.dumps(
                            {
                                "cluster": root,
                                "cluster_size": len(members),
                                "status": status,
                                "kept_source": primary.source_database,
                                "merged_source": member.source_database,
                                "kept_score": round(
                                    completeness_score(primary, self.settings), 3
                                ),
                                "merged_score": round(
                                    completeness_score(member, self.settings), 3
                                ),
                            }
                        ),
                    },
                )
                result.n_merged += 1

    # -- the human review queue -------------------------------------------

    def pending_pairs(self, limit: int = 500) -> list[dict[str, Any]]:
        """Uncertain pairs awaiting a reviewer, highest score first."""
        rows = self.db.query(
            "SELECT * FROM dedup_pair WHERE status = 'uncertain' "
            "ORDER BY score DESC, id LIMIT ?",
            (limit,),
        )
        return [self._pair_payload(row) for row in rows]

    def pair(self, pair_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM dedup_pair WHERE id = ?", (pair_id,))
        return self._pair_payload(row) if row else None

    def _pair_payload(self, row: Any) -> dict[str, Any]:
        def record(record_id: int) -> dict[str, Any]:
            data = self.db.query_one(
                "SELECT id, title, authors, year, journal, volume, issue, pages, doi, "
                "abstract, source_database, doc_type, language, accession, "
                "has_abstract, import_file_id FROM record WHERE id = ?",
                (record_id,),
            )
            if data is None:
                return {}
            payload = dict(data)
            payload["authors"] = json.loads(payload.get("authors") or "[]")
            return payload

        return {
            "id": row["id"],
            "rule": row["rule"],
            "score": row["score"],
            "status": row["status"],
            "evidence": json.loads(row["evidence"] or "{}"),
            "a": record(row["record_a_id"]),
            "b": record(row["record_b_id"]),
        }

    def decide_pair(
        self,
        pair_id: int,
        is_duplicate: bool,
        *,
        actor: str | None = None,
        reason: str = "",
        reapply: bool = True,
    ) -> None:
        """Record a reviewer's verdict on an uncertain pair.

        The human decision overrides the AI's (integrity rule 3), is written to
        the override table and the audit log, and -- unless the caller is
        batching -- the merge picture is rebuilt immediately so the PRISMA
        counts on screen stay true.
        """
        row = self.db.query_one("SELECT * FROM dedup_pair WHERE id = ?", (pair_id,))
        if row is None:
            raise ValueError(f"No duplicate pair with id {pair_id}")
        status = "human_match" if is_duplicate else "human_distinct"
        actor = actor or self.audit.actor
        with self.db.transaction():
            self.db.execute(
                "UPDATE dedup_pair SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE id = ?",
                (status, utc_now(), actor, pair_id),
            )
            self.audit.log_override(
                "dedup",
                "dedup_pair",
                pair_id,
                "status",
                row["status"],
                status,
                reason=reason,
                actor=actor,
            )
        if reapply:
            self.run(actor=actor)

    # -- reporting ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        run = self.db.query_one("SELECT * FROM dedup_run ORDER BY id DESC LIMIT 1")
        counts = self.project.counts()
        return {
            "last_run": dict(run) if run else None,
            "n_uncertain": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM dedup_pair WHERE status = 'uncertain'"
                ) or 0
            ),
            "n_identified": counts.get("n_identified", 0),
            "n_duplicates_removed": counts.get("n_duplicates_removed", 0),
            "n_screened": counts.get("n_screened", 0),
        }

    def merged_groups(self, limit: int = 500) -> list[dict[str, Any]]:
        """Every applied merge, for the audit panel and the Excel export."""
        rows = self.db.query(
            "SELECT m.*, k.title AS kept_title, d.title AS merged_title, "
            "       k.source_database AS kept_db, d.source_database AS merged_db "
            "FROM merge_log m "
            "JOIN record k ON k.id = m.kept_record_id "
            "JOIN record d ON d.id = m.merged_record_id "
            "WHERE m.reverted_at IS NULL ORDER BY m.kept_record_id, m.id LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]
