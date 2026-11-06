"""Assessing retrieved reports for eligibility.

One AI assessment per report, followed by a check the AI cannot talk its way
past: the words it quoted are looked for on the page it cited. Three outcomes
are kept apart, because they mean different things:

* **found on the cited page** — the assessment is grounded;
* **found elsewhere in the report** — a page number off by a little, which a
  reviewer can accept at a glance;
* **found nowhere** — the decision rests on words the report does not
  contain, so it is flagged and queued whatever it decided.

An exclusion here is final and is reported in the PRISMA diagram, so anything
borderline goes in front of a human rather than through.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..ai.client import AIClient, BudgetExceeded, CostEstimate
from ..core.db import utc_now
from .pdf import PdfPage, find_quote
from .prompts import PROMPT_VERSION, assessor_system, eligibility_user, render_report
from .schemas import EligibilityAnswer

__all__ = [
    "EligibilityEngine",
    "EligibilitySettings",
    "FullTextOutcome",
    "EligibilityError",
]

ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]


class EligibilityError(RuntimeError):
    pass


@dataclass
class EligibilitySettings:
    max_tokens: int = 900
    #: How much of a report is sent. A long paper is trimmed, and the prompt
    #: says which pages were left out.
    max_report_chars: int = 60_000
    head_pages: int = 6
    budget_usd: float | None = None
    #: Send every include to a human as well. On by default: at this stage an
    #: inclusion decides what the review is made of.
    review_all_inclusions: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "max_report_chars": self.max_report_chars,
            "head_pages": self.head_pages,
            "budget_usd": self.budget_usd,
            "review_all_inclusions": self.review_all_inclusions,
            "prompt_version": PROMPT_VERSION,
        }


@dataclass
class FullTextOutcome:
    run_id: int
    status: str = "finished"
    n_records: int = 0
    n_done: int = 0
    n_included: int = 0
    n_excluded: int = 0
    n_unclear: int = 0
    n_quotes_checked: int = 0
    n_quotes_on_cited_page: int = 0
    n_quotes_elsewhere: int = 0
    n_quotes_not_found: int = 0
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
            "n_included": self.n_included,
            "n_excluded": self.n_excluded,
            "n_unclear": self.n_unclear,
            "n_quotes_checked": self.n_quotes_checked,
            "n_quotes_on_cited_page": self.n_quotes_on_cited_page,
            "n_quotes_elsewhere": self.n_quotes_elsewhere,
            "n_quotes_not_found": self.n_quotes_not_found,
            "n_queued": self.n_queued,
            "n_calls": self.n_calls,
            "n_cached": self.n_cached,
            "cost_usd": round(self.cost_usd, 4),
            "messages": list(self.messages),
        }


_RECORD_COLUMNS = "id, title, abstract, year, journal, doc_type, language, authors"


class EligibilityEngine:
    def __init__(
        self,
        project,
        client: AIClient,
        settings: EligibilitySettings | None = None,
    ) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.client = client
        self.settings = settings or EligibilitySettings()
        if self.settings.budget_usd is not None:
            self.client.set_budget(self.settings.budget_usd)

    # -- what there is to do ----------------------------------------------

    def pending_records(self) -> list[dict[str, Any]]:
        """Retrieved reports with no eligibility decision yet."""
        return [
            dict(r)
            for r in self.db.query(
                f"SELECT {_RECORD_COLUMNS} FROM record r "
                "JOIN report rep ON rep.record_id = r.id AND rep.status = 'retrieved' "
                "WHERE r.active = 1 "
                "AND NOT EXISTS (SELECT 1 FROM fulltext_status f "
                "                WHERE f.record_id = r.id) ORDER BY r.id"
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
        row = self.db.query_one("SELECT * FROM v_prisma_fulltext")
        return dict(row) if row else {}

    def exclusion_reasons(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM v_fulltext_exclusion_reasons ORDER BY n DESC, exclusion_code"
            )
        ]

    # -- the protocol ------------------------------------------------------

    def _protocol(self) -> tuple[str, int | None]:
        current = self.project.protocol.current()
        if current is None:
            raise EligibilityError(
                "The protocol has not been locked. Eligibility decisions are "
                "final and are reported in the PRISMA diagram, so they must be "
                "traceable to a fixed protocol."
            )
        return current.content, current.id

    # -- estimating --------------------------------------------------------

    def estimate(self) -> CostEstimate:
        records = self.pending_records()
        protocol_text, _ = self._protocol()
        system = assessor_system(protocol_text, self.project.exclusion_codes())
        prompts: list[tuple[str, str]] = []
        for record in records:
            text, _ = render_report(
                self._pages(record["id"]),
                max_chars=self.settings.max_report_chars,
                head_pages=self.settings.head_pages,
            )
            prompts.append((system, eligibility_user(record, text)))
        estimate = self.client.estimate(
            prompts, schema=EligibilityAnswer, expected_completion_tokens=220
        )
        estimate.notes.append(
            "Full texts are much longer than abstracts, so this stage costs "
            "far more per record than screening did."
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
    ) -> FullTextOutcome:
        actor = actor or self.audit.actor
        protocol_text, protocol_id = self._protocol()
        system = assessor_system(protocol_text, self.project.exclusion_codes())

        records = self.pending_records()
        if limit is not None:
            records = records[:limit]

        baseline = (self.client.n_calls, self.client.n_cached, self.client.spent_usd)
        run_id = self.db.insert(
            "fulltext_run",
            {
                "kind": "fulltext",
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
        outcome = FullTextOutcome(run_id=run_id, n_records=len(records))

        for index, record in enumerate(records, start=1):
            if should_stop is not None and should_stop():
                outcome.status = "paused"
                outcome.messages.append(
                    f"Paused after {outcome.n_done} of {len(records)} reports. "
                    "Nothing already assessed will be asked again."
                )
                break
            try:
                self._assess(run_id, record, system, outcome, actor)
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break
            outcome.n_done = index
            if on_progress:
                on_progress(index, len(records), f"Assessed {index} of {len(records)}")

        counts = self.counts()
        outcome.n_included = counts.get("n_included", 0)
        outcome.n_excluded = counts.get("n_excluded", 0)
        outcome.n_unclear = counts.get("n_unclear", 0)
        outcome.n_calls = self.client.n_calls - baseline[0]
        outcome.n_cached = self.client.n_cached - baseline[1]
        outcome.cost_usd = round(self.client.spent_usd - baseline[2], 6)

        if outcome.n_quotes_not_found:
            outcome.messages.append(
                f"{outcome.n_quotes_not_found} assessment(s) quoted words that "
                "are not in the report at all. Those are flagged and queued "
                "for you whatever they decided."
            )
        self.db.execute(
            "UPDATE fulltext_run SET status = ?, finished_at = ?, n_done = ?, "
            "n_calls = ?, n_cached = ?, cost_usd = ? WHERE id = ?",
            (
                outcome.status,
                utc_now() if outcome.status != "paused" else None,
                outcome.n_done, outcome.n_calls, outcome.n_cached,
                outcome.cost_usd, run_id,
            ),
        )
        self.audit.log(
            "fulltext", "eligibility_run", entity="fulltext_run", entity_id=run_id,
            detail=outcome.as_dict(), actor=actor,
        )
        return outcome

    def _assess(
        self,
        run_id: int,
        record: dict[str, Any],
        system: str,
        outcome: FullTextOutcome,
        actor: str,
    ) -> None:
        record_id = record["id"]
        pages = self._pages(record_id)
        report_text, shown = render_report(
            pages,
            max_chars=self.settings.max_report_chars,
            head_pages=self.settings.head_pages,
        )
        result = self.client.ask(
            "fulltext:assess",
            system=system,
            user=eligibility_user(record, report_text),
            schema=EligibilityAnswer,
            entity="record",
            entity_id=record_id,
            max_tokens=self.settings.max_tokens,
        )
        answer: EligibilityAnswer = result.value

        location = find_quote(
            answer.quote,
            [PdfPage(page_no=p["page_no"], text=p["text"]) for p in pages],
            answer.quote_page,
        )
        if answer.quote:
            outcome.n_quotes_checked += 1
            if not location.found:
                outcome.n_quotes_not_found += 1
            elif location.on_cited_page:
                outcome.n_quotes_on_cited_page += 1
            else:
                outcome.n_quotes_elsewhere += 1

        with self.db.transaction():
            self._store_decision(
                run_id, record_id, "assessor", self.client.provider.model,
                answer, result.ai_call_id, location,
            )
            needs_review, reason, queue_reason = self._triage(answer, location)
            self._set_status(
                record_id, answer.decision, answer.exclusion_code, "ai",
                self.client.provider.model,
                needs_review=needs_review, review_reason=reason,
            )
            if needs_review:
                self._queue(record_id, queue_reason, run_id, reason)
                outcome.n_queued += 1

    def _triage(self, answer: EligibilityAnswer, location) -> tuple[bool, str, str]:
        """Decide whether a human has to look at this one, and why."""
        if answer.quote and not location.found:
            return (
                True,
                "the quotation supporting this decision is not in the report",
                "quote_failed",
            )
        if answer.decision == "unclear":
            return True, "the assessor could not settle it", "unclear"
        if answer.decision == "exclude" and not answer.quote:
            return (
                True,
                "excluded without quoting anything from the report",
                "quote_failed",
            )
        if answer.quote and not location.on_cited_page:
            return True, location.detail, "quote_failed"
        if answer.decision == "include" and self.settings.review_all_inclusions:
            return (
                True,
                "included: confirm before it enters the synthesis",
                "confirm_inclusion",
            )
        return False, "", ""

    def _store_decision(
        self,
        run_id: int | None,
        record_id: int,
        role: str,
        reviewer: str,
        answer: EligibilityAnswer,
        ai_call_id: int | None,
        location,
    ) -> int:
        payload = {
            "run_id": run_id,
            "record_id": record_id,
            "reviewer_role": role,
            "reviewer": reviewer,
            "decision": answer.decision,
            "exclusion_code": answer.exclusion_code,
            "reason": answer.reason,
            "quote": answer.quote,
            "quote_page": answer.quote_page,
            "quote_verified": 1 if (location and location.on_cited_page) else 0,
            "quote_found_on": location.page_no if location else None,
            "verify_detail": (location.detail if location else ""),
            "ai_call_id": ai_call_id,
            "created_at": utc_now(),
        }
        existing = self.db.query_one(
            "SELECT id FROM fulltext_decision WHERE record_id = ? "
            "AND reviewer_role = ? AND IFNULL(run_id, 0) = IFNULL(?, 0)",
            (record_id, role, run_id),
        )
        if existing is not None:
            assignments = ", ".join(f"{k} = :{k}" for k in payload)
            payload["id"] = existing["id"]
            self.db.execute(
                f"UPDATE fulltext_decision SET {assignments} WHERE id = :id", payload
            )
            return int(existing["id"])
        return self.db.insert("fulltext_decision", payload)

    def _set_status(
        self,
        record_id: int,
        decision: str,
        exclusion_code: str,
        source: str,
        decided_by: str,
        *,
        needs_review: bool = False,
        review_reason: str = "",
    ) -> None:
        self.db.execute(
            "INSERT INTO fulltext_status(record_id, decision, exclusion_code, "
            "  source, decided_by, decided_at, needs_review, review_reason) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(record_id) DO UPDATE SET decision = excluded.decision, "
            "  exclusion_code = excluded.exclusion_code, source = excluded.source, "
            "  decided_by = excluded.decided_by, decided_at = excluded.decided_at, "
            "  needs_review = excluded.needs_review, "
            "  review_reason = excluded.review_reason",
            (
                record_id, decision, exclusion_code, source, decided_by, utc_now(),
                1 if needs_review else 0, review_reason,
            ),
        )

    def _queue(self, record_id: int, reason: str, run_id: int | None, detail: str) -> None:
        existing = self.db.query_one(
            "SELECT id FROM review_queue WHERE record_id = ? AND reason = ? "
            "AND stage = 'fulltext' AND resolved_at IS NULL",
            (record_id, reason),
        )
        if existing is not None:
            return
        self.db.insert(
            "review_queue",
            {
                "record_id": record_id,
                "reason": reason,
                "detail": detail,
                "stage": "fulltext",
                "created_at": utc_now(),
            },
        )

    # -- the human queue ---------------------------------------------------

    def review_queue(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT q.*, r.title, r.year, r.journal, r.authors, r.doi, "
            "       f.decision AS current_decision, f.exclusion_code AS current_code, "
            "       f.source AS decided_how, f.review_reason, "
            "       rep.filename, rep.n_pages, rep.n_pages_ocr "
            "FROM review_queue q "
            "JOIN record r ON r.id = q.record_id "
            "LEFT JOIN fulltext_status f ON f.record_id = q.record_id "
            "LEFT JOIN report rep ON rep.record_id = q.record_id "
            "WHERE q.resolved_at IS NULL AND q.stage = 'fulltext' "
            "ORDER BY CASE q.reason WHEN 'quote_failed' THEN 0 "
            "                       WHEN 'unclear' THEN 1 "
            "                       WHEN 'conflict' THEN 2 ELSE 3 END, q.id LIMIT ?",
            (limit,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["authors"] = json.loads(item.get("authors") or "[]")
            except ValueError:
                item["authors"] = []
            item["assessments"] = self.decisions_for(item["record_id"])
            out.append(item)
        return out

    def decisions_for(self, record_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT reviewer_role, reviewer, decision, exclusion_code, reason, "
                "       quote, quote_page, quote_verified, quote_found_on, "
                "       verify_detail, created_at "
                "FROM fulltext_decision WHERE record_id = ? ORDER BY id",
                (record_id,),
            )
        ]

    def page_text(self, record_id: int, page_no: int) -> str:
        """The text of one page, so a reviewer can see the quotation in place."""
        row = self.db.query_one(
            "SELECT text FROM report_page WHERE record_id = ? AND page_no = ?",
            (record_id, page_no),
        )
        return str(row["text"]) if row else ""

    def resolve(
        self,
        record_id: int,
        decision: str,
        *,
        exclusion_code: str = "",
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        """A human settles a report. This always wins."""
        if decision not in ("include", "exclude", "unclear"):
            raise EligibilityError(f"{decision!r} is not a decision")
        if decision == "exclude" and not exclusion_code:
            raise EligibilityError(
                "An exclusion at full text needs a code: PRISMA reports these "
                "with their reasons."
            )
        actor = actor or self.audit.actor
        previous = self.db.query_one(
            "SELECT decision, exclusion_code, source FROM fulltext_status "
            "WHERE record_id = ?",
            (record_id,),
        )
        answer = EligibilityAnswer(
            decision=decision,
            exclusion_code=exclusion_code if decision == "exclude" else "",
            reason=reason or "Decided by the reviewer after reading the report.",
        )
        with self.db.transaction():
            self._store_decision(None, record_id, "human", actor, answer, None, None)
            self._set_status(
                record_id, decision, answer.exclusion_code, "human", actor
            )
            self.db.execute(
                "UPDATE review_queue SET resolved_at = ?, resolution = ? "
                "WHERE record_id = ? AND stage = 'fulltext' AND resolved_at IS NULL",
                (utc_now(), decision, record_id),
            )
            if previous is not None and previous["decision"] != decision:
                self.audit.log_override(
                    "fulltext", "record", record_id, "eligibility",
                    {"decision": previous["decision"],
                     "exclusion_code": previous["exclusion_code"]},
                    {"decision": decision, "exclusion_code": answer.exclusion_code},
                    reason=reason, actor=actor,
                )
            else:
                self.audit.log(
                    "fulltext", "human_review", entity="record", entity_id=record_id,
                    detail={"decision": decision,
                            "exclusion_code": answer.exclusion_code,
                            "reason": reason,
                            "confirmed_ai": previous is not None},
                    actor=actor,
                )

    # -- reporting ---------------------------------------------------------

    def runs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM fulltext_run ORDER BY id")]

    def included_records(self) -> list[dict[str, Any]]:
        """The studies the synthesis will be built from."""
        return [
            dict(r)
            for r in self.db.query(
                "SELECT r.id, r.title, r.year, r.journal, r.doi, r.authors "
                "FROM record r JOIN fulltext_status f ON f.record_id = r.id "
                "WHERE f.decision = 'include' ORDER BY r.year DESC, r.id"
            )
        ]

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        return counts | {
            "protocol_locked": self.project.protocol.is_locked(),
            "n_pending": len(self.pending_records()),
            "unverified_quotes": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM fulltext_decision "
                    "WHERE quote <> '' AND quote_verified = 0"
                ) or 0
            ),
            "quotes_not_found": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM fulltext_decision "
                    "WHERE quote <> '' AND quote_found_on IS NULL"
                ) or 0
            ),
        }
