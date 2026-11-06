"""The synthesis engine: from author labels to a graded inventory.

Seven steps, in a fixed order, each of which the human can stop at:

1. **Codebook.** The model proposes constructs from the objectives and the
   extracted labels. Nothing may be mapped until a person approves it.
2. **Mapping.** Each label is placed against a construct by what was
   measured. Doubtful placements are flagged and queued.
3. **Orientation.** The person fixes which pole of each construct counts as
   "more". This is never the model's decision: it determines the sign of
   every finding downstream.
4. **Harmonisation.** Every relationship is re-expressed against those
   orientations, and each sign flip is recorded with its reason.
5. **Linkage.** Reports that share a body of fieldwork are detected and,
   once the person confirms, collapsed into one dataset that votes once.
6. **Grading.** Votes are counted at dataset level and graded.
7. **Sensitivity and contradictions.** The same counting under different
   rules, and an explanation for each contested link.

The order is enforced, not merely documented: each step refuses to run until
the one before it has been done. The reason is that every step after the
codebook inherits its decisions, and a codebook changed after mapping would
leave mappings pointing at constructs that no longer mean what they meant.

This module does the orchestration and the database work. The counting rules
live in `grading.py` and the statistics in `stats.py`, both free of any
database, which is what lets the sensitivity analyses re-run the same rules
over a filtered set of votes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Iterable, Sequence

from ..ai.client import AIClient, BudgetExceeded, CostEstimate
from ..core.db import utc_now
from ..importers.normalise import normalise_title
from .grading import GradedLink, GradingRules, Vote, grade_link
from .linkage import score_shared_dataset
from .prompts import (
    PROMPT_VERSION,
    codebook_system,
    codebook_user,
    contradiction_system,
    contradiction_user,
    linkage_system,
    linkage_user,
    mapping_system,
    mapping_user,
)
from .schemas import (
    RESIDUAL_CONSTRUCT,
    CodebookProposal,
    ContradictionExplanation,
    LinkageJudgement,
    MappingAnswer,
)

__all__ = [
    "SynthesisEngine",
    "SynthesisError",
    "SynthesisSettings",
    "StepOutcome",
]

ProgressFn = Callable[[int, int, str], None]
StopFn = Callable[[], bool]

#: Designs that describe other people's data rather than collecting any. They
#: are kept in the review and may be cited, but they do not get a vote: a
#: literature review agreeing with the studies it reviewed is not independent
#: evidence, it is the same evidence counted twice.
NON_VOTING_FAMILIES = ("review", "conceptual")


def _flip(direction: str) -> str:
    """The opposite sign. Anything that is not directional is unchanged."""
    return {"positive": "negative", "negative": "positive"}.get(direction, direction)


class SynthesisError(RuntimeError):
    pass


@dataclass
class SynthesisSettings:
    max_tokens: int = 2400
    budget_usd: float | None = None
    #: Labels sent in one mapping call. Batched because a review has hundreds
    #: of labels and one call each would be slow and expensive, but kept small
    #: enough that the model is not asked to hold too much at once.
    mapping_batch: int = 12
    #: Below this the model's own confidence makes a mapping contestable.
    contestable_below: float = 0.70
    #: Pairs scoring at least this on the offline signals are put to the model.
    linkage_threshold: float = 0.45
    #: And at least this to be proposed to the human at all.
    linkage_propose_above: float = 0.55
    grading: GradingRules = field(default_factory=GradingRules)

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "budget_usd": self.budget_usd,
            "mapping_batch": self.mapping_batch,
            "contestable_below": self.contestable_below,
            "linkage_threshold": self.linkage_threshold,
            "linkage_propose_above": self.linkage_propose_above,
            "grading": self.grading.as_dict(),
            "prompt_version": PROMPT_VERSION,
        }


@dataclass
class StepOutcome:
    """What one step of the synthesis did."""

    run_id: int
    step: str
    status: str = "finished"
    counts: dict[str, int] = field(default_factory=dict)
    n_calls: int = 0
    n_cached: int = 0
    cost_usd: float = 0.0
    messages: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "step": self.step,
            "status": self.status,
            "counts": dict(self.counts),
            "n_calls": self.n_calls,
            "n_cached": self.n_cached,
            "cost_usd": round(self.cost_usd, 6),
            "messages": list(self.messages),
        }

    #: How each step's counts read in a sentence, in the order they belong.
    #: Keyed by step as well as by count, because the same name means
    #: different things in different steps: `n_proposed` is constructs in the
    #: codebook and shared samples in the linkage, and a single table keyed on
    #: the count alone would misreport one of them.
    _PHRASES: ClassVar[dict[str, dict[str, str]]] = {
        "codebook": {
            "n_labels": "{} label(s) read",
            "n_proposed": "{} construct(s) proposed",
        },
        "mapping": {
            "n_labels": "{} label(s)",
            "n_mapped": "{} mapped",
            "n_residual": "{} to the residual code",
            "n_contestable": "{} flagged as contestable",
            "n_unanswered": "{} the model did not answer for",
        },
        "harmonise": {
            "n_counted": "{} relationship(s) will be counted",
            "n_flipped": "{} sign(s) flipped",
            "n_non_voting": "{} from reviews or conceptual papers",
            "n_no_direction": "{} with no usable direction",
            "n_unmapped": "{} whose labels are unmapped",
        },
        "linkage": {
            "n_studies": "{} report(s) compared",
            "n_candidates": "{} candidate pair(s)",
            "n_proposed": "{} shared sample(s) proposed",
        },
        "datasets": {
            "n_reports": "{} report(s)",
            "n_datasets": "{} dataset(s)",
            "n_shared": "{} reported more than once",
        },
        "sensitivity": {
            "n_analyses": "{} analysis(es)",
            "n_changed": "{} link-and-analysis combination(s) changed",
        },
        "contradiction": {
            "n_contested": "{} contested link(s)",
            "n_explained": "{} explained",
        },
    }

    #: Grading names a count per grade, so its phrases are built rather than
    #: listed: `n_links` plus one `n_<grade>` for each grade awarded.
    def _parts(self) -> list[str]:
        if self.step == "grade":
            parts = []
            if "n_links" in self.counts:
                parts.append(f"{self.counts['n_links']} graded link(s)")
            parts.extend(
                f"{value} {key[2:]}"
                for key, value in self.counts.items()
                if key != "n_links" and key.startswith("n_") and value
            )
            return parts
        phrases = self._PHRASES.get(self.step, {})
        return [
            phrase.format(self.counts[key])
            for key, phrase in phrases.items()
            if key in self.counts
        ]

    def describe(self) -> str:
        """One readable paragraph, for the command line and the status bar."""
        head = self.step.replace("_", " ").capitalize()
        if self.status != "finished":
            head += f" ({self.status})"
        parts = self._parts()
        lines = [f"{head}: " + (", ".join(parts) if parts else "nothing to do") + "."]
        if self.n_calls or self.cost_usd:
            cached = f", {self.n_cached} from cache" if self.n_cached else ""
            lines.append(
                f"  {self.n_calls} call(s){cached}, ${self.cost_usd:.4f}."
            )
        lines.extend(f"  {message}" for message in self.messages)
        return "\n".join(lines)


class SynthesisEngine:
    def __init__(
        self,
        project,
        client: AIClient,
        settings: SynthesisSettings | None = None,
    ) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.client = client
        self.settings = settings or SynthesisSettings()
        if self.settings.budget_usd is not None:
            self.client.set_budget(self.settings.budget_usd)
        #: Datasets whose own reports disagreed about a link, filled in by
        #: `collect_votes` and read back when the votes are stored.
        self._conflicts: list[tuple[tuple[int, int], int]] = []

    # -- shared ------------------------------------------------------------

    def counts(self) -> dict[str, int]:
        row = self.db.query_one("SELECT * FROM v_synthesis_progress")
        if row is None:
            return {}
        counts = dict(row)
        # The view counts labels as they were typed, because SQL cannot apply
        # the same folding `labels()` does. Taking the figure from `labels()`
        # keeps the number on screen equal to the list underneath it.
        counts["n_labels"] = len(self.labels())
        return counts

    def _protocol(self) -> tuple[str, int | None]:
        current = self.project.protocol.current()
        if current is None:
            raise SynthesisError(
                "The protocol has not been locked. The synthesis decides what "
                "the review concludes, so it has to rest on a fixed protocol."
            )
        return current.content, current.id

    def _start_run(self, kind: str) -> tuple[int, tuple[int, int, float]]:
        _, protocol_id = self._protocol()
        baseline = (self.client.n_calls, self.client.n_cached, self.client.spent_usd)
        run_id = self.db.insert(
            "synthesis_run",
            {
                "kind": kind,
                "status": "running",
                "started_at": utc_now(),
                "provider": self.client.provider.name,
                "model": self.client.provider.model,
                "temperature": self.client.temperature,
                "prompt_version": PROMPT_VERSION,
                "protocol_version": protocol_id,
                "settings": json.dumps(self.settings.as_dict()),
            },
        )
        return run_id, baseline

    def _finish_run(
        self, outcome: StepOutcome, baseline: tuple[int, int, float], actor: str
    ) -> StepOutcome:
        outcome.n_calls = self.client.n_calls - baseline[0]
        outcome.n_cached = self.client.n_cached - baseline[1]
        outcome.cost_usd = round(self.client.spent_usd - baseline[2], 6)
        self.db.execute(
            "UPDATE synthesis_run SET status = ?, finished_at = ?, n_calls = ?, "
            "n_cached = ?, cost_usd = ?, summary = ? WHERE id = ?",
            (
                outcome.status,
                utc_now() if outcome.status != "paused" else None,
                outcome.n_calls,
                outcome.n_cached,
                outcome.cost_usd,
                json.dumps(outcome.counts),
                outcome.run_id,
            ),
        )
        self.audit.log(
            "synthesise",
            f"synthesis:{outcome.step}",
            entity="synthesis_run",
            entity_id=outcome.run_id,
            detail=outcome.as_dict(),
            actor=actor,
        )
        return outcome

    # =====================================================================
    # 1. the codebook
    # =====================================================================

    def labels(self) -> list[dict[str, Any]]:
        """Every distinct label extraction recorded, commonest first.

        Distinct by what was measured, not by how it was typed. One author
        writes "Community Trust" and another "community trust"; they are one
        label, and the mapping table says so by allowing a single row per
        normalised label and role. Folding here rather than in the view keeps
        the folding rule identical to the one the unique index is built on --
        `normalise_title` strips accents and punctuation, which SQL cannot
        reproduce without a custom function that would make the project file
        unreadable outside this application.

        The surface form kept is the commonest one, which is what a reviewer
        should see on screen; `v_label_inventory` still holds every spelling
        for anyone who needs them.
        """
        folded: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.db.query(
            "SELECT label, role, n_uses, first_record_id FROM v_label_inventory "
            "ORDER BY n_uses DESC, label"
        ):
            entry = dict(row)
            key = (normalise_title(entry["label"]), entry["role"])
            kept = folded.get(key)
            if kept is None:
                entry["spellings"] = 1
                folded[key] = entry
                continue
            # Rows arrive commonest first, so the one already kept is the
            # spelling to show.
            kept["n_uses"] += entry["n_uses"]
            kept["spellings"] += 1
            kept["first_record_id"] = min(
                kept["first_record_id"], entry["first_record_id"]
            )
        return sorted(
            folded.values(), key=lambda r: (-r["n_uses"], r["label"])
        )

    def constructs(self, *, status: str | None = "approved") -> list[dict[str, Any]]:
        sql = "SELECT * FROM construct"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY position, id"
        return [dict(r) for r in self.db.query(sql, params)]

    def propose_codebook(self, *, actor: str | None = None) -> StepOutcome:
        """Ask for a codebook. Proposed only -- nothing is approved by this.

        What the model is asked to do depends on how the reviewer said the
        framework would be built, which is recorded in the protocol:

        *bottom_up* -- derive the constructs from the extracted labels. The
        model proposes the taxonomy.

        *top_down* -- the constructs are the reviewer's, declared in advance
        and locked into the protocol. The model writes their definitions and
        boundaries against the label inventory, and may not add to the list
        or take anything off it.
        """
        actor = actor or self.audit.actor
        labels = self.labels()
        if not labels:
            raise SynthesisError(
                "There are no extracted labels to build a codebook from. Run "
                "the extraction stage first."
            )
        info = self.project.info()
        declared = self._a_priori_constructs(info)
        protocol_text, _ = self._protocol()
        run_id, baseline = self._start_run("codebook")
        outcome = StepOutcome(run_id=run_id, step="codebook")

        result = self.client.ask(
            "synthesise:codebook",
            system=codebook_system(
                protocol_text,
                info.get("objectives", ""),
                a_priori=declared or None,
            ),
            user=codebook_user(labels, a_priori=bool(declared)),
            schema=CodebookProposal,
            entity="synthesis_run",
            entity_id=run_id,
            max_tokens=self.settings.max_tokens,
        )
        proposal: CodebookProposal = result.value

        if declared:
            counts, messages = self._write_a_priori_codebook(
                declared, proposal, run_id, result.ai_call_id
            )
        else:
            counts, messages = self._write_proposed_codebook(
                proposal, run_id, result.ai_call_id
            )

        counts["n_labels"] = len(labels)
        counts["n_returned"] = len(proposal.constructs)
        outcome.counts = counts
        outcome.messages.extend(messages)
        outcome.messages.append(
            "Read and edit the constructs, then approve the codebook; "
            "nothing can be mapped until you do."
        )
        return self._finish_run(outcome, baseline, actor)

    # -- the two kinds of codebook -----------------------------------------

    def _a_priori_constructs(self, info: dict[str, Any]) -> list[tuple[str, str]]:
        """The reviewer's declared constructs, if this is a top-down review.

        Empty for a bottom-up review, which is what makes the caller take
        the inductive path. Anything left in the field by a reviewer who
        changed their mind about the approach is ignored, not quietly used.
        """
        from ..core.protocol import (
            framework_development_section,
            parse_a_priori_constructs,
        )

        if (info.get("framework_approach") or "bottom_up") != "top_down":
            return []
        declared = parse_a_priori_constructs(info.get("a_priori_constructs", ""))
        if not declared:
            raise SynthesisError(
                "This review is set to build its framework top-down, and no "
                "constructs have been declared. A deductive codebook is the "
                "constructs the reviewer specified in advance; there is "
                "nothing here to code into. Enter them on the Project page, "
                "or change the approach to bottom-up to derive them from "
                "the extracted labels instead."
            )

        # The constructs have to be the ones the locked protocol commits to.
        # Otherwise a reviewer could read the evidence, retype the list, and
        # get a codebook the protocol never mentioned -- while the Methods
        # section went on calling it a priori.
        current = self.project.protocol.current()
        expected = framework_development_section(info)
        if current is not None and expected not in current.content:
            raise SynthesisError(
                "The declared constructs are not the ones in the locked "
                f"protocol (version {current.version}). A top-down codebook "
                "is only a priori if the constructs were fixed before the "
                "evidence was coded, so this step will not use a list that "
                "the protocol does not contain. Lock the change as an "
                "amendment on the Project page -- it will be recorded with "
                "your reason and reported in the Methods section -- or put "
                "the constructs back to the locked wording."
            )
        return declared

    def _write_proposed_codebook(
        self,
        proposal: CodebookProposal,
        run_id: int,
        ai_call_id: int | None,
    ) -> tuple[dict[str, int], list[str]]:
        """Bottom-up: the constructs the model proposed become the codebook."""
        with self.db.transaction():
            # A fresh proposal replaces any earlier one that nobody approved;
            # approved constructs are left exactly as they are.
            self.db.execute("DELETE FROM construct WHERE status = 'proposed'")
            n_new = 0
            for position, construct in enumerate(proposal.constructs):
                name_norm = normalise_title(construct.name)
                existing = self.db.query_one(
                    "SELECT id, status FROM construct WHERE name_norm = ?",
                    (name_norm,),
                )
                if existing:
                    continue
                self.db.insert(
                    "construct",
                    {
                        "name": construct.name,
                        "name_norm": name_norm,
                        "definition": construct.definition,
                        "includes": json.dumps(construct.includes),
                        "excludes": json.dumps(construct.excludes),
                        "status": "proposed",
                        "source": "ai",
                        "position": position,
                        "run_id": run_id,
                        "ai_call_id": ai_call_id,
                        "created_at": utc_now(),
                    },
                )
                n_new += 1
            self._ensure_residual()
        return (
            {"n_proposed": n_new},
            [f"{n_new} construct(s) proposed from the extracted labels."],
        )

    def _write_a_priori_codebook(
        self,
        declared: list[tuple[str, str]],
        proposal: CodebookProposal,
        run_id: int,
        ai_call_id: int | None,
    ) -> tuple[dict[str, int], list[str]]:
        """Top-down: the reviewer's list is the codebook, whatever came back.

        The model's answer is used for the definitions and boundaries only.
        A construct it invented is dropped and reported; a construct it
        skipped is still created, because the reviewer declared it and a
        model's silence must not be able to narrow a protocol.
        """
        returned = {
            construct.name.casefold(): construct for construct in proposal.constructs
        }
        declared_keys = {name.casefold() for name, _ in declared}
        discarded = [
            construct.name
            for key, construct in returned.items()
            if key not in declared_keys
        ]

        written = 0
        undefined: list[str] = []
        with self.db.transaction():
            self.db.execute("DELETE FROM construct WHERE status = 'proposed'")
            for position, (name, reviewer_definition) in enumerate(declared):
                name_norm = normalise_title(name)
                if self.db.query_one(
                    "SELECT id FROM construct WHERE name_norm = ?", (name_norm,)
                ):
                    continue
                answer = returned.get(name.casefold())
                # The reviewer's own definition wins where they wrote one:
                # they are the one committing to it. The model's wording is
                # kept in the note so nothing is lost and both are visible
                # to whoever approves the codebook.
                definition = reviewer_definition or (
                    answer.definition if answer else ""
                )
                note = ""
                if answer and reviewer_definition:
                    note = f"Model's proposed wording: {answer.definition}"
                elif not answer:
                    note = (
                        "The model did not return this construct. It is here "
                        "because the protocol declares it; its definition and "
                        "boundaries need writing by hand."
                    )
                    undefined.append(name)
                self.db.insert(
                    "construct",
                    {
                        "name": name,
                        "name_norm": name_norm,
                        "definition": definition,
                        "includes": json.dumps(answer.includes if answer else []),
                        "excludes": json.dumps(answer.excludes if answer else []),
                        "status": "proposed",
                        # The construct is the reviewer's; only its wording
                        # came from the model, which `ai_call_id` records.
                        "source": "human",
                        "note": note,
                        "position": position,
                        "run_id": run_id,
                        "ai_call_id": ai_call_id,
                        "created_at": utc_now(),
                    },
                )
                written += 1
            self._ensure_residual()

        messages = [
            f"{written} construct(s) from the protocol's declared list, with "
            "definitions written against the extracted labels."
        ]
        if discarded:
            messages.append(
                f"{len(discarded)} construct(s) the model added were "
                "discarded: this review's codebook is closed by its "
                "protocol. Discarded: " + ", ".join(sorted(discarded)) + "."
            )
        if undefined:
            messages.append(
                "The model returned no definition for "
                + ", ".join(sorted(undefined))
                + ". They are in the codebook because the protocol declares "
                "them, and need a definition written by hand before approval."
            )
        return (
            {
                "n_proposed": written,
                "n_declared": len(declared),
                "n_discarded": len(discarded),
                "n_undefined": len(undefined),
            },
            messages,
        )

    def _ensure_residual(self) -> int:
        """The 'other' code, always present and always approved.

        It exists so that a label which fits nothing has somewhere honest to
        go. Forcing such a label into the nearest construct is how a review
        ends up counting two different things as one.
        """
        row = self.db.query_one(
            "SELECT id FROM construct WHERE is_residual = 1 LIMIT 1"
        )
        if row:
            return row["id"]
        return self.db.insert(
            "construct",
            {
                "name": RESIDUAL_CONSTRUCT,
                "name_norm": normalise_title(RESIDUAL_CONSTRUCT),
                "definition": (
                    "Residual code: a label that does not fit any construct in "
                    "the codebook. Labels here are not counted in any link."
                ),
                "includes": json.dumps(["anything the codebook does not cover"]),
                "excludes": json.dumps(["anything a construct does cover"]),
                "is_residual": 1,
                "status": "approved",
                "source": "human",
                "position": 999,
                "created_at": utc_now(),
            },
        )

    def add_construct(
        self,
        name: str,
        *,
        definition: str = "",
        includes: Sequence[str] = (),
        excludes: Sequence[str] = (),
        actor: str | None = None,
    ) -> int:
        """A construct the person adds themselves."""
        actor = actor or self.audit.actor
        name_norm = normalise_title(name)
        if not name_norm:
            raise SynthesisError("A construct needs a name.")
        if self.db.query_one("SELECT 1 FROM construct WHERE name_norm = ?", (name_norm,)):
            raise SynthesisError(f"There is already a construct called {name!r}.")
        construct_id = self.db.insert(
            "construct",
            {
                "name": name.strip(),
                "name_norm": name_norm,
                "definition": definition,
                "includes": json.dumps(list(includes)),
                "excludes": json.dumps(list(excludes)),
                "status": "proposed",
                "source": "human",
                "created_at": utc_now(),
            },
        )
        self.audit.log(
            "synthesise", "construct_added", entity="construct",
            entity_id=construct_id, detail={"name": name}, actor=actor,
        )
        return construct_id

    def edit_construct(
        self, construct_id: int, *, actor: str | None = None, **fields: Any
    ) -> None:
        """Change a construct, recording what the model had said."""
        actor = actor or self.audit.actor
        before = self.db.query_one(
            "SELECT * FROM construct WHERE id = ?", (construct_id,)
        )
        if before is None:
            raise SynthesisError(f"No construct {construct_id}.")
        allowed = {"name", "definition", "includes", "excludes", "note"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        if "includes" in updates and not isinstance(updates["includes"], str):
            updates["includes"] = json.dumps(list(updates["includes"]))
        if "excludes" in updates and not isinstance(updates["excludes"], str):
            updates["excludes"] = json.dumps(list(updates["excludes"]))
        if "name" in updates:
            updates["name_norm"] = normalise_title(updates["name"])
        assignments = ", ".join(f"{k} = ?" for k in updates)
        self.db.execute(
            f"UPDATE construct SET {assignments} WHERE id = ?",
            (*updates.values(), construct_id),
        )
        for field_name, value in updates.items():
            if field_name == "name_norm" or before[field_name] == value:
                continue
            self.audit.log_override(
                "synthesise", "construct", construct_id, field_name,
                before[field_name], value, actor=actor,
                reason="codebook edited before approval",
            )

    def reject_construct(
        self, construct_id: int, *, reason: str = "", actor: str | None = None
    ) -> None:
        actor = actor or self.audit.actor
        self.db.execute(
            "UPDATE construct SET status = 'rejected', note = ? WHERE id = ?",
            (reason, construct_id),
        )
        self.audit.log(
            "synthesise", "construct_rejected", entity="construct",
            entity_id=construct_id, detail={"reason": reason}, actor=actor,
        )

    def codebook_hash(self) -> str:
        """A fingerprint of exactly what is being approved.

        Stored with the approval so that a codebook edited afterwards can be
        detected: the mapping was done against a particular set of
        definitions, and if those change the mapping has to be revisited.
        """
        rows = self.db.query(
            "SELECT name, definition, includes, excludes FROM construct "
            "WHERE status IN ('proposed','approved') ORDER BY name_norm"
        )
        digest = hashlib.sha256()
        for row in rows:
            digest.update(
                f"{row['name']}|{row['definition']}|{row['includes']}|"
                f"{row['excludes']}\n".encode()
            )
        return digest.hexdigest()

    def approve_codebook(
        self, *, actor: str | None = None, note: str = ""
    ) -> int:
        """Sign off the codebook. Nothing may be mapped before this."""
        actor = actor or self.audit.actor
        pending = self.db.query(
            "SELECT id FROM construct WHERE status = 'proposed'"
        )
        if not pending and not self.db.query_one(
            "SELECT 1 FROM construct WHERE status = 'approved' AND is_residual = 0"
        ):
            raise SynthesisError(
                "There is nothing to approve. Propose a codebook, or add "
                "constructs yourself, first."
            )
        now = utc_now()
        with self.db.transaction():
            self.db.execute(
                "UPDATE codebook_approval SET superseded_at = ? "
                "WHERE superseded_at IS NULL",
                (now,),
            )
            self.db.execute(
                "UPDATE construct SET status = 'approved', approved_by = ?, "
                "approved_at = ? WHERE status = 'proposed'",
                (actor, now),
            )
            self._ensure_residual()
            n = self.db.scalar(
                "SELECT COUNT(*) FROM construct WHERE status = 'approved'"
            )
            approval_id = self.db.insert(
                "codebook_approval",
                {
                    "n_constructs": n,
                    "construct_hash": self.codebook_hash(),
                    "approved_by": actor,
                    "approved_at": now,
                    "note": note,
                },
            )
        self.audit.log(
            "synthesise", "codebook_approved", entity="codebook_approval",
            entity_id=approval_id, detail={"n_constructs": n, "note": note},
            actor=actor,
        )
        return approval_id

    def codebook_approved(self) -> bool:
        return bool(
            self.db.query_one(
                "SELECT 1 FROM codebook_approval WHERE superseded_at IS NULL"
            )
        )

    def codebook_changed_since_approval(self) -> bool:
        """Whether the codebook has been edited since it was signed off."""
        row = self.db.query_one(
            "SELECT construct_hash FROM codebook_approval "
            "WHERE superseded_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if row is None:
            return False
        return row["construct_hash"] != self.codebook_hash()

    def _require_codebook(self) -> list[dict[str, Any]]:
        if not self.codebook_approved():
            raise SynthesisError(
                "The codebook has not been approved. Mapping labels onto "
                "constructs nobody has read would make the model's vocabulary "
                "the review's, which is the one thing this stage exists to "
                "prevent."
            )
        constructs = self.constructs(status="approved")
        if not [c for c in constructs if not c["is_residual"]]:
            raise SynthesisError(
                "The approved codebook contains only the residual code."
            )
        return constructs

    # =====================================================================
    # 2. mapping labels onto constructs
    # =====================================================================

    def map_labels(
        self,
        *,
        actor: str | None = None,
        on_progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
    ) -> StepOutcome:
        """Place every unmapped label against an approved construct."""
        actor = actor or self.audit.actor
        constructs = self._require_codebook()
        by_name = {normalise_title(c["name"]): c for c in constructs}
        residual_id = self._ensure_residual()

        pending = self._pending_labels()
        run_id, baseline = self._start_run("mapping")
        outcome = StepOutcome(run_id=run_id, step="mapping")
        if not pending:
            outcome.messages.append("Every label is already mapped.")
            return self._finish_run(outcome, baseline, actor)

        system = mapping_system(constructs)
        n_mapped = n_contestable = n_residual = n_missing = 0
        batches = [
            pending[i : i + self.settings.mapping_batch]
            for i in range(0, len(pending), self.settings.mapping_batch)
        ]
        for index, batch in enumerate(batches, start=1):
            if should_stop is not None and should_stop():
                outcome.status = "paused"
                outcome.messages.append(
                    f"Paused after {index - 1} of {len(batches)} batches. "
                    "Labels already mapped will not be asked again."
                )
                break
            try:
                result = self.client.ask(
                    "synthesise:map",
                    system=system,
                    user=mapping_user(batch),
                    schema=MappingAnswer,
                    entity="synthesis_run",
                    entity_id=run_id,
                    max_tokens=self.settings.max_tokens,
                )
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break

            answered = {
                normalise_title(m.label): m for m in result.value.mappings
            }
            with self.db.transaction():
                for row in batch:
                    mapping = answered.get(normalise_title(row["label"]))
                    if mapping is None:
                        # Asked about and not answered. Recorded as unmapped
                        # rather than dropped, and queued, because a label
                        # nobody placed is a hole in the count.
                        n_missing += 1
                        self._store_mapping(
                            row, None, run_id, result.ai_call_id,
                            measured_as="", rationale="",
                            confidence=None, contestable=True,
                            contest_reason="the model did not answer for this label",
                        )
                        self._queue_label(row, "unmapped_label", actor)
                        continue

                    target = by_name.get(normalise_title(mapping.construct_name))
                    contest_reason = mapping.contest_reason
                    contestable = mapping.contestable
                    if target is None:
                        # A construct that is not in the approved codebook.
                        # Sent to the residual code and flagged, never
                        # created: the approval stage exists to stop that.
                        target = {"id": residual_id, "name": RESIDUAL_CONSTRUCT}
                        contestable = True
                        contest_reason = (
                            f"the model answered {mapping.construct_name!r}, which "
                            "is not in the approved codebook"
                        )
                    elif target["is_residual"]:
                        n_residual += 1
                    if (
                        mapping.confidence is not None
                        and mapping.confidence < self.settings.contestable_below
                        and not contestable
                    ):
                        contestable = True
                        contest_reason = (
                            f"the model's own confidence was "
                            f"{mapping.confidence:.0%}"
                        )
                    self._store_mapping(
                        row, target["id"], run_id, result.ai_call_id,
                        measured_as=mapping.measured_as,
                        rationale=mapping.rationale,
                        confidence=mapping.confidence,
                        contestable=contestable,
                        contest_reason=contest_reason,
                    )
                    n_mapped += 1
                    if contestable:
                        n_contestable += 1
                        self._queue_label(row, "contestable_mapping", actor)
            if on_progress:
                on_progress(index, len(batches), f"Mapped batch {index} of {len(batches)}")

        outcome.counts = {
            "n_labels": len(pending),
            "n_mapped": n_mapped,
            "n_contestable": n_contestable,
            "n_residual": n_residual,
            "n_unanswered": n_missing,
        }
        if n_contestable:
            outcome.messages.append(
                f"{n_contestable} mapping(s) are flagged for you to confirm. "
                "They are counted as mapped meanwhile; confirming or changing "
                "one is logged as an override."
            )
        if n_residual:
            outcome.messages.append(
                f"{n_residual} label(s) fit no construct and went to "
                f"{RESIDUAL_CONSTRUCT!r}. They take part in no link."
            )
        return self._finish_run(outcome, baseline, actor)

    def _pending_labels(self) -> list[dict[str, Any]]:
        mapped = {
            (r["label_norm"], r["role"])
            for r in self.db.query("SELECT label_norm, role FROM label_mapping")
        }
        pending = []
        for row in self.labels():
            if (normalise_title(row["label"]), row["role"]) in mapped:
                continue
            pending.append(row)
        return pending

    def _store_mapping(
        self,
        row: dict[str, Any],
        construct_id: int | None,
        run_id: int,
        ai_call_id: int | None,
        *,
        measured_as: str,
        rationale: str,
        confidence: float | None,
        contestable: bool,
        contest_reason: str,
    ) -> int:
        return self.db.insert(
            "label_mapping",
            {
                "label": row["label"],
                "label_norm": normalise_title(row["label"]),
                "role": row["role"],
                "construct_id": construct_id,
                "measured_as": measured_as,
                "rationale": rationale,
                "confidence": confidence,
                "contestable": int(contestable),
                "contest_reason": contest_reason,
                "n_uses": row.get("n_uses", 0),
                "run_id": run_id,
                "ai_call_id": ai_call_id,
                "created_at": utc_now(),
            },
        )

    def _queue_label(self, row: dict[str, Any], reason: str, actor: str) -> None:
        record_id = row.get("first_record_id")
        if not record_id:
            return
        self.db.execute(
            "INSERT OR IGNORE INTO review_queue"
            "(record_id, reason, detail, stage, created_at) "
            "VALUES(?, ?, ?, 'synthesise', ?)",
            (record_id, reason, f"{row['role']}: {row['label']}", utc_now()),
        )

    def contestable_mappings(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT m.*, c.name AS construct_name FROM label_mapping m "
                "LEFT JOIN construct c ON c.id = m.construct_id "
                "WHERE m.contestable = 1 AND m.status = 'proposed' "
                "ORDER BY m.n_uses DESC, m.label"
            )
        ]

    def confirm_mapping(
        self,
        mapping_id: int,
        *,
        construct_id: int | None = None,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        """Accept a mapping, or move the label to a different construct.

        Either way the mapping stops being contestable; moving it is recorded
        as an override against the construct the model chose.
        """
        actor = actor or self.audit.actor
        before = self.db.query_one(
            "SELECT * FROM label_mapping WHERE id = ?", (mapping_id,)
        )
        if before is None:
            raise SynthesisError(f"No mapping {mapping_id}.")
        now = utc_now()
        if construct_id is not None and construct_id != before["construct_id"]:
            if not self.db.query_one(
                "SELECT 1 FROM construct WHERE id = ? AND status = 'approved'",
                (construct_id,),
            ):
                raise SynthesisError(
                    f"Construct {construct_id} is not in the approved codebook."
                )
            self.db.execute(
                "UPDATE label_mapping SET construct_id = ?, status = 'overridden', "
                "overridden_from = ?, override_reason = ?, decided_by = ?, "
                "decided_at = ?, contestable = 0 WHERE id = ?",
                (construct_id, before["construct_id"], reason, actor, now, mapping_id),
            )
            self.audit.log_override(
                "synthesise", "label_mapping", mapping_id, "construct_id",
                before["construct_id"], construct_id, reason=reason, actor=actor,
            )
        else:
            self.db.execute(
                "UPDATE label_mapping SET status = 'accepted', decided_by = ?, "
                "decided_at = ?, contestable = 0 WHERE id = ?",
                (actor, now, mapping_id),
            )
            self.audit.log(
                "synthesise", "mapping_confirmed", entity="label_mapping",
                entity_id=mapping_id, detail={"label": before["label"]}, actor=actor,
            )
        self.db.execute(
            "UPDATE review_queue SET resolved_at = ?, resolution = ? "
            "WHERE stage = 'synthesise' AND resolved_at IS NULL "
            "AND reason IN ('contestable_mapping','unmapped_label') AND detail = ?",
            (now, "settled", f"{before['role']}: {before['label']}"),
        )

    # =====================================================================
    # 3. orientation, and 4. harmonisation
    # =====================================================================

    def set_orientation(
        self, construct_id: int, orientation: str, *, actor: str | None = None
    ) -> None:
        """Fix which pole of a construct counts as "more".

        Always the person's decision. It determines the sign of every finding
        that touches the construct, so a model choosing it would be choosing
        what the review concludes.
        """
        actor = actor or self.audit.actor
        text = " ".join(orientation.split())
        if not text:
            raise SynthesisError(
                "An orientation has to say which direction counts as more, "
                'for example "higher trust in local government".'
            )
        before = self.db.query_one(
            "SELECT orientation FROM construct WHERE id = ?", (construct_id,)
        )
        if before is None:
            raise SynthesisError(f"No construct {construct_id}.")
        self.db.execute(
            "UPDATE construct SET orientation = ?, orientation_set_by = ?, "
            "orientation_set_at = ? WHERE id = ?",
            (text, actor, utc_now(), construct_id),
        )
        self.audit.log(
            "synthesise", "orientation_set", entity="construct",
            entity_id=construct_id,
            detail={"from": before["orientation"], "to": text}, actor=actor,
        )

    def constructs_without_orientation(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM construct WHERE status = 'approved' "
                "AND is_residual = 0 AND orientation = '' ORDER BY position, id"
            )
        ]

    #: Words that mark a label as naming the *absence* or *reverse* of the
    #: thing its construct is oriented on. A relationship to "distrust" when
    #: the construct is oriented on "higher trust" runs the other way, and
    #: counting it unflipped would record two agreeing studies as a
    #: disagreement.
    _REVERSE_MARKERS = (
        "dis", "mis", "un", "non", "anti", "lack of", "absence of", "loss of",
        "low ", "lower ", "reduced ", "decreased ", "declining ", "negative ",
        "barrier", "obstacle", "constraint", "opposition", "resistance",
    )

    def harmonise(self, *, actor: str | None = None) -> StepOutcome:
        """Re-express every relationship against the reference orientations.

        A relationship is only counted when both of its ends map to a real
        construct with an orientation set. Everything else is stored with the
        reason it does not count, so the inventory can always account for a
        relationship that went missing between extraction and grading.
        """
        actor = actor or self.audit.actor
        self._require_codebook()
        missing = self.constructs_without_orientation()
        if missing:
            raise SynthesisError(
                "These constructs have no reference orientation yet: "
                + ", ".join(c["name"] for c in missing)
                + ". Until each one says which direction counts as more, a "
                "positive finding and a negative one cannot be compared."
            )

        run_id, baseline = self._start_run("harmonise")
        outcome = StepOutcome(run_id=run_id, step="harmonise")

        mappings = {
            (r["label_norm"], r["role"]): r
            for r in self.db.query(
                "SELECT m.*, c.name AS construct_name, c.orientation, "
                "c.is_residual FROM label_mapping m "
                "LEFT JOIN construct c ON c.id = m.construct_id"
            )
        }
        relationships = self.db.query(
            "SELECT rel.*, ex.design_family, ex.country, ex.appraisal_concern "
            "FROM relationship rel "
            "LEFT JOIN report_extraction ex "
            "  ON ex.record_id = rel.record_id AND ex.coder_role = rel.coder_role "
            "WHERE rel.coder_role = 'ai' ORDER BY rel.record_id, rel.position"
        )

        counts = {
            "n_relationships": len(relationships),
            "n_counted": 0,
            "n_flipped": 0,
            "n_unmapped": 0,
            "n_residual": 0,
            "n_non_voting": 0,
            "n_no_direction": 0,
        }
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM harmonised_relationship WHERE relationship_id IN "
                "(SELECT id FROM relationship WHERE coder_role = 'ai')"
            )
            for rel in relationships:
                self._harmonise_one(dict(rel), mappings, run_id, counts)

        outcome.counts = counts
        outcome.messages.append(
            f"{counts['n_counted']} of {len(relationships)} relationship(s) "
            f"will be counted; {counts['n_flipped']} had their sign flipped "
            "to match the reference orientations."
        )
        if counts["n_non_voting"]:
            outcome.messages.append(
                f"{counts['n_non_voting']} relationship(s) come from reviews "
                "or conceptual papers and do not vote."
            )
        if counts["n_unmapped"] or counts["n_residual"]:
            outcome.messages.append(
                f"{counts['n_unmapped'] + counts['n_residual']} relationship(s) "
                "have an end that maps to no construct, so they cannot be "
                "counted. They are listed with the reason."
            )
        return self._finish_run(outcome, baseline, actor)

    def _harmonise_one(
        self,
        rel: dict[str, Any],
        mappings: dict[tuple[str, str], Any],
        run_id: int,
        counts: dict[str, int],
    ) -> None:
        def construct_for(label: str, role: str) -> Any:
            if not label:
                return None
            return mappings.get((normalise_title(label), role))

        predictor = construct_for(rel["predictor_label"], "predictor")
        outcome_map = construct_for(rel["outcome_label"], "outcome")
        mediator = construct_for(rel["mediator_label"], "mediator")
        moderator = construct_for(rel["moderator_label"], "moderator")

        counted, excluded_reason = 1, ""
        if predictor is None or outcome_map is None:
            counted, excluded_reason = 0, "an end of this relationship is not mapped"
            counts["n_unmapped"] += 1
        elif predictor["is_residual"] or outcome_map["is_residual"]:
            counted, excluded_reason = 0, (
                f"an end maps to the residual code {RESIDUAL_CONSTRUCT!r}"
            )
            counts["n_residual"] += 1
        elif (rel.get("design_family") or "") in NON_VOTING_FAMILIES:
            counted, excluded_reason = 0, (
                f"the report is a {rel['design_family']}, which reports other "
                "studies' data rather than collecting its own"
            )
            counts["n_non_voting"] += 1
        elif rel["direction"] in ("unclear",):
            counted, excluded_reason = 0, "the report does not say which way it runs"
            counts["n_no_direction"] += 1

        direction = rel["direction"]
        flipped = 0
        flip_reason = ""
        if counted:
            flips = []
            for side, mapping in (("predictor", predictor), ("outcome", outcome_map)):
                label = rel[f"{side}_label"]
                if self._runs_against(label, mapping):
                    flips.append(
                        f"{side} {label!r} names the reverse of "
                        f"{mapping['construct_name']!r} "
                        f"(oriented on {mapping['orientation']!r})"
                    )
            # Two reversals cancel: lower distrust raising lower dissatisfaction
            # is the same finding as higher trust raising higher satisfaction.
            if len(flips) % 2 == 1:
                direction = _flip(direction)
                flipped = 1
                flip_reason = "; ".join(flips)
            elif flips:
                flip_reason = (
                    "both ends name reverses, which cancel: "
                    + "; ".join(flips)
                )
            counts["n_counted"] += 1
            counts["n_flipped"] += flipped

        self.db.insert(
            "harmonised_relationship",
            {
                "relationship_id": rel["id"],
                "record_id": rel["record_id"],
                "predictor_construct": predictor["construct_id"] if predictor else None,
                "outcome_construct": outcome_map["construct_id"] if outcome_map else None,
                "mediator_construct": mediator["construct_id"] if mediator else None,
                "moderator_construct": moderator["construct_id"] if moderator else None,
                "original_direction": rel["direction"],
                "direction": direction,
                "flipped": flipped,
                "flip_reason": flip_reason,
                "counts": counted,
                "excluded_reason": excluded_reason,
                "run_id": run_id,
                "created_at": utc_now(),
            },
        )

    @classmethod
    def _runs_against(cls, label: str, mapping: Any) -> bool:
        """Whether a label names the reverse of its construct's orientation.

        A deliberately cautious test. It fires only when the label carries a
        reversing marker that the orientation does not, so "distrust" against
        an orientation of "higher trust" flips, while "distrust" against an
        orientation of "higher distrust" does not. Every flip is recorded
        with this reasoning attached, so a reviewer can see and undo it.
        """
        orientation = (mapping["orientation"] or "").casefold()
        text = (label or "").casefold()
        construct = (mapping["construct_name"] or "").casefold()
        for marker in cls._REVERSE_MARKERS:
            if marker.endswith(" "):
                hit = marker in f"{text} "
                in_orientation = marker in f"{orientation} "
            else:
                hit = bool(re.search(rf"\b{re.escape(marker)}", text))
                in_orientation = bool(
                    re.search(rf"\b{re.escape(marker)}", orientation)
                )
            if hit and not in_orientation and marker not in construct:
                return True
        return False

    def sign_flips(self) -> list[dict[str, Any]]:
        """Every flip, for the reviewer to check and the Methods to report."""
        return [
            dict(r)
            for r in self.db.query(
                "SELECT h.*, r.predictor_label, r.outcome_label, rec.title "
                "FROM harmonised_relationship h "
                "JOIN relationship r ON r.id = h.relationship_id "
                "JOIN record rec ON rec.id = h.record_id "
                "WHERE h.flipped = 1 ORDER BY h.record_id"
            )
        ]

    def uncounted_relationships(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT h.*, r.predictor_label, r.outcome_label "
                "FROM harmonised_relationship h "
                "JOIN relationship r ON r.id = h.relationship_id "
                "WHERE h.counts = 0 ORDER BY h.record_id"
            )
        ]

    # =====================================================================
    # 5. dataset linkage
    # =====================================================================

    def study_descriptions(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT r.id AS record_id, r.title, r.authors, r.year, "
                "ex.country, ex.setting, ex.population, ex.design, "
                "ex.design_family, ex.sampling, ex.sample_size, "
                "ex.sample_size_text, ex.time_horizon, ex.appraisal_concern "
                "FROM record r "
                "JOIN report_extraction ex ON ex.record_id = r.id "
                "  AND ex.coder_role = 'ai' "
                "WHERE r.active = 1 ORDER BY r.id"
            )
        ]

    def find_dataset_links(self, *, actor: str | None = None) -> StepOutcome:
        """Look for reports that share a body of fieldwork.

        Two passes. The first is offline and cheap: it scores every pair on
        shared authors, country, setting, sample size and time horizon. Only
        pairs that clear a threshold are put to the model, because a review
        of 80 reports has 3,160 pairs and almost all of them are obviously
        unrelated.

        Nothing is merged here. Every candidate is stored as a proposal for
        the person to confirm or reject, because a wrong merge silently
        deletes a vote and a wrong split silently doubles one.
        """
        actor = actor or self.audit.actor
        studies = self.study_descriptions()
        run_id, baseline = self._start_run("linkage")
        outcome = StepOutcome(run_id=run_id, step="linkage")

        candidates: list[tuple[dict[str, Any], dict[str, Any], float, dict[str, Any]]] = []
        for i, a in enumerate(studies):
            for b in studies[i + 1 :]:
                score, signals = score_shared_dataset(a, b)
                if score >= self.settings.linkage_threshold:
                    candidates.append((a, b, score, signals))

        n_asked = n_proposed = 0
        for a, b, score, signals in candidates:
            judgement = None
            try:
                result = self.client.ask(
                    "synthesise:linkage",
                    system=linkage_system(),
                    user=linkage_user(a, b),
                    schema=LinkageJudgement,
                    entity="record",
                    entity_id=a["record_id"],
                    max_tokens=600,
                )
                judgement = result.value
                n_asked += 1
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break

            combined = (score + judgement.confidence) / 2 if judgement else score
            if judgement and judgement.same_dataset == "no":
                continue
            if combined < self.settings.linkage_propose_above:
                continue
            reason = judgement.reason if judgement else "matched on stored fields"
            if judgement:
                signals = {**signals, "model": judgement.shared_signals}
            self.db.execute(
                "INSERT OR IGNORE INTO dataset_link"
                "(record_a, record_b, score, signals, reason, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    min(a["record_id"], b["record_id"]),
                    max(a["record_id"], b["record_id"]),
                    round(combined, 4),
                    json.dumps(signals),
                    reason,
                    utc_now(),
                ),
            )
            n_proposed += 1

        outcome.counts = {
            "n_studies": len(studies),
            "n_pairs": len(studies) * (len(studies) - 1) // 2,
            "n_candidates": len(candidates),
            "n_asked": n_asked,
            "n_proposed": n_proposed,
        }
        outcome.messages.append(
            f"{n_proposed} possible shared dataset(s) found among "
            f"{len(studies)} reports. Confirm or reject each one; reports "
            "are only collapsed into a single vote once you confirm."
        )
        return self._finish_run(outcome, baseline, actor)

    def proposed_links(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT l.*, ra.title AS title_a, rb.title AS title_b "
                "FROM dataset_link l "
                "JOIN record ra ON ra.id = l.record_a "
                "JOIN record rb ON rb.id = l.record_b "
                "WHERE l.status = 'proposed' ORDER BY l.score DESC"
            )
        ]

    def decide_link(
        self,
        link_id: int,
        *,
        confirmed: bool,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        actor = actor or self.audit.actor
        row = self.db.query_one("SELECT * FROM dataset_link WHERE id = ?", (link_id,))
        if row is None:
            raise SynthesisError(f"No dataset link {link_id}.")
        self.db.execute(
            "UPDATE dataset_link SET status = ?, decided_by = ?, decided_at = ?, "
            "decision_reason = ? WHERE id = ?",
            (
                "confirmed" if confirmed else "rejected",
                actor,
                utc_now(),
                reason,
                link_id,
            ),
        )
        self.audit.log(
            "synthesise",
            "dataset_link_confirmed" if confirmed else "dataset_link_rejected",
            entity="dataset_link", entity_id=link_id,
            detail={
                "record_a": row["record_a"],
                "record_b": row["record_b"],
                "reason": reason,
            },
            actor=actor,
        )

    def build_datasets(self, *, actor: str | None = None) -> StepOutcome:
        """Group reports into datasets using the confirmed links only.

        A report with no confirmed link is its own dataset of one, which is
        the usual case. Confirmed links are transitive: if A shares a sample
        with B and B with C, all three are one dataset, because they are all
        the same fieldwork.
        """
        actor = actor or self.audit.actor
        run_id, baseline = self._start_run("linkage")
        outcome = StepOutcome(run_id=run_id, step="datasets")

        studies = {s["record_id"]: s for s in self.study_descriptions()}
        parent = {record_id: record_id for record_id in studies}

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        n_links = 0
        for row in self.db.query(
            "SELECT record_a, record_b FROM dataset_link WHERE status = 'confirmed'"
        ):
            a, b = row["record_a"], row["record_b"]
            if a not in parent or b not in parent:
                continue
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
                n_links += 1

        groups: dict[int, list[int]] = {}
        for record_id in studies:
            groups.setdefault(find(record_id), []).append(record_id)

        with self.db.transaction():
            self.db.execute("DELETE FROM dataset_member")
            self.db.execute("DELETE FROM dataset")
            for root, members in sorted(groups.items()):
                members.sort()
                primary = studies[members[0]]
                dataset_id = self.db.insert(
                    "dataset",
                    {
                        "label": (primary["title"] or f"record {members[0]}")[:120],
                        "country": primary.get("country") or "",
                        "note": (
                            f"{len(members)} reports share this dataset"
                            if len(members) > 1
                            else ""
                        ),
                        "created_at": utc_now(),
                    },
                )
                for record_id in members:
                    self.db.insert(
                        "dataset_member",
                        {
                            "dataset_id": dataset_id,
                            "record_id": record_id,
                            "is_primary": int(record_id == members[0]),
                            "added_at": utc_now(),
                        },
                    )
            self.db.execute(
                "UPDATE harmonised_relationship SET dataset_id = ("
                "  SELECT dataset_id FROM dataset_member m "
                "   WHERE m.record_id = harmonised_relationship.record_id)"
            )

        shared = sum(1 for members in groups.values() if len(members) > 1)
        outcome.counts = {
            "n_reports": len(studies),
            "n_datasets": len(groups),
            "n_shared": shared,
            "n_links_applied": n_links,
        }
        outcome.messages.append(
            f"{len(studies)} report(s) form {len(groups)} dataset(s); "
            f"{shared} dataset(s) are reported more than once and vote once."
        )
        return self._finish_run(outcome, baseline, actor)

    # =====================================================================
    # 6. counting and grading
    # =====================================================================

    def _vote_rows(self) -> list[dict[str, Any]]:
        """Every countable harmonised relationship, with its study context."""
        return [
            dict(r)
            for r in self.db.query(
                "SELECT h.id, h.dataset_id, h.direction, h.record_id, "
                "       h.predictor_construct, h.outcome_construct, "
                "       p.name AS predictor_name, o.name AS outcome_name, "
                "       rel.evidence_basis, rel.significance, "
                "       ex.design_family, ex.country, ex.appraisal_concern, "
                "       ex.sample_size "
                "FROM harmonised_relationship h "
                "JOIN relationship rel ON rel.id = h.relationship_id "
                "JOIN construct p ON p.id = h.predictor_construct "
                "JOIN construct o ON o.id = h.outcome_construct "
                "LEFT JOIN report_extraction ex "
                "  ON ex.record_id = h.record_id AND ex.coder_role = 'ai' "
                "WHERE h.counts = 1 AND h.dataset_id IS NOT NULL"
            )
        ]

    @staticmethod
    def _reconcile(directions: list[str]) -> tuple[str, bool]:
        """One dataset's verdict when its reports say different things.

        Reports of the same fieldwork disagreeing is a real occurrence --
        different models, different subsamples. The dataset votes 'mixed',
        which counts in the denominator but for neither side, and the
        conflict is flagged. Picking a winner here would hide inside one
        vote a disagreement the review ought to show.
        """
        distinct = set(directions)
        if len(distinct) == 1:
            return directions[0], False
        directional = {d for d in distinct if d in ("positive", "negative")}
        if len(directional) > 1:
            return "mixed", True
        if directional:
            # A direction and some nulls: the direction stands, flagged.
            return directional.pop(), True
        return "mixed", True

    def collect_votes(
        self, rows: Sequence[dict[str, Any]] | None = None
    ) -> dict[tuple[int, int], list[Vote]]:
        """One vote per dataset per construct pair."""
        rows = self._vote_rows() if rows is None else rows
        grouped: dict[tuple[int, int], dict[int, list[dict[str, Any]]]] = {}
        for row in rows:
            key = (row["predictor_construct"], row["outcome_construct"])
            grouped.setdefault(key, {}).setdefault(row["dataset_id"], []).append(row)

        votes: dict[tuple[int, int], list[Vote]] = {}
        for key, by_dataset in grouped.items():
            for dataset_id, members in by_dataset.items():
                direction, conflict = self._reconcile([m["direction"] for m in members])
                first = members[0]
                votes.setdefault(key, []).append(
                    Vote(
                        dataset_id=dataset_id,
                        direction=direction,
                        country=first.get("country") or "",
                        design_family=first.get("design_family") or "",
                        evidence_basis=_strongest_basis(
                            [m.get("evidence_basis") or "asserted" for m in members]
                        ),
                        appraisal_concern=_worst_concern(
                            [m.get("appraisal_concern") or "unclear" for m in members]
                        ),
                        n_reports=len({m["record_id"] for m in members}),
                    )
                )
                if conflict:
                    self._conflicts.append((key, dataset_id))
        return votes

    def grade(self, *, actor: str | None = None) -> StepOutcome:
        """Count the votes and grade every link."""
        actor = actor or self.audit.actor
        self._require_codebook()
        if not self.db.query_one("SELECT 1 FROM harmonised_relationship"):
            raise SynthesisError(
                "Nothing has been harmonised yet. Run the harmonisation step "
                "so that every finding is expressed against the same "
                "reference orientations before they are counted together."
            )
        if not self.db.query_one("SELECT 1 FROM dataset"):
            raise SynthesisError(
                "The datasets have not been built. Until reports are grouped "
                "into datasets, a team that published one survey four times "
                "would out-vote four independent studies."
            )

        run_id, baseline = self._start_run("grade")
        outcome = StepOutcome(run_id=run_id, step="grade")
        self._conflicts = []
        votes = self.collect_votes()
        names = {
            r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM construct")
        }

        by_grade: dict[str, int] = {}
        with self.db.transaction():
            self.db.execute("DELETE FROM link_vote")
            self.db.execute("DELETE FROM graded_link")
            for (predictor, outcome_id), link_votes in sorted(votes.items()):
                key = link_key(names[predictor], names[outcome_id])
                for vote in link_votes:
                    self.db.insert(
                        "link_vote",
                        {
                            "run_id": run_id,
                            "predictor_construct": predictor,
                            "outcome_construct": outcome_id,
                            "dataset_id": vote.dataset_id,
                            "direction": vote.direction,
                            "n_relationships": vote.n_reports,
                            "within_dataset_conflict": int(
                                ((predictor, outcome_id), vote.dataset_id)
                                in self._conflicts
                            ),
                            "country": vote.country,
                            "design_family": vote.design_family,
                            "evidence_basis": vote.evidence_basis,
                            "appraisal_concern": vote.appraisal_concern,
                            "created_at": utc_now(),
                        },
                    )
                graded = grade_link(link_votes, self.settings.grading)
                by_grade[graded.grade] = by_grade.get(graded.grade, 0) + 1
                self._store_graded(run_id, key, predictor, outcome_id, graded)

        outcome.counts = {"n_links": len(votes), **{f"n_{k}": v for k, v in by_grade.items()}}
        note = self.settings.grading.unreachable_note()
        if note:
            outcome.messages.append(note)
        if self._conflicts:
            outcome.messages.append(
                f"{len(self._conflicts)} dataset(s) have reports that "
                "disagree with each other about the same link. Each votes "
                "once, and the conflict is marked."
            )
        outcome.messages.append(
            f"{len(votes)} link(s) graded: "
            + ", ".join(f"{v} {k}" for k, v in sorted(by_grade.items()))
            + "."
        )
        return self._finish_run(outcome, baseline, actor)

    def _store_graded(
        self,
        run_id: int,
        key: str,
        predictor: int,
        outcome_id: int,
        graded: GradedLink,
    ) -> int:
        return self.db.insert(
            "graded_link",
            {
                "run_id": run_id,
                "link_key": key,
                "predictor_construct": predictor,
                "outcome_construct": outcome_id,
                "direction": graded.direction,
                "grade": graded.grade,
                "grade_reason": graded.grade_reason,
                "rule_applied": graded.rule_applied,
                "n_datasets": graded.n_datasets,
                "n_reports": graded.n_reports,
                "n_agree": graded.n_agree,
                "n_oppose": graded.n_oppose,
                "n_null": graded.n_null,
                "n_unclear": graded.n_unclear,
                "n_countries": graded.n_countries,
                "countries": json.dumps(graded.countries),
                "proportion": graded.interval.proportion,
                "ci_low": graded.interval.low,
                "ci_high": graded.interval.high,
                "ci_level": graded.interval.level,
                "sign_p": graded.sign.p_value,
                "sign_significant": int(graded.sign.significant),
                "evidence_bases": json.dumps(graded.evidence_bases),
                "design_families": json.dumps(graded.design_families),
                "created_at": utc_now(),
            },
        )

    def graded_inventory(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query("SELECT * FROM v_graded_inventory")]

    def override_grade(
        self,
        graded_link_id: int,
        grade: str,
        *,
        reason: str,
        actor: str | None = None,
    ) -> None:
        """A reviewer overruling a grade. The original is kept beside it."""
        actor = actor or self.audit.actor
        if not reason.strip():
            raise SynthesisError(
                "An overridden grade needs a reason: it is the only thing "
                "that explains the inventory to a reader who recounts it."
            )
        before = self.db.query_one(
            "SELECT * FROM graded_link WHERE id = ?", (graded_link_id,)
        )
        if before is None:
            raise SynthesisError(f"No graded link {graded_link_id}.")
        self.db.execute(
            "UPDATE graded_link SET grade = ?, grade_overridden = 1, "
            "original_grade = ?, override_reason = ?, overridden_by = ?, "
            "overridden_at = ? WHERE id = ?",
            (
                grade,
                before["original_grade"] or before["grade"],
                reason,
                actor,
                utc_now(),
                graded_link_id,
            ),
        )
        self.audit.log_override(
            "synthesise", "graded_link", graded_link_id, "grade",
            before["grade"], grade, reason=reason, actor=actor,
        )

    # =====================================================================
    # 7a. sensitivity analyses
    # =====================================================================

    #: Each analysis is the same counting over a filtered set of votes, which
    #: is why the grading rules take plain data and know nothing of the
    #: database. The alternative strong rule is the one exception: it changes
    #: the thresholds rather than the evidence.
    SENSITIVITY = {
        "alternative_strong_rule": (
            "A stricter strong rule: seven datasets and three countries.",
            None,
        ),
        "exclude_high_concern": (
            "Studies with a high appraisal concern removed.",
            lambda row: row.get("appraisal_concern") != "high",
        ),
        "quantitative_only": (
            "Only findings resting on a reported statistic.",
            lambda row: row.get("evidence_basis") == "statistical",
        ),
        "experiments_only": (
            "Only randomised and quasi-experimental designs.",
            lambda row: row.get("design_family")
            in ("randomised_experiment", "quasi_experiment"),
        ),
        "observational_only": (
            "Only observational designs.",
            lambda row: row.get("design_family")
            in ("cross_sectional", "longitudinal", "case_study"),
        ),
    }

    def run_sensitivity(self, *, actor: str | None = None) -> StepOutcome:
        """Re-count under each variant and report what moved."""
        actor = actor or self.audit.actor
        base = {
            row["link_key"]: row
            for row in self.db.query("SELECT * FROM graded_link")
        }
        if not base:
            raise SynthesisError("Nothing has been graded yet.")

        run_id, baseline = self._start_run("sensitivity")
        outcome = StepOutcome(run_id=run_id, step="sensitivity")
        rows = self._vote_rows()
        names = {
            r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM construct")
        }
        self._conflicts = []

        n_changed = 0
        with self.db.transaction():
            self.db.execute("DELETE FROM sensitivity_result")
            for analysis, (description, predicate) in self.SENSITIVITY.items():
                if analysis == "alternative_strong_rule":
                    rules = GradingRules(
                        **{
                            **self.settings.grading.as_dict(),
                            "strong_min_datasets": 7,
                            "strong_min_countries": 3,
                            "asserted_bases": tuple(
                                self.settings.grading.asserted_bases
                            ),
                        }
                    )
                    kept = rows
                else:
                    rules = self.settings.grading
                    kept = [r for r in rows if predicate(r)]

                votes = self.collect_votes(kept)
                for key_pair, link_votes in votes.items():
                    key = link_key(names[key_pair[0]], names[key_pair[1]])
                    original = base.get(key)
                    if original is None:
                        continue
                    graded = grade_link(link_votes, rules)
                    changed_grade = graded.grade != original["grade"]
                    changed_sign = (
                        graded.direction != original["direction"]
                        and graded.direction in ("positive", "negative")
                        and original["direction"] in ("positive", "negative")
                    )
                    if not (changed_grade or changed_sign):
                        continue
                    n_changed += 1
                    self.db.insert(
                        "sensitivity_result",
                        {
                            "run_id": run_id,
                            "analysis": analysis,
                            "description": description,
                            "link_key": key,
                            "base_grade": original["grade"],
                            "new_grade": graded.grade,
                            "base_direction": original["direction"],
                            "new_direction": graded.direction,
                            "changed_grade": int(changed_grade),
                            "changed_sign": int(changed_sign),
                            "n_datasets": graded.n_datasets,
                            "base_n_datasets": original["n_datasets"],
                            "detail": json.dumps(graded.as_dict()),
                            "created_at": utc_now(),
                        },
                    )
                # A link that disappears entirely under a filter is itself a
                # finding: the evidence rested on the studies removed.
                for key, original in base.items():
                    if key in {
                        link_key(names[a], names[b]) for a, b in votes
                    }:
                        continue
                    n_changed += 1
                    self.db.insert(
                        "sensitivity_result",
                        {
                            "run_id": run_id,
                            "analysis": analysis,
                            "description": description,
                            "link_key": key,
                            "base_grade": original["grade"],
                            "new_grade": "none",
                            "base_direction": original["direction"],
                            "new_direction": "none",
                            "changed_grade": 1,
                            "changed_sign": 0,
                            "n_datasets": 0,
                            "base_n_datasets": original["n_datasets"],
                            "detail": json.dumps(
                                {"note": "no datasets remain under this analysis"}
                            ),
                            "created_at": utc_now(),
                        },
                    )

        outcome.counts = {"n_analyses": len(self.SENSITIVITY), "n_changed": n_changed}
        outcome.messages.append(
            f"{n_changed} link-and-analysis combination(s) changed grade or "
            f"sign across {len(self.SENSITIVITY)} analyses."
            if n_changed
            else "No link changed grade or sign under any analysis."
        )
        return self._finish_run(outcome, baseline, actor)

    def sensitivity_results(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM sensitivity_result ORDER BY analysis, link_key"
            )
        ]

    # =====================================================================
    # 7b. contradictions
    # =====================================================================

    def explain_contradictions(self, *, actor: str | None = None) -> StepOutcome:
        """Explain each contested link from the study characteristics."""
        actor = actor or self.audit.actor
        contested = [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM graded_link WHERE grade = 'contested' "
                "ORDER BY link_key"
            )
        ]
        run_id, baseline = self._start_run("contradiction")
        outcome = StepOutcome(run_id=run_id, step="contradiction")
        if not contested:
            # Zeroes rather than an empty dict: a step that ran and found
            # nothing should read differently from a step that never ran.
            outcome.counts = {"n_contested": 0, "n_explained": 0}
            outcome.messages.append("No link is contested.")
            return self._finish_run(outcome, baseline, actor)

        rows = self._vote_rows()
        names = {
            r["id"]: r["name"] for r in self.db.query("SELECT id, name FROM construct")
        }
        measured = {
            (r["label_norm"], r["role"]): r["measured_as"]
            for r in self.db.query("SELECT label_norm, role, measured_as FROM label_mapping")
        }

        n_explained = 0
        for link in contested:
            predictor = names.get(link["predictor_construct"], "?")
            outcome_name = names.get(link["outcome_construct"], "?")
            relevant = [
                r
                for r in rows
                if r["predictor_construct"] == link["predictor_construct"]
                and r["outcome_construct"] == link["outcome_construct"]
            ]
            sides = {"positive": [], "negative": [], "none": []}
            for row in relevant:
                if row["direction"] in sides:
                    sides[row["direction"]].append(
                        {
                            "country": row.get("country"),
                            "design_family": row.get("design_family"),
                            "sample_size": row.get("sample_size"),
                            "appraisal_concern": row.get("appraisal_concern"),
                            "measured_as": measured.get(
                                (
                                    normalise_title(row.get("predictor_name") or ""),
                                    "predictor",
                                )
                            ),
                        }
                    )
            try:
                result = self.client.ask(
                    "synthesise:contradiction",
                    system=contradiction_system(),
                    user=contradiction_user(
                        predictor,
                        outcome_name,
                        sides["positive"],
                        sides["negative"],
                        sides["none"],
                    ),
                    schema=ContradictionExplanation,
                    entity="graded_link",
                    entity_id=link["id"],
                    max_tokens=900,
                )
            except BudgetExceeded as exc:
                outcome.status = "paused"
                outcome.messages.append(str(exc))
                break
            explanation: ContradictionExplanation = result.value
            self.db.execute(
                "INSERT OR REPLACE INTO contradiction"
                "(run_id, link_key, graded_link_id, explanation_type, explanation, "
                " suggestion_type, suggested_moderator, suggested_split, "
                " supporting, ai_call_id, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    link["link_key"],
                    link["id"],
                    explanation.explanation_type,
                    explanation.explanation,
                    explanation.suggestion_type,
                    explanation.suggested_moderator,
                    json.dumps(explanation.split_into),
                    json.dumps(
                        {k: len(v) for k, v in sides.items()}
                    ),
                    result.ai_call_id,
                    utc_now(),
                ),
            )
            n_explained += 1

        outcome.counts = {"n_contested": len(contested), "n_explained": n_explained}
        outcome.messages.append(
            f"{n_explained} contested link(s) explained. Accept or reject "
            "each suggestion; nothing is applied until you do."
        )
        return self._finish_run(outcome, baseline, actor)

    def contradictions(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM contradiction ORDER BY link_key"
            )
        ]

    def decide_contradiction(
        self,
        contradiction_id: int,
        *,
        accepted: bool,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        actor = actor or self.audit.actor
        self.db.execute(
            "UPDATE contradiction SET status = ?, decided_by = ?, decided_at = ?, "
            "decision_reason = ? WHERE id = ?",
            (
                "accepted" if accepted else "rejected",
                actor,
                utc_now(),
                reason,
                contradiction_id,
            ),
        )
        self.audit.log(
            "synthesise",
            "contradiction_accepted" if accepted else "contradiction_rejected",
            entity="contradiction", entity_id=contradiction_id,
            detail={"reason": reason}, actor=actor,
        )

    # -- estimating --------------------------------------------------------

    def estimate(self, step: str) -> CostEstimate:
        """What one step would cost before it runs (integrity rule 5)."""
        if step == "codebook":
            protocol_text, _ = self._protocol()
            info = self.project.info()
            prompts = [
                (
                    codebook_system(protocol_text, info.get("objectives", "")),
                    codebook_user(self.labels()),
                )
            ]
            return self.client.estimate(
                prompts, schema=CodebookProposal, expected_completion_tokens=1400
            )
        if step == "mapping":
            constructs = self.constructs(status="approved")
            system = mapping_system(constructs) if constructs else ""
            pending = self._pending_labels()
            batches = [
                pending[i : i + self.settings.mapping_batch]
                for i in range(0, len(pending), self.settings.mapping_batch)
            ]
            return self.client.estimate(
                [(system, mapping_user(batch)) for batch in batches],
                schema=MappingAnswer,
                expected_completion_tokens=140 * self.settings.mapping_batch,
            )
        if step == "linkage":
            studies = self.study_descriptions()
            prompts = []
            for i, a in enumerate(studies):
                for b in studies[i + 1 :]:
                    score, _ = score_shared_dataset(a, b)
                    if score >= self.settings.linkage_threshold:
                        prompts.append((linkage_system(), linkage_user(a, b)))
            return self.client.estimate(
                prompts, schema=LinkageJudgement, expected_completion_tokens=120
            )
        if step == "contradiction":
            n = self.db.scalar(
                "SELECT COUNT(*) FROM graded_link WHERE grade = 'contested'"
            )
            prompts = [
                (contradiction_system(), contradiction_user("a", "b", [], []))
            ] * int(n or 0)
            return self.client.estimate(
                prompts,
                schema=ContradictionExplanation,
                expected_completion_tokens=300,
            )
        raise SynthesisError(f"No estimate for step {step!r}.")


def link_key(predictor: str, outcome: str) -> str:
    """A stable, readable identifier for a link.

    Readable on purpose: it appears in the framework's propositions and in
    the sensitivity tables, and "trust -> participation" tells a reader what
    L17 does not.
    """
    return f"{predictor.strip()} -> {outcome.strip()}"


def _strongest_basis(bases: Sequence[str]) -> str:
    for basis in ("statistical", "thematic", "asserted"):
        if basis in bases:
            return basis
    return "asserted"


def _worst_concern(concerns: Sequence[str]) -> str:
    for concern in ("high", "moderate", "unclear", "low"):
        if concern in concerns:
            return concern
    return "unclear"
