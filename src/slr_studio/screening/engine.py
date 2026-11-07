"""Running title and abstract screening.

The order is fixed and each step is stored before the next begins, so a run
that is paused, interrupted or re-run picks up where it stopped without
re-asking anything already answered:

1. **Pilot.** The reviewer labels a random sample by hand, the AI screens the
   same sample, and agreement is reported. The full run is blocked until that
   has happened or been explicitly waived.
2. **Two blinded screeners.** Each record is judged twice, by personas that
   differ in what they do with ambiguity.
3. **Adjudication.** A third call settles include/exclude disagreements.
4. **Quality control.** A reproducible random sample of the records both
   screeners excluded, plus every exclusion that trips a keyword signal from
   the protocol, is read again by a fresh call.
5. **The human queue.** Conflicts, unclear records, QC rescues and a random
   audit of agreed decisions. A human decision always overrides the AI's.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from ..ai.base import ProviderError, RateLimitError
from ..ai.client import AIClient, BudgetExceeded, CostEstimate
from ..core.db import utc_now
from ..core.hashing import sha256_text
from .prompts import (
    PERSONAS,
    PROMPT_VERSION,
    adjudication_user,
    adjudicator_system,
    qc_system,
    screener_system,
    screening_user,
)
from .schemas import AdjudicationAnswer, ScreeningAnswer
from .stats import AgreementResult, agreement

__all__ = ["ScreeningEngine", "ScreeningSettings", "RunOutcome", "ScreeningError"]

CATEGORIES = ("include", "exclude", "unclear")

#: The conventional pilot size. Advisory, not enforced: see ScreeningSettings.
PILOT_CONVENTIONAL_MINIMUM = 50
PILOT_CONVENTIONAL_MAXIMUM = 100
_RECORD_COLUMNS = (
    "id, title, abstract, authors, keywords, year, journal, doc_type, language, "
    "has_abstract, source_database"
)


class ScreeningError(RuntimeError):
    pass


@dataclass
class ScreeningSettings:
    #: How many records the reviewer labels by hand before trusting the AI.
    #: The convention is 50 to 100; a pilot outside that band still runs, and
    #: the run says so, because a reviewer with 30 records in total should not
    #: be blocked by a rule written for corpora of thousands.
    pilot_size: int = 60
    #: The agreement the pilot must reach. Advisory: the reviewer is told when
    #: it is not met and decides whether to go on, because no threshold is
    #: right for every review.
    pilot_target_kappa: float = 0.60
    #: Share of joint exclusions re-read by the quality-control pass.
    qc_fraction: float = 0.10
    qc_minimum: int = 10
    #: Share of agreed decisions shown to the human anyway, as an audit.
    audit_fraction: float = 0.05
    audit_minimum: int = 5
    max_tokens: int = 700
    budget_usd: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pilot_size": self.pilot_size,
            "pilot_target_kappa": self.pilot_target_kappa,
            "qc_fraction": self.qc_fraction,
            "qc_minimum": self.qc_minimum,
            "audit_fraction": self.audit_fraction,
            "audit_minimum": self.audit_minimum,
            "max_tokens": self.max_tokens,
            "budget_usd": self.budget_usd,
            "prompt_version": PROMPT_VERSION,
        }


@dataclass
class RunOutcome:
    run_id: int
    kind: str
    status: str = "finished"
    n_records: int = 0
    n_done: int = 0
    n_included: int = 0
    n_excluded: int = 0
    n_unclear: int = 0
    n_conflicts: int = 0
    n_adjudicated: int = 0
    n_qc_rescued: int = 0
    n_queued: int = 0
    n_calls: int = 0
    n_cached: int = 0
    cost_usd: float = 0.0
    agreement: AgreementResult | None = None
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = {
            "run_id": self.run_id,
            "kind": self.kind,
            "status": self.status,
            "n_records": self.n_records,
            "n_done": self.n_done,
            "n_included": self.n_included,
            "n_excluded": self.n_excluded,
            "n_unclear": self.n_unclear,
            "n_conflicts": self.n_conflicts,
            "n_adjudicated": self.n_adjudicated,
            "n_qc_rescued": self.n_qc_rescued,
            "n_queued": self.n_queued,
            "n_calls": self.n_calls,
            "n_cached": self.n_cached,
            "cost_usd": round(self.cost_usd, 4),
            "messages": list(self.messages),
        }
        if self.agreement is not None:
            data["agreement"] = self.agreement.as_dict()
        return data


ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]


class ScreeningEngine:
    def __init__(
        self,
        project,
        client: AIClient,
        settings: ScreeningSettings | None = None,
    ) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.client = client
        self.settings = settings or ScreeningSettings()
        if self.settings.budget_usd is not None:
            self.client.set_budget(self.settings.budget_usd)

    # -- what there is to do ----------------------------------------------

    def pending_records(self) -> list[dict[str, Any]]:
        """Active records with no settled screening decision yet."""
        return [
            dict(r)
            for r in self.db.query(
                f"SELECT {_RECORD_COLUMNS} FROM record r WHERE r.active = 1 "
                "AND NOT EXISTS (SELECT 1 FROM screening_status s "
                "                WHERE s.record_id = r.id) ORDER BY r.id"
            )
        ]

    def _record(self, record_id: int) -> dict[str, Any]:
        row = self.db.query_one(
            f"SELECT {_RECORD_COLUMNS} FROM record WHERE id = ?", (record_id,)
        )
        if row is None:
            raise ScreeningError(f"No record with id {record_id}")
        return dict(row)

    def counts(self) -> dict[str, int]:
        row = self.db.query_one("SELECT * FROM v_prisma_screening")
        return dict(row) if row else {}

    def exclusion_reasons(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT * FROM v_exclusion_reasons ORDER BY n DESC, exclusion_code"
        )]

    # -- reproducible sampling --------------------------------------------

    def _rng(self, scope: str) -> random.Random:
        """A generator seeded from the project and the scope.

        Same project, same scope, same sample -- on any machine, at any time.
        That is what makes "a reproducible random sample" in the Methods
        section a claim somebody could check rather than a form of words.
        """
        info = self.project.info()
        seed_text = f"{info['created_at']}|{info['title']}|{scope}"
        return random.Random(int(sha256_text(seed_text)[:16], 16))

    def _sample(self, items: Sequence[Any], n: int, scope: str) -> list[Any]:
        if n >= len(items):
            return list(items)
        return sorted(self._rng(scope).sample(list(items), n))

    # -- the protocol the screeners read ----------------------------------

    def _protocol_text(self) -> tuple[str, int | None]:
        current = self.project.protocol.current()
        if current is None:
            raise ScreeningError(
                "The protocol has not been locked. Screening against a "
                "protocol that can still change would make the decisions "
                "unreproducible: lock it on the Project page first."
            )
        return current.content, current.id

    def _codes(self) -> list[dict[str, Any]]:
        return self.project.exclusion_codes()

    # -- runs --------------------------------------------------------------

    def _start_run(self, kind: str, n_records: int, protocol_version: int | None) -> int:
        return self.db.insert(
            "screening_run",
            {
                "kind": kind,
                "status": "running",
                "started_at": utc_now(),
                "provider": self.client.provider.name,
                "model": self.client.provider.model,
                "temperature": self.client.temperature,
                "prompt_version": PROMPT_VERSION,
                "protocol_version": protocol_version,
                "settings": json.dumps(self.settings.as_dict()),
                "n_records": n_records,
            },
        )

    def _usage_baseline(self) -> tuple[int, int, float]:
        """The client's counters before a run starts.

        One client serves several runs, so its totals are cumulative. A run
        has to report what *it* spent, or the quality-control pass appears to
        have made every call the full run did.
        """
        return (self.client.n_calls, self.client.n_cached, self.client.spent_usd)

    def _apply_usage(self, outcome: RunOutcome, baseline: tuple[int, int, float]) -> None:
        outcome.n_calls = self.client.n_calls - baseline[0]
        outcome.n_cached = self.client.n_cached - baseline[1]
        outcome.cost_usd = round(self.client.spent_usd - baseline[2], 6)

    def _finish_run(self, run_id: int, outcome: RunOutcome) -> None:
        self.db.execute(
            "UPDATE screening_run SET status = ?, finished_at = ?, n_done = ?, "
            "n_calls = ?, n_cached = ?, cost_usd = ? WHERE id = ?",
            (
                outcome.status,
                utc_now() if outcome.status != "paused" else None,
                outcome.n_done,
                outcome.n_calls,
                outcome.n_cached,
                outcome.cost_usd,
                run_id,
            ),
        )

    # -- asking one screener ----------------------------------------------

    def _ask_screener(
        self, role: str, record: dict[str, Any], system: str, run_id: int
    ) -> tuple[ScreeningAnswer, int]:
        result = self.client.ask(
            f"screen:{role}",
            system=system,
            user=screening_user(record),
            schema=ScreeningAnswer,
            entity="record",
            entity_id=record["id"],
            max_tokens=self.settings.max_tokens,
        )
        answer: ScreeningAnswer = result.value
        self._store_decision(
            run_id, record["id"], role, self.client.provider.model, answer, result.ai_call_id,
            quote_verified=self._quote_in_record(answer.quote, record),
        )
        return answer, result.ai_call_id

    def _store_decision(
        self,
        run_id: int | None,
        record_id: int,
        role: str,
        reviewer: str,
        answer: ScreeningAnswer,
        ai_call_id: int | None,
        *,
        quote_verified: bool = False,
    ) -> int:
        existing = self.db.query_one(
            "SELECT id FROM screening_decision WHERE record_id = ? "
            "AND reviewer_role = ? AND IFNULL(run_id, 0) = IFNULL(?, 0)",
            (record_id, role, run_id),
        )
        payload = {
            "run_id": run_id,
            "record_id": record_id,
            "reviewer_role": role,
            "reviewer": reviewer,
            "decision": answer.decision,
            "exclusion_code": answer.exclusion_code,
            "reason": answer.reason,
            "quote": answer.quote,
            "quote_verified": 1 if quote_verified else 0,
            "ai_call_id": ai_call_id,
            "created_at": utc_now(),
        }
        if existing is not None:
            assignments = ", ".join(f"{k} = :{k}" for k in payload)
            payload["id"] = existing["id"]
            self.db.execute(
                f"UPDATE screening_decision SET {assignments} WHERE id = :id", payload
            )
            return int(existing["id"])
        return self.db.insert("screening_decision", payload)

    @staticmethod
    def _quote_in_record(quote: str, record: dict[str, Any]) -> bool:
        """Check a quotation really appears in the record.

        A screener that invents a supporting quotation has invented its
        reasoning. Comparison is on collapsed whitespace and case, because
        models reflow text; anything looser would stop catching fabrication.
        """
        if not quote:
            return False
        haystack = " ".join(
            f"{record.get('title') or ''} {record.get('abstract') or ''}".split()
        ).casefold()
        needle = " ".join(quote.split()).casefold().strip(" .,:;\"'")
        return bool(needle) and needle in haystack

    # -- the pilot ---------------------------------------------------------

    def pilot_sample(self) -> list[dict[str, Any]]:
        """The records the reviewer is asked to label, chosen reproducibly."""
        ids = [
            r["id"]
            for r in self.db.query("SELECT id FROM record WHERE active = 1 ORDER BY id")
        ]
        chosen = self._sample(ids, self.settings.pilot_size, "pilot")
        return [self._record(rid) for rid in chosen]

    def record_pilot_label(
        self,
        record_id: int,
        decision: str,
        *,
        exclusion_code: str = "",
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        """Store one of the reviewer's own pilot judgements."""
        if decision not in CATEGORIES:
            raise ScreeningError(f"{decision!r} is not one of {', '.join(CATEGORIES)}")
        actor = actor or self.audit.actor
        answer = ScreeningAnswer(
            decision=decision,
            exclusion_code=exclusion_code if decision == "exclude" else "",
            reason=reason or "Labelled by the reviewer during the pilot.",
        )
        with self.db.transaction():
            self._store_decision(None, record_id, "pilot_human", actor, answer, None)
            self.audit.log(
                "screen",
                "pilot_label",
                entity="record",
                entity_id=record_id,
                detail={"decision": decision, "exclusion_code": exclusion_code},
                actor=actor,
            )

    def pilot_labels(self) -> dict[int, str]:
        return {
            r["record_id"]: r["decision"]
            for r in self.db.query(
                "SELECT record_id, decision FROM screening_decision "
                "WHERE reviewer_role = 'pilot_human'"
            )
        }

    def run_pilot(
        self,
        *,
        actor: str | None = None,
        on_progress: ProgressFn | None = None,
    ) -> RunOutcome:
        """Screen the records the reviewer labelled, and compare.

        Only the inclusive screener is run here. The pilot asks whether the AI
        agrees with *this reviewer* about *this protocol*; running both
        personas would blur that with the question of whether the two personas
        agree with each other, which the full run answers separately.
        """
        actor = actor or self.audit.actor
        labels = self.pilot_labels()
        if not labels:
            raise ScreeningError(
                "Label some pilot records first: the pilot compares your own "
                "judgements with the AI's on the same records."
            )
        protocol_text, protocol_id = self._protocol_text()
        system = screener_system("screener_a", protocol_text, self._codes())
        record_ids = sorted(labels)

        baseline = self._usage_baseline()
        run_id = self._start_run("pilot", len(record_ids), protocol_id)
        outcome = RunOutcome(run_id=run_id, kind="pilot", n_records=len(record_ids))
        human: list[str] = []
        machine: list[str] = []

        for index, record_id in enumerate(record_ids, start=1):
            record = self._record(record_id)
            answer, _ = self._ask_screener("screener_a", record, system, run_id)
            human.append(labels[record_id])
            machine.append(answer.decision)
            outcome.n_done = index
            if on_progress:
                on_progress(index, len(record_ids), f"Pilot record {index} of {len(record_ids)}")

        outcome.agreement = agreement(human, machine, categories=list(CATEGORIES))
        self._apply_usage(outcome, baseline)
        self._store_agreement(run_id, "pilot:human_vs_ai", outcome.agreement)
        self._finish_run(run_id, outcome)

        if len(record_ids) < PILOT_CONVENTIONAL_MINIMUM:
            outcome.messages.append(
                f"This pilot compared {len(record_ids)} record(s). The "
                f"convention is {PILOT_CONVENTIONAL_MINIMUM} to "
                f"{PILOT_CONVENTIONAL_MAXIMUM}, and agreement measured on "
                "fewer is unstable — treat the figures below as indicative."
            )

        kappa = outcome.agreement.kappa
        if kappa is not None and kappa < self.settings.pilot_target_kappa:
            outcome.messages.append(
                f"Agreement with your own labels is κ = {kappa:.2f}, below "
                f"the {self.settings.pilot_target_kappa:.2f} you set. Consider "
                "sharpening the protocol's criteria before the full run."
            )
        self.audit.log(
            "screen", "pilot_run", entity="screening_run", entity_id=run_id,
            detail=outcome.as_dict(), actor=actor,
        )
        return outcome

    def _store_agreement(self, run_id: int, scope: str, result: AgreementResult) -> int:
        return self.db.insert(
            "agreement_report",
            {
                "run_id": run_id,
                "scope": scope,
                "n": result.n,
                "n_agree": result.n_agree,
                "percent_agreement": result.percent_agreement,
                "kappa": result.kappa,
                "pabak": result.pabak,
                "p_observed": result.p_observed,
                "p_expected": result.p_expected,
                "detail": json.dumps(result.as_dict()),
                "created_at": utc_now(),
            },
        )

    def pilot_done(self) -> bool:
        return bool(
            self.db.scalar(
                "SELECT COUNT(*) FROM agreement_report WHERE scope = 'pilot:human_vs_ai'"
            )
        )

    def pilot_waived(self) -> bool:
        return bool(
            self.db.scalar("SELECT COUNT(*) FROM screening_waiver WHERE scope = 'pilot'")
        )

    def waive_pilot(self, reason: str, *, actor: str | None = None) -> None:
        """Skip the pilot. Permitted, never silent."""
        actor = actor or self.audit.actor
        if not reason.strip():
            raise ScreeningError(
                "Waiving the pilot needs a reason. It is recorded and reported "
                "in the Methods section."
            )
        with self.db.transaction():
            self.db.insert(
                "screening_waiver",
                {
                    "scope": "pilot",
                    "reason": reason.strip(),
                    "actor": actor,
                    "created_at": utc_now(),
                },
            )
            self.audit.log(
                "screen", "pilot_waived", detail={"reason": reason.strip()}, actor=actor
            )

    # -- the full run ------------------------------------------------------

    def estimate_full_run(self) -> CostEstimate:
        """What screening everything outstanding would cost, before it starts."""
        records = self.pending_records()
        protocol_text, _ = self._protocol_text()
        codes = self._codes()
        prompts: list[tuple[str, str]] = []
        for role in ("screener_a", "screener_b"):
            system = screener_system(role, protocol_text, codes)
            prompts += [(system, screening_user(r)) for r in records]
        estimate = self.client.estimate(prompts, schema=ScreeningAnswer)
        # Adjudication is not free, but how often it happens is not knowable
        # in advance. A tenth is a stated assumption, not a measurement.
        estimate.notes.append(
            "Adjudication of disagreements is extra; budget roughly 10% more."
        )
        return estimate

    def run_full(
        self,
        *,
        actor: str | None = None,
        on_progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
        limit: int | None = None,
    ) -> RunOutcome:
        """Screen every outstanding record twice, then adjudicate.

        Safe to stop and restart: each record's decisions are committed as
        they are made, and a resumed run skips whatever is already settled.
        """
        actor = actor or self.audit.actor
        # Protocol first: it is the more fundamental precondition, and being
        # told to run a pilot against a protocol that does not exist yet would
        # send the reviewer the wrong way.
        protocol_text, protocol_id = self._protocol_text()
        if not (self.pilot_done() or self.pilot_waived()):
            raise ScreeningError(
                "Run the pilot first, or waive it with a reason. Screening "
                "thousands of records on a protocol nobody has checked the AI "
                "against is the expensive mistake this step exists to prevent."
            )
        codes = self._codes()
        systems = {
            role: screener_system(role, protocol_text, codes)
            for role in ("screener_a", "screener_b")
        }
        adjudicator = adjudicator_system(protocol_text, codes)

        records = self.pending_records()
        if limit is not None:
            records = records[:limit]
        baseline = self._usage_baseline()
        run_id = self._start_run("full", len(records), protocol_id)
        outcome = RunOutcome(run_id=run_id, kind="full", n_records=len(records))
        decisions_a: list[str] = []
        decisions_b: list[str] = []

        for index, record in enumerate(records, start=1):
            if should_stop is not None and should_stop():
                outcome.status = "paused"
                outcome.messages.append(
                    f"Paused after {outcome.n_done} of {len(records)} records. "
                    "Nothing already decided will be asked again."
                )
                break
            try:
                answer_a, _ = self._ask_screener("screener_a", record, systems["screener_a"], run_id)
                answer_b, _ = self._ask_screener("screener_b", record, systems["screener_b"], run_id)
                self._settle(run_id, record, answer_a, answer_b, adjudicator, outcome, actor)
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break
            except RateLimitError as exc:
                # A free tier's daily quota, most likely. Everything decided so
                # far is committed and cached; running again carries on.
                outcome.status = "paused"
                outcome.messages.append(
                    f"The provider's rate limit was reached ({exc}) after "
                    f"{outcome.n_done} of {len(records)} records. Nothing already "
                    "decided will be asked again when the run is resumed."
                )
                break
            except ProviderError as exc:
                if not exc.retryable:
                    raise  # a rejected key or request: waiting will not help
                outcome.status = "paused"
                outcome.messages.append(
                    f"The provider kept failing ({exc}) after {outcome.n_done} "
                    f"of {len(records)} records, and retrying for a while did "
                    "not help. Nothing already decided will be asked again "
                    "when the run is resumed."
                )
                break

            decisions_a.append(answer_a.decision)
            decisions_b.append(answer_b.decision)
            outcome.n_done = index
            if on_progress:
                on_progress(index, len(records), f"Screened {index} of {len(records)}")

        if decisions_a:
            outcome.agreement = agreement(
                decisions_a, decisions_b, categories=list(CATEGORIES)
            )
            self._store_agreement(run_id, "full:a_vs_b", outcome.agreement)

        self._tally(outcome)
        self._apply_usage(outcome, baseline)
        self._finish_run(run_id, outcome)
        self.audit.log(
            "screen", "screening_run", entity="screening_run", entity_id=run_id,
            detail=outcome.as_dict(), actor=actor,
        )
        return outcome

    def _settle(
        self,
        run_id: int,
        record: dict[str, Any],
        answer_a: ScreeningAnswer,
        answer_b: ScreeningAnswer,
        adjudicator_prompt: str,
        outcome: RunOutcome,
        actor: str,
    ) -> None:
        """Turn two screeners' answers into one settled status."""
        record_id = record["id"]

        if answer_a.decision == answer_b.decision:
            decision = answer_a.decision
            source = "agreement"
            code = answer_a.exclusion_code or answer_b.exclusion_code
            decided_by = self.client.provider.model
            if decision == "unclear":
                # Both unclear is not a settled answer, it is a question for a
                # human. It is recorded so the counts are complete, and queued.
                self._set_status(record_id, decision, code, source, decided_by,
                                 needs_review=True, review_reason="both screeners unclear")
                self._queue(record_id, "unclear", run_id,
                            "Both screeners answered unclear.")
                outcome.n_queued += 1
                return
            self._set_status(record_id, decision, code, source, decided_by)
            self._maybe_audit(record_id, run_id, outcome)
            return

        # A disagreement. Unclear against a definite answer is not worth a
        # third call: it is already a question for a human.
        if "unclear" in (answer_a.decision, answer_b.decision):
            definite = answer_a if answer_b.decision == "unclear" else answer_b
            self._set_status(
                record_id, "unclear", definite.exclusion_code, "unresolved",
                self.client.provider.model, needs_review=True,
                review_reason="one screener was unsure",
            )
            self._queue(
                record_id, "unclear", run_id,
                f"One screener said {definite.decision}, the other was unsure.",
            )
            outcome.n_conflicts += 1
            outcome.n_queued += 1
            return

        outcome.n_conflicts += 1
        result = self.client.ask(
            "screen:adjudicate",
            system=adjudicator_prompt,
            user=adjudication_user(
                record, answer_a.model_dump(), answer_b.model_dump()
            ),
            schema=AdjudicationAnswer,
            entity="record",
            entity_id=record_id,
            max_tokens=self.settings.max_tokens,
        )
        ruling: AdjudicationAnswer = result.value
        self._store_decision(
            run_id, record_id, "adjudicator", self.client.provider.model, ruling,
            result.ai_call_id,
            quote_verified=self._quote_in_record(ruling.quote, record),
        )
        outcome.n_adjudicated += 1

        if ruling.decision == "unclear":
            self._set_status(
                record_id, "unclear", "", "adjudication", self.client.provider.model,
                needs_review=True, review_reason="the adjudicator could not settle it",
            )
            self._queue(record_id, "conflict", run_id,
                        "The screeners disagreed and the adjudicator was unsure.")
            outcome.n_queued += 1
            return

        self._set_status(
            record_id, ruling.decision, ruling.exclusion_code, "adjudication",
            self.client.provider.model,
        )
        # An adjudicated record is a borderline record by definition, so it
        # goes in front of a human even though a decision now exists.
        self._queue(
            record_id, "conflict", run_id,
            f"Screeners disagreed ({answer_a.decision} vs {answer_b.decision}); "
            f"adjudicator chose {ruling.decision}.",
        )
        outcome.n_queued += 1

    def _maybe_audit(self, record_id: int, run_id: int, outcome: RunOutcome) -> None:
        """Put a share of agreed decisions in front of a human anyway.

        Two screeners agreeing is the commonest outcome and the least examined
        one. Sampling it is how a systematic error in the protocol gets caught
        before it has quietly shaped thousands of decisions.
        """
        rng = self._rng(f"audit:{record_id}")
        if rng.random() < self.settings.audit_fraction:
            self._queue(record_id, "random_audit", run_id,
                        "Random audit of a decision both screeners agreed on.")
            outcome.n_queued += 1

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
            "INSERT INTO screening_status(record_id, decision, exclusion_code, "
            "  source, decided_by, decided_at, needs_review, review_reason) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(record_id) DO UPDATE SET decision = excluded.decision, "
            "  exclusion_code = excluded.exclusion_code, source = excluded.source, "
            "  decided_by = excluded.decided_by, decided_at = excluded.decided_at, "
            "  needs_review = excluded.needs_review, "
            "  review_reason = excluded.review_reason",
            (
                record_id, decision, exclusion_code, source, decided_by,
                utc_now(), 1 if needs_review else 0, review_reason,
            ),
        )

    def _queue(self, record_id: int, reason: str, run_id: int | None, detail: str) -> None:
        existing = self.db.query_one(
            "SELECT id FROM review_queue WHERE record_id = ? AND reason = ? "
            "AND resolved_at IS NULL",
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
                "run_id": run_id,
                "created_at": utc_now(),
            },
        )

    def _tally(self, outcome: RunOutcome) -> None:
        counts = self.counts()
        outcome.n_included = counts.get("n_included", 0)
        outcome.n_excluded = counts.get("n_excluded", 0)
        outcome.n_unclear = counts.get("n_unclear", 0)

    # -- quality control ---------------------------------------------------

    def protocol_keywords(self, limit: int = 25) -> list[str]:
        """Distinctive words from the protocol's own criteria.

        Used to find exclusions that mention the review's core concepts, which
        are the exclusions most worth re-reading. Derived from the protocol
        rather than typed in separately, so it cannot drift away from it.
        """
        info = self.project.info()
        text = " ".join(
            [
                info.get("objectives", ""),
                info.get("inclusion_criteria", ""),
                " ".join(info.get("research_questions") or []),
            ]
        ).casefold()
        stop = {
            "about", "after", "against", "among", "between", "could", "every",
            "first", "included", "including", "other", "research", "review",
            "studies", "study", "their", "there", "these", "those", "through",
            "which", "whether", "while", "with", "within", "would", "what",
            "where", "when", "they", "that", "this", "from", "have", "been",
            "will", "does", "such", "into", "more", "most", "than", "then",
            "them", "also", "any", "are", "how", "its", "the", "and", "for",
        }
        words = re.findall(r"[a-z][a-z-]{3,}", text)
        seen: dict[str, int] = {}
        for word in words:
            if word in stop:
                continue
            seen[word] = seen.get(word, 0) + 1
        ranked = sorted(seen, key=lambda w: (-seen[w], w))
        return ranked[:limit]

    def qc_candidates(self) -> tuple[list[int], list[int]]:
        """(random sample, keyword-flagged) joint exclusions to re-read."""
        excluded = [
            r["record_id"]
            for r in self.db.query(
                "SELECT s.record_id FROM screening_status s "
                "JOIN record r ON r.id = s.record_id AND r.active = 1 "
                "WHERE s.decision = 'exclude' ORDER BY s.record_id"
            )
        ]
        if not excluded:
            return [], []
        n = max(self.settings.qc_minimum, int(len(excluded) * self.settings.qc_fraction))
        sampled = self._sample(excluded, min(n, len(excluded)), "qc")

        keywords = self.protocol_keywords()
        flagged: list[int] = []
        if keywords:
            for record_id in excluded:
                record = self._record(record_id)
                text = f"{record.get('title') or ''} {record.get('abstract') or ''}".casefold()
                hits = sum(1 for word in keywords if word in text)
                # Two independent signals, so a single incidental word does
                # not drag every exclusion back for re-reading.
                if hits >= 2 and record_id not in sampled:
                    flagged.append(record_id)
        return sampled, flagged

    def run_qc(
        self,
        *,
        actor: str | None = None,
        on_progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
    ) -> RunOutcome:
        """Re-read excluded records with a fresh call.

        A rescue never overturns the exclusion by itself. It puts the record
        in front of a human, because an automatic reversal would mean three
        AI calls could undo each other with nobody watching.
        """
        actor = actor or self.audit.actor
        protocol_text, protocol_id = self._protocol_text()
        system = qc_system(protocol_text, self._codes())
        sampled, flagged = self.qc_candidates()
        targets = sorted(set(sampled) | set(flagged))

        baseline = self._usage_baseline()
        run_id = self._start_run("qc", len(targets), protocol_id)
        outcome = RunOutcome(run_id=run_id, kind="qc", n_records=len(targets))

        for index, record_id in enumerate(targets, start=1):
            if should_stop is not None and should_stop():
                outcome.status = "paused"
                break
            record = self._record(record_id)
            result = self.client.ask(
                "screen:qc",
                system=system,
                user=screening_user(record),
                schema=ScreeningAnswer,
                entity="record",
                entity_id=record_id,
                max_tokens=self.settings.max_tokens,
            )
            answer: ScreeningAnswer = result.value
            self._store_decision(
                run_id, record_id, "qc", self.client.provider.model, answer,
                result.ai_call_id,
                quote_verified=self._quote_in_record(answer.quote, record),
            )
            outcome.n_done = index
            if answer.decision != "exclude":
                why = "flagged by a protocol keyword" if record_id in flagged else "random sample"
                self._queue(
                    record_id, "qc_rescue", run_id,
                    f"Quality control ({why}) read this exclusion again and "
                    f"answered {answer.decision}: {answer.reason}",
                )
                self.db.execute(
                    "UPDATE screening_status SET needs_review = 1, review_reason = ? "
                    "WHERE record_id = ?",
                    (f"quality control answered {answer.decision}", record_id),
                )
                outcome.n_qc_rescued += 1
                outcome.n_queued += 1
            if on_progress:
                on_progress(index, len(targets), f"Re-read {index} of {len(targets)}")

        self._tally(outcome)
        self._apply_usage(outcome, baseline)
        self._finish_run(run_id, outcome)
        self.audit.log(
            "screen", "qc_run", entity="screening_run", entity_id=run_id,
            detail=outcome.as_dict() | {
                "n_sampled": len(sampled), "n_keyword_flagged": len(flagged),
            },
            actor=actor,
        )
        return outcome

    # -- the human queue ---------------------------------------------------

    def review_queue(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT q.*, r.title, r.abstract, r.year, r.journal, r.doc_type, "
            "       r.authors, r.source_database, r.doi, "
            "       s.decision AS current_decision, s.exclusion_code AS current_code, "
            "       s.source AS decided_how "
            "FROM review_queue q "
            "JOIN record r ON r.id = q.record_id "
            "LEFT JOIN screening_status s ON s.record_id = q.record_id "
            "WHERE q.resolved_at IS NULL "
            "ORDER BY CASE q.reason WHEN 'conflict' THEN 0 WHEN 'unclear' THEN 1 "
            "                       WHEN 'qc_rescue' THEN 2 ELSE 3 END, q.id "
            "LIMIT ?",
            (limit,),
        )
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["authors"] = json.loads(item.get("authors") or "[]")
            except ValueError:
                item["authors"] = []
            item["decisions"] = self.decisions_for(item["record_id"])
            out.append(item)
        return out

    def decisions_for(self, record_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT reviewer_role, reviewer, decision, exclusion_code, reason, "
                "       quote, quote_verified, created_at "
                "FROM screening_decision WHERE record_id = ? "
                "ORDER BY CASE reviewer_role WHEN 'pilot_human' THEN 0 "
                "  WHEN 'screener_a' THEN 1 WHEN 'screener_b' THEN 2 "
                "  WHEN 'adjudicator' THEN 3 WHEN 'qc' THEN 4 ELSE 5 END, id",
                (record_id,),
            )
        ]

    def resolve(
        self,
        record_id: int,
        decision: str,
        *,
        exclusion_code: str = "",
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        """A human settles a record. This always wins (integrity rule 3)."""
        if decision not in CATEGORIES:
            raise ScreeningError(f"{decision!r} is not one of {', '.join(CATEGORIES)}")
        if decision == "exclude" and not exclusion_code:
            raise ScreeningError(
                "An exclusion needs a code, so the PRISMA diagram can report "
                "why the record was excluded."
            )
        actor = actor or self.audit.actor
        previous = self.db.query_one(
            "SELECT decision, exclusion_code, source FROM screening_status "
            "WHERE record_id = ?",
            (record_id,),
        )
        answer = ScreeningAnswer(
            decision=decision,
            exclusion_code=exclusion_code if decision == "exclude" else "",
            reason=reason or "Decided by the reviewer.",
        )
        with self.db.transaction():
            self._store_decision(None, record_id, "human", actor, answer, None)
            self._set_status(record_id, decision, answer.exclusion_code, "human", actor)
            self.db.execute(
                "UPDATE review_queue SET resolved_at = ?, resolution = ? "
                "WHERE record_id = ? AND resolved_at IS NULL",
                (utc_now(), decision, record_id),
            )
            if previous is not None and previous["decision"] != decision:
                self.audit.log_override(
                    "screen", "record", record_id, "screening_decision",
                    {"decision": previous["decision"],
                     "exclusion_code": previous["exclusion_code"],
                     "source": previous["source"]},
                    {"decision": decision, "exclusion_code": answer.exclusion_code},
                    reason=reason, actor=actor,
                )
            else:
                self.audit.log(
                    "screen", "human_review", entity="record", entity_id=record_id,
                    detail={"decision": decision, "exclusion_code": answer.exclusion_code,
                            "reason": reason, "confirmed_ai": previous is not None},
                    actor=actor,
                )

    # -- reporting ---------------------------------------------------------

    def agreement_reports(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query("SELECT * FROM agreement_report ORDER BY id")
        ]

    def latest_agreement(self, scope: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM agreement_report WHERE scope = ? ORDER BY id DESC LIMIT 1",
            (scope,),
        )
        return dict(row) if row else None

    def runs(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM screening_run ORDER BY id")]

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        return counts | {
            "pilot_done": self.pilot_done(),
            "pilot_waived": self.pilot_waived(),
            "n_pilot_labels": len(self.pilot_labels()),
            "protocol_locked": self.project.protocol.is_locked(),
            "agreement_full": self.latest_agreement("full:a_vs_b"),
            "agreement_pilot": self.latest_agreement("pilot:human_vs_ai"),
            "unverified_quotes": int(
                self.db.scalar(
                    "SELECT COUNT(*) FROM screening_decision "
                    "WHERE quote <> '' AND quote_verified = 0"
                ) or 0
            ),
        }
