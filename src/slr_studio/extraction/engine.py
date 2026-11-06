"""Extracting data from included reports, and checking it.

Three things happen to every relationship before it reaches the synthesis:

1. **It is extracted**, with the authors' own labels and a quotation.
2. **The quotation is checked** against the page it cites, exactly as at
   full-text eligibility.
3. **The direction is re-derived** by a separate call that sees only the
   quotation and the two labels — not the direction already recorded. When
   the two disagree, the relationship is flagged and queued; the extracted
   direction is kept, because overwriting it with the checker's would just
   move the unexamined judgement from one call to another.

Anything that fails a rule is recorded and surfaced, never dropped. A
relationship quietly discarded is a relationship nobody can argue about.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..ai.client import AIClient, BudgetExceeded, CostEstimate
from ..core.db import utc_now
from ..fulltext.pdf import PdfPage, find_quote
from ..fulltext.prompts import render_report
from .prompts import (
    PROMPT_VERSION,
    direction_system,
    direction_user,
    extraction_system,
    extraction_user,
)
from .schemas import (
    DESIGN_FAMILIES,
    DirectionCheck,
    ExtractionAnswer,
    Relationship,
    ReportExtraction,
)

__all__ = [
    "ExtractionEngine",
    "ExtractionSettings",
    "ExtractionOutcome",
    "ExtractionError",
]

ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]

_RECORD_COLUMNS = "id, title, abstract, year, journal, doc_type, language, authors"


class ExtractionError(RuntimeError):
    pass


@dataclass
class ExtractionSettings:
    max_tokens: int = 2400
    max_report_chars: int = 70_000
    head_pages: int = 8
    budget_usd: float | None = None
    #: Re-derive every relationship's direction from its quotation.
    verify_directions: bool = True
    #: Share of reports a second person is asked to code independently.
    second_coder_fraction: float = 0.20
    second_coder_minimum: int = 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_report_chars": self.max_report_chars,
            "head_pages": self.head_pages,
            "budget_usd": self.budget_usd,
            "verify_directions": self.verify_directions,
            "second_coder_fraction": self.second_coder_fraction,
            "second_coder_minimum": self.second_coder_minimum,
            "prompt_version": PROMPT_VERSION,
        }


@dataclass
class ExtractionOutcome:
    run_id: int
    status: str = "finished"
    n_records: int = 0
    n_done: int = 0
    n_relationships: int = 0
    n_quotes_checked: int = 0
    n_quotes_on_cited_page: int = 0
    n_quotes_elsewhere: int = 0
    n_quotes_not_found: int = 0
    n_directions_checked: int = 0
    n_direction_mismatches: int = 0
    n_designs_downgraded: int = 0
    n_instrument_mismatches: int = 0
    n_queued: int = 0
    n_calls: int = 0
    n_cached: int = 0
    cost_usd: float = 0.0
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "n_records": self.n_records,
            "n_done": self.n_done,
            "n_relationships": self.n_relationships,
            "n_quotes_checked": self.n_quotes_checked,
            "n_quotes_on_cited_page": self.n_quotes_on_cited_page,
            "n_quotes_elsewhere": self.n_quotes_elsewhere,
            "n_quotes_not_found": self.n_quotes_not_found,
            "n_directions_checked": self.n_directions_checked,
            "n_direction_mismatches": self.n_direction_mismatches,
            "n_designs_downgraded": self.n_designs_downgraded,
            "n_instrument_mismatches": self.n_instrument_mismatches,
            "n_queued": self.n_queued,
            "n_calls": self.n_calls,
            "n_cached": self.n_cached,
            "cost_usd": round(self.cost_usd, 4),
            "messages": list(self.messages),
        }


class ExtractionEngine:
    def __init__(
        self,
        project,
        client: AIClient,
        settings: ExtractionSettings | None = None,
    ) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.client = client
        self.settings = settings or ExtractionSettings()
        if self.settings.budget_usd is not None:
            self.client.set_budget(self.settings.budget_usd)

    # -- what there is to do ----------------------------------------------

    def included_records(self) -> list[dict[str, Any]]:
        """Reports the full-text stage included, in record order."""
        return [
            dict(r)
            for r in self.db.query(
                f"SELECT {_RECORD_COLUMNS} FROM record r "
                "JOIN fulltext_status f ON f.record_id = r.id AND f.decision = 'include' "
                "JOIN report rep ON rep.record_id = r.id AND rep.status = 'retrieved' "
                "WHERE r.active = 1 ORDER BY r.id"
            )
        ]

    def pending_records(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.included_records()
            if not self.db.query_one(
                "SELECT 1 FROM report_extraction WHERE record_id = ? "
                "AND coder_role = 'ai'",
                (record["id"],),
            )
        ]

    def _pages(self, record_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT page_no, text, ocr FROM report_page WHERE record_id = ? "
                "ORDER BY page_no",
                (record_id,),
            )
        ]

    def counts(self) -> dict[str, int]:
        row = self.db.query_one("SELECT * FROM v_extraction_progress")
        return dict(row) if row else {}

    # -- the protocol ------------------------------------------------------

    def _protocol(self) -> tuple[str, int | None]:
        current = self.project.protocol.current()
        if current is None:
            raise ExtractionError(
                "The protocol has not been locked. Everything extracted here "
                "is counted in the synthesis, so it has to be traceable to a "
                "fixed protocol."
            )
        return current.content, current.id

    def _system(self) -> str:
        protocol_text, _ = self._protocol()
        info = self.project.info()
        return extraction_system(
            protocol_text,
            DESIGN_FAMILIES,
            framework_stages=info.get("research_questions") or [],
        )

    # -- estimating --------------------------------------------------------

    def estimate(self) -> CostEstimate:
        records = self.pending_records()
        system = self._system()
        prompts = []
        for record in records:
            text, _ = render_report(
                self._pages(record["id"]),
                max_chars=self.settings.max_report_chars,
                head_pages=self.settings.head_pages,
            )
            prompts.append((system, extraction_user(record, text)))
        estimate = self.client.estimate(
            prompts, schema=ExtractionAnswer, expected_completion_tokens=900
        )
        if self.settings.verify_directions:
            estimate.notes.append(
                "Each relationship is then checked by a second, much smaller "
                "call that re-derives its direction from the quotation."
            )
        return estimate

    # -- the run -----------------------------------------------------------

    def run(
        self,
        *,
        actor: str | None = None,
        on_progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
        limit: int | None = None,
    ) -> ExtractionOutcome:
        actor = actor or self.audit.actor
        protocol_text, protocol_id = self._protocol()
        system = self._system()
        records = self.pending_records()
        if limit is not None:
            records = records[:limit]

        baseline = (self.client.n_calls, self.client.n_cached, self.client.spent_usd)
        run_id = self.db.insert(
            "extraction_run",
            {
                "kind": "extract",
                "status": "running",
                "started_at": utc_now(),
                "provider": self.client.provider.name,
                "model": self.client.provider.model,
                "temperature": self.client.temperature,
                "prompt_version": PROMPT_VERSION,
                "protocol_version": protocol_id,
                "settings": json.dumps(self.settings.as_dict()),
                "n_records": len(records),
            },
        )
        outcome = ExtractionOutcome(run_id=run_id, n_records=len(records))

        for index, record in enumerate(records, start=1):
            if should_stop is not None and should_stop():
                outcome.status = "paused"
                outcome.messages.append(
                    f"Paused after {outcome.n_done} of {len(records)} reports. "
                    "Nothing already extracted will be asked again."
                )
                break
            try:
                self._extract_one(run_id, record, system, outcome, actor)
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break
            outcome.n_done = index
            if on_progress:
                on_progress(index, len(records), f"Extracted {index} of {len(records)}")

        outcome.n_calls = self.client.n_calls - baseline[0]
        outcome.n_cached = self.client.n_cached - baseline[1]
        outcome.cost_usd = round(self.client.spent_usd - baseline[2], 6)

        if outcome.n_direction_mismatches:
            outcome.messages.append(
                f"{outcome.n_direction_mismatches} relationship(s) have a "
                "direction the check did not agree with from the quotation. "
                "They are queued for you; the extracted direction stands "
                "until you settle it."
            )
        if outcome.n_quotes_not_found:
            outcome.messages.append(
                f"{outcome.n_quotes_not_found} quotation(s) are not in their "
                "report at all."
            )
        self.db.execute(
            "UPDATE extraction_run SET status = ?, finished_at = ?, n_done = ?, "
            "n_relationships = ?, n_calls = ?, n_cached = ?, cost_usd = ? WHERE id = ?",
            (
                outcome.status,
                utc_now() if outcome.status != "paused" else None,
                outcome.n_done, outcome.n_relationships, outcome.n_calls,
                outcome.n_cached, outcome.cost_usd, run_id,
            ),
        )
        self.audit.log(
            "extract", "extraction_run", entity="extraction_run", entity_id=run_id,
            detail=outcome.as_dict(), actor=actor,
        )
        return outcome

    def _extract_one(
        self,
        run_id: int,
        record: dict[str, Any],
        system: str,
        outcome: ExtractionOutcome,
        actor: str,
    ) -> None:
        record_id = record["id"]
        pages = self._pages(record_id)
        report_text, _ = render_report(
            pages,
            max_chars=self.settings.max_report_chars,
            head_pages=self.settings.head_pages,
        )
        result = self.client.ask(
            "extract:report",
            system=system,
            user=extraction_user(record, report_text),
            schema=ExtractionAnswer,
            entity="record",
            entity_id=record_id,
            max_tokens=self.settings.max_tokens,
        )
        answer: ExtractionAnswer = result.value
        page_objects = [PdfPage(page_no=p["page_no"], text=p["text"]) for p in pages]

        # Storing happens in one transaction; the direction checks are network
        # calls and are made afterwards, so a write lock is never held open
        # across a request to a model.
        stored: list[tuple[int, Relationship, list[str]]] = []
        with self.db.transaction():
            self._store_report(
                run_id, record_id, "ai", self.client.provider.model,
                answer.report, result.ai_call_id, page_objects, outcome,
            )
            for position, relationship in enumerate(answer.relationships, start=1):
                relationship_id, problems = self._store_relationship(
                    run_id, record_id, "ai", self.client.provider.model,
                    relationship, position, result.ai_call_id, page_objects, outcome,
                )
                stored.append((relationship_id, relationship, problems))
            if not answer.relationships:
                self._queue(
                    record_id, "nothing_extracted", run_id,
                    "No relationship was extracted from this report. Either it "
                    "states none, or the extraction missed them.",
                )
                outcome.n_queued += 1

        for relationship_id, relationship, problems in stored:
            mismatch = False
            if self.settings.verify_directions and relationship.quote:
                agrees, note = self._check_direction(
                    relationship, relationship_id, outcome
                )
                if agrees is False:
                    problems = problems + [note]
                    mismatch = True
            if not problems:
                continue
            with self.db.transaction():
                self.db.execute(
                    "UPDATE relationship SET needs_review = 1, review_reason = ? "
                    "WHERE id = ?",
                    ("; ".join(p for p in problems if p), relationship_id),
                )
                self._queue(
                    record_id,
                    "direction_mismatch" if mismatch else "quote_failed",
                    run_id,
                    f"{relationship.predictor_label} → {relationship.outcome_label}: "
                    + "; ".join(p for p in problems if p),
                )
            outcome.n_queued += 1

    # -- storing -----------------------------------------------------------

    def _store_report(
        self,
        run_id: int | None,
        record_id: int,
        coder_role: str,
        coder: str,
        report: ReportExtraction,
        ai_call_id: int | None,
        pages: list[PdfPage],
        outcome: ExtractionOutcome | None = None,
    ) -> int:
        location = find_quote(report.quote, pages, report.quote_page) if report.quote else None
        problems: list[str] = []
        if report.design_downgraded:
            problems.append(report.design_note)
            if outcome:
                outcome.n_designs_downgraded += 1
        if not report.instrument_matches_design:
            problems.append(
                f"appraised with {report.appraisal_instrument or 'nothing'} but "
                f"the design suggests {report.expected_instrument}"
            )
            if outcome:
                outcome.n_instrument_mismatches += 1
        if report.quote and location is not None and not location.found:
            problems.append("the supporting quotation is not in the report")

        payload = {
            "run_id": run_id,
            "record_id": record_id,
            "coder_role": coder_role,
            "coder": coder,
            "country": report.country,
            "setting": report.setting,
            "population": report.population,
            "design": report.design,
            "design_family": report.design_family,
            "manipulated_treatment": 1 if report.manipulated_treatment else 0,
            "control_group": 1 if report.control_group else 0,
            "design_downgraded": 1 if report.design_downgraded else 0,
            "design_note": report.design_note,
            "sampling": report.sampling,
            "sample_size": report.sample_size,
            "sample_size_text": report.sample_size_text,
            "time_horizon": report.time_horizon,
            "theories": json.dumps(
                [t.model_dump() for t in report.theories], ensure_ascii=False
            ),
            "framework_stages": json.dumps(report.framework_stages, ensure_ascii=False),
            "key_findings": report.key_findings,
            "limitations": report.limitations,
            "appraisal_instrument": report.appraisal_instrument,
            "appraisal_note": report.appraisal_note,
            "appraisal_concern": report.appraisal_concern,
            "instrument_matches_design": 1 if report.instrument_matches_design else 0,
            "quote": report.quote,
            "quote_page": report.quote_page,
            "quote_verified": 1 if (location and location.on_cited_page) else 0,
            "quote_found_on": location.page_no if location else None,
            "needs_review": 1 if problems else 0,
            "review_reason": "; ".join(p for p in problems if p),
            "ai_call_id": ai_call_id,
            "created_at": utc_now(),
        }
        row_id = self._upsert("report_extraction", payload, ("record_id", "coder_role"))
        if problems and coder_role == "ai":
            self._queue(
                record_id, "design_rule", run_id, "; ".join(p for p in problems if p)
            )
            if outcome:
                outcome.n_queued += 1
        return row_id

    def _store_relationship(
        self,
        run_id: int | None,
        record_id: int,
        coder_role: str,
        coder: str,
        relationship: Relationship,
        position: int,
        ai_call_id: int | None,
        pages: list[PdfPage],
        outcome: ExtractionOutcome | None = None,
    ) -> tuple[int, list[str]]:
        """Store one relationship; return its id and anything wrong with it."""
        location = (
            find_quote(relationship.quote, pages, relationship.quote_page)
            if relationship.quote
            else None
        )
        if outcome and relationship.quote:
            outcome.n_quotes_checked += 1
            if location is None or not location.found:
                outcome.n_quotes_not_found += 1
            elif location.on_cited_page:
                outcome.n_quotes_on_cited_page += 1
            else:
                outcome.n_quotes_elsewhere += 1

        payload = {
            "run_id": run_id,
            "record_id": record_id,
            "coder_role": coder_role,
            "coder": coder,
            "position": position,
            "predictor_label": relationship.predictor_label,
            "outcome_label": relationship.outcome_label,
            "mediator_label": relationship.mediator_label,
            "moderator_label": relationship.moderator_label,
            "relationship_type": relationship.relationship_type,
            "direction": relationship.direction,
            "strength": relationship.strength,
            "statistic": relationship.statistic,
            "significance": relationship.significance,
            "evidence_basis": relationship.evidence_basis,
            "quote": relationship.quote,
            "quote_page": relationship.quote_page,
            "quote_verified": 1 if (location and location.on_cited_page) else 0,
            "quote_found_on": location.page_no if location else None,
            "verify_detail": location.detail if location else "",
            "needs_review": 0,
            "review_reason": "",
            "ai_call_id": ai_call_id,
            "created_at": utc_now(),
        }
        relationship_id = self.db.insert("relationship", payload)
        if outcome:
            outcome.n_relationships += 1

        problems: list[str] = []
        if relationship.quote and (location is None or not location.found):
            problems.append("the quotation is not in the report")
        elif location is not None and location.found and not location.on_cited_page:
            problems.append(location.detail)
        return relationship_id, problems

    def _check_direction(
        self,
        relationship: Relationship,
        relationship_id: int,
        outcome: ExtractionOutcome | None,
    ) -> tuple[bool | None, str]:
        """Re-derive the direction from the quotation alone."""
        result = self.client.ask(
            "extract:direction",
            system=direction_system(),
            user=direction_user(
                relationship.predictor_label,
                relationship.outcome_label,
                relationship.quote,
            ),
            schema=DirectionCheck,
            entity="relationship",
            entity_id=relationship_id,
            max_tokens=200,
        )
        check: DirectionCheck = result.value
        if outcome:
            outcome.n_directions_checked += 1

        # "unclear" from either side is not a disagreement about direction; it
        # is one of them declining to call it, which the reviewer can see.
        if relationship.direction == "unclear" or check.direction == "unclear":
            agrees: bool | None = None
        else:
            agrees = check.direction == relationship.direction
        note = ""
        if agrees is False:
            note = (
                f"the extraction recorded a {relationship.direction} direction, "
                f"but the quotation alone reads as {check.direction}"
            )
            if outcome:
                outcome.n_direction_mismatches += 1

        self.db.execute(
            "UPDATE relationship SET verified_direction = ?, verify_agrees = ?, "
            "verify_note = ?, verify_call_id = ? WHERE id = ?",
            (
                check.direction,
                None if agrees is None else (1 if agrees else 0),
                check.note or note,
                result.ai_call_id,
                relationship_id,
            ),
        )
        return agrees, note

    def _upsert(self, table: str, payload: dict[str, Any], keys: tuple[str, ...]) -> int:
        where = " AND ".join(f"{k} = ?" for k in keys)
        existing = self.db.query_one(
            f"SELECT id FROM {table} WHERE {where}", tuple(payload[k] for k in keys)
        )
        if existing is not None:
            assignments = ", ".join(f"{k} = :{k}" for k in payload)
            payload = dict(payload) | {"id": existing["id"]}
            self.db.execute(f"UPDATE {table} SET {assignments} WHERE id = :id", payload)
            return int(existing["id"])
        return self.db.insert(table, payload)

    def _queue(self, record_id: int, reason: str, run_id: int | None, detail: str) -> None:
        existing = self.db.query_one(
            "SELECT id FROM review_queue WHERE record_id = ? AND reason = ? "
            "AND stage = 'extract' AND resolved_at IS NULL",
            (record_id, reason),
        )
        if existing is not None:
            # Already queued for this reason; add what is new to the detail
            # rather than opening a second item for the same report.
            self.db.execute(
                "UPDATE review_queue SET detail = detail || char(10) || ? WHERE id = ?",
                (detail, existing["id"]),
            )
            return
        self.db.insert(
            "review_queue",
            {
                "record_id": record_id,
                "reason": reason,
                "detail": detail,
                "stage": "extract",
                "created_at": utc_now(),
            },
        )

    # -- reading back ------------------------------------------------------

    def extraction_for(self, record_id: int, coder_role: str = "ai") -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM report_extraction WHERE record_id = ? AND coder_role = ?",
            (record_id, coder_role),
        )
        if row is None:
            return None
        data = dict(row)
        for key in ("theories", "framework_stages"):
            try:
                data[key] = json.loads(data.get(key) or "[]")
            except ValueError:
                data[key] = []
        return data

    def relationships_for(
        self, record_id: int, coder_role: str = "ai"
    ) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM relationship WHERE record_id = ? AND coder_role = ? "
                "ORDER BY position, id",
                (record_id, coder_role),
            )
        ]

    def inventory(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Every relationship the synthesis will read."""
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM v_relationship_inventory LIMIT ?", (limit,)
            )
        ]

    def review_queue(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT q.*, r.title, r.year, r.journal "
            "FROM review_queue q JOIN record r ON r.id = q.record_id "
            "WHERE q.resolved_at IS NULL AND q.stage = 'extract' "
            "ORDER BY CASE q.reason WHEN 'direction_mismatch' THEN 0 "
            "                       WHEN 'quote_failed' THEN 1 "
            "                       WHEN 'design_rule' THEN 2 ELSE 3 END, q.id "
            "LIMIT ?",
            (limit,),
        )
        out = []
        for row in rows:
            item = dict(row)
            item["extraction"] = self.extraction_for(item["record_id"])
            item["relationships"] = self.relationships_for(item["record_id"])
            out.append(item)
        return out

    # -- human corrections -------------------------------------------------

    def correct_relationship(
        self,
        relationship_id: int,
        *,
        actor: str | None = None,
        reason: str = "",
        **fields: Any,
    ) -> None:
        """Change a relationship by hand. The human decision always wins."""
        allowed = {
            "predictor_label", "outcome_label", "mediator_label", "moderator_label",
            "relationship_type", "direction", "strength", "statistic",
            "significance", "evidence_basis",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ExtractionError(f"Unknown relationship fields: {sorted(unknown)}")
        if not fields:
            return
        actor = actor or self.audit.actor
        before = self.db.query_one(
            "SELECT * FROM relationship WHERE id = ?", (relationship_id,)
        )
        if before is None:
            raise ExtractionError(f"No relationship with id {relationship_id}")
        changed = {k: v for k, v in fields.items() if before[k] != v}
        if not changed:
            return
        assignments = ", ".join(f"{k} = :{k}" for k in changed)
        with self.db.transaction():
            self.db.execute(
                f"UPDATE relationship SET {assignments}, coder = :coder, "
                "needs_review = 0, review_reason = '' WHERE id = :id",
                changed | {"id": relationship_id, "coder": actor},
            )
            self.audit.log_override(
                "extract", "relationship", relationship_id,
                ", ".join(sorted(changed)),
                {k: before[k] for k in changed},
                changed,
                reason=reason,
                actor=actor,
            )

    def resolve(
        self, record_id: int, note: str = "", *, actor: str | None = None
    ) -> None:
        """Mark a report's extraction as checked by a human."""
        actor = actor or self.audit.actor
        with self.db.transaction():
            self.db.execute(
                "UPDATE review_queue SET resolved_at = ?, resolution = ? "
                "WHERE record_id = ? AND stage = 'extract' AND resolved_at IS NULL",
                (utc_now(), note or "checked", record_id),
            )
            self.db.execute(
                "UPDATE report_extraction SET needs_review = 0 WHERE record_id = ? "
                "AND coder_role = 'ai'",
                (record_id,),
            )
            self.db.execute(
                "UPDATE relationship SET needs_review = 0 WHERE record_id = ? "
                "AND coder_role = 'ai'",
                (record_id,),
            )
            self.audit.log(
                "extract", "extraction_checked", entity="record", entity_id=record_id,
                detail={"note": note}, actor=actor,
            )

    def delete_relationship(
        self, relationship_id: int, reason: str, *, actor: str | None = None
    ) -> None:
        """Remove a relationship the extraction should not have recorded.

        It is marked rather than deleted, so the inventory stops counting it
        while the audit trail keeps the fact that it was once there.
        """
        if not reason.strip():
            raise ExtractionError(
                "Say why this relationship should not be counted; it is kept "
                "in the record either way."
            )
        actor = actor or self.audit.actor
        before = self.db.query_one(
            "SELECT * FROM relationship WHERE id = ?", (relationship_id,)
        )
        if before is None:
            raise ExtractionError(f"No relationship with id {relationship_id}")
        with self.db.transaction():
            self.db.execute(
                "UPDATE relationship SET coder_role = 'human', coder = ?, "
                "needs_review = 0, review_reason = ? WHERE id = ?",
                (actor, f"withdrawn: {reason.strip()}", relationship_id),
            )
            self.audit.log_override(
                "extract", "relationship", relationship_id, "withdrawn",
                {"predictor_label": before["predictor_label"],
                 "outcome_label": before["outcome_label"]},
                None, reason=reason, actor=actor,
            )

    # -- reporting ---------------------------------------------------------

    def runs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM extraction_run ORDER BY id")]

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        return counts | {
            "protocol_locked": self.project.protocol.is_locked(),
            "n_pending": len(self.pending_records()),
            "n_relationships_flagged": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM relationship WHERE coder_role = 'ai' "
                    "AND needs_review = 1"
                ) or 0
            ),
            "n_designs_downgraded": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM report_extraction WHERE design_downgraded = 1"
                ) or 0
            ),
        }
