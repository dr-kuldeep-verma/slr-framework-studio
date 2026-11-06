"""Themes, propositions and figures, built from the graded inventory.

The stage has one job that matters more than the others: it must not let a
claim into the framework that the evidence does not carry. Three things do
that, and they are deliberately not the same thing:

1. The prompt tells the model to cite only link ids from the inventory.
2. `draft_propositions` checks every citation against the inventory and
   discards the whole proposition if any one of them is missing, rather than
   deleting the bad citation and keeping the argument.
3. The `proposition_link` foreign key means the database could not store a
   dangling citation even if the first two failed.

A reviewer can accept, reject or rewrite anything here, and the model's
original wording is kept beside the edit so the audit trail shows both.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..ai.client import AIClient, BudgetExceeded, CostEstimate
from ..core.db import utc_now
from ..importers.normalise import normalise_title
from .figures import write_figures
from .prompts import (
    PROMPT_VERSION,
    proposition_system,
    proposition_user,
    theme_system,
    theme_user,
)
from .schemas import PropositionAnswer, ThemeAnswer
from .style import GRADE_ORDER

__all__ = [
    "FrameworkEngine",
    "FrameworkError",
    "FrameworkSettings",
    "StepOutcome",
]


class FrameworkError(RuntimeError):
    """Raised when a step is asked for before what it rests on exists."""


@dataclass
class FrameworkSettings:
    max_tokens: int = 4000
    budget_usd: float | None = None
    #: Grades weak enough that a proposition resting only on them is flagged
    #: for the reviewer rather than presented as a finding.
    weak_grades: tuple[str, ...] = ("tentative", "contested", "asserted")

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_tokens": self.max_tokens,
            "budget_usd": self.budget_usd,
            "weak_grades": list(self.weak_grades),
            "prompt_version": PROMPT_VERSION,
        }


@dataclass
class StepOutcome:
    """What one step of the framework builder did."""

    step: str
    status: str = "finished"
    counts: dict[str, int] = field(default_factory=dict)
    n_calls: int = 0
    n_cached: int = 0
    cost_usd: float = 0.0
    messages: list[str] = field(default_factory=list)

    _PHRASES: ClassVar[dict[str, dict[str, str]]] = {
        "themes": {
            "n_constructs": "{} construct(s)",
            "n_themes": "{} theme(s) proposed",
            "n_unassigned": "{} construct(s) left over",
        },
        "propositions": {
            "n_returned": "{} returned",
            "n_kept": "{} kept",
            "n_rejected": "{} rejected",
            "n_weak": "{} resting only on weak evidence",
        },
        "evidence_maps": {
            "n_themes": "{} theme(s)",
            "n_figures": "{} figure(s)",
            "n_links": "{} link(s) drawn",
        },
        "integrated": {
            "n_figures": "{} figure(s)",
            "n_evidenced": "{} evidenced link(s)",
            "n_mechanisms": "{} proposed mechanism(s)",
            "n_untested": "{} untested pathway(s)",
        },
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


class FrameworkEngine:
    def __init__(self, project, client: AIClient, settings=None) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit
        self.client = client
        self.settings = settings or FrameworkSettings()

    # -- reading what the synthesis left ----------------------------------

    def counts(self) -> dict[str, int]:
        row = self.db.query_one("SELECT * FROM v_framework_progress")
        return dict(row) if row else {}

    def inventory(self) -> list[dict[str, Any]]:
        """Every graded link, with its constructs named.

        This is the only evidence this stage may use, which is why it is the
        only thing it reads.
        """
        return [
            dict(row)
            for row in self.db.query(
                "SELECT g.*, p.name AS predictor_name, o.name AS outcome_name "
                "FROM graded_link g "
                "JOIN construct p ON p.id = g.predictor_construct "
                "JOIN construct o ON o.id = g.outcome_construct "
                "ORDER BY g.id"
            )
        ]

    def themes(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT t.*, "
                "  (SELECT COUNT(*) FROM theme_member m WHERE m.theme_id = t.id) "
                "    AS n_constructs "
                "FROM theme t ORDER BY t.position, t.id"
            )
        ]

    def theme_constructs(self, theme_id: int) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT c.* FROM theme_member m "
                "JOIN construct c ON c.id = m.construct_id "
                "WHERE m.theme_id = ? ORDER BY c.position, c.id",
                (theme_id,),
            )
        ]

    def propositions(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM v_propositions ORDER BY number, id"
            )
        ]

    def elements(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM framework_element ORDER BY kind, id"
            )
        ]

    def links_not_drawn(self) -> list[dict[str, Any]]:
        """Graded links whose two constructs are in different themes.

        One figure per theme is what the spec asks for, so a link across two
        themes belongs to neither. It is not lost -- the integrated diagram
        draws every link -- but the reviewer is told, because a figure that
        silently omits evidence is the kind of thing nobody notices until a
        referee does.
        """
        return [
            link
            for link in self.inventory()
            if not self.db.scalar(
                "SELECT COUNT(*) FROM theme_member a "
                "JOIN theme_member b ON b.theme_id = a.theme_id "
                "JOIN theme t ON t.id = a.theme_id "
                "WHERE a.construct_id = ? AND b.construct_id = ? "
                "AND t.status <> 'rejected'",
                (link["predictor_construct"], link["outcome_construct"]),
            )
        ]

    def figures(self) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM figure ORDER BY kind, theme_id, format, id"
            )
        ]

    # -- guards ------------------------------------------------------------

    def _require_inventory(self) -> list[dict[str, Any]]:
        links = self.inventory()
        if not links:
            raise FrameworkError(
                "There is no graded inventory yet. The framework may only "
                "cite graded links, so the synthesis has to be run first."
            )
        return links

    def _require_themes(self) -> list[dict[str, Any]]:
        """The themes that may be drawn.

        None proposed and all rejected are different states and must not be
        reported as the same thing: the first means the stage has not been
        run, the second means the reviewer has read the groupings and turned
        them all down. Only the first is an error.
        """
        themes = self.themes()
        if not themes:
            raise FrameworkError(
                "No themes have been proposed. One evidence map is drawn per "
                "theme, so the themes come first."
            )
        return [t for t in themes if t["status"] != "rejected"]

    def _protocol(self) -> str:
        current = self.project.protocol.current()
        if current is None:
            raise FrameworkError(
                "The protocol has not been locked. The framework is what the "
                "review concludes, so it has to rest on a fixed protocol."
            )
        return current.content

    def _baseline(self) -> tuple[int, int, float]:
        return (self.client.n_calls, self.client.n_cached, self.client.spent_usd)

    def _apply(self, outcome: StepOutcome, baseline) -> StepOutcome:
        calls, cached, spent = baseline
        outcome.n_calls = self.client.n_calls - calls
        outcome.n_cached = self.client.n_cached - cached
        outcome.cost_usd = round(self.client.spent_usd - spent, 6)
        return outcome

    # -- themes ------------------------------------------------------------

    def propose_themes(self, *, actor: str | None = None) -> StepOutcome:
        """Group the graded constructs into themes, one figure each."""
        actor = actor or self.audit.actor
        links = self._require_inventory()
        protocol = self._protocol()
        baseline = self._baseline()
        outcome = StepOutcome(step="themes")

        used_ids = {link["predictor_construct"] for link in links} | {
            link["outcome_construct"] for link in links
        }
        constructs = [
            dict(row)
            for row in self.db.query(
                "SELECT * FROM construct WHERE id IN "
                f"({','.join('?' * len(used_ids))}) ORDER BY position, id",
                tuple(sorted(used_ids)),
            )
        ]
        outcome.counts["n_constructs"] = len(constructs)

        info = self.project.info()
        try:
            result = self.client.ask(
                "framework:themes",
                system=theme_system(protocol, info.get("objectives", "")),
                user=theme_user(constructs),
                schema=ThemeAnswer,
                entity="theme",
                max_tokens=self.settings.max_tokens,
            )
            proposed = result.value.themes
            call_id = result.ai_call_id
        except BudgetExceeded as exc:
            outcome.status = "paused"
            outcome.messages.append(str(exc))
            return self._apply(outcome, baseline)

        by_name = {normalise_title(c["name"]): c for c in constructs}
        claimed: set[int] = set()
        n_themes = 0
        with self.db.transaction():
            for position, theme in enumerate(proposed, start=1):
                members = [
                    by_name[normalise_title(name)]
                    for name in theme.constructs
                    if normalise_title(name) in by_name
                    and by_name[normalise_title(name)]["id"] not in claimed
                ]
                if not members:
                    continue
                theme_id = self.db.insert(
                    "theme",
                    {
                        "name": theme.name,
                        "description": theme.description,
                        "position": position,
                        "ai_call_id": call_id,
                        "created_at": utc_now(),
                        "created_by": "ai",
                    },
                )
                n_themes += 1
                for construct in members:
                    self.db.insert(
                        "theme_member",
                        {"theme_id": theme_id, "construct_id": construct["id"]},
                    )
                    claimed.add(construct["id"])

            # A construct the model left out would be a link left out of every
            # figure, so the leftovers get a theme of their own rather than
            # disappearing.
            leftover = [c for c in constructs if c["id"] not in claimed]
            if leftover:
                theme_id = self.db.insert(
                    "theme",
                    {
                        "name": "Other constructs",
                        "description": (
                            "Constructs the proposal did not place in a theme. "
                            "They are kept together so that no graded link is "
                            "left out of every figure."
                        ),
                        "position": len(proposed) + 1,
                        "ai_call_id": call_id,
                        "created_at": utc_now(),
                        "created_by": "ai",
                    },
                )
                n_themes += 1
                for construct in leftover:
                    self.db.insert(
                        "theme_member",
                        {"theme_id": theme_id, "construct_id": construct["id"]},
                    )

        outcome.counts["n_themes"] = n_themes
        outcome.counts["n_unassigned"] = len(leftover)
        if leftover:
            outcome.messages.append(
                f"{len(leftover)} construct(s) were not placed in a theme and "
                "have been put together under \"Other constructs\". Move them "
                "where they belong before drawing the figures."
            )
        outcome.messages.append(
            f"{n_themes} theme(s) proposed. Approve or reject each; a rejected "
            "theme gets no figure."
        )
        self.audit.log(
            "framework", "themes_proposed", entity="theme",
            detail={"n_themes": n_themes, "n_constructs": len(constructs)},
            actor=actor,
        )
        return self._apply(outcome, baseline)

    def decide_theme(
        self,
        theme_id: int,
        *,
        approved: bool,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        actor = actor or self.audit.actor
        self.db.execute(
            "UPDATE theme SET status = ?, status_reason = ?, decided_by = ?, "
            "decided_at = ? WHERE id = ?",
            (
                "approved" if approved else "rejected",
                reason,
                actor,
                utc_now(),
                theme_id,
            ),
        )
        self.audit.log(
            "framework",
            "theme_approved" if approved else "theme_rejected",
            entity="theme",
            entity_id=theme_id,
            detail={"reason": reason},
            actor=actor,
        )

    def rename_theme(
        self, theme_id: int, name: str, *, actor: str | None = None
    ) -> None:
        actor = actor or self.audit.actor
        before = self.db.scalar("SELECT name FROM theme WHERE id = ?", (theme_id,))
        self.db.execute("UPDATE theme SET name = ? WHERE id = ?", (name, theme_id))
        self.audit.log(
            "framework", "theme_renamed", entity="theme", entity_id=theme_id,
            detail={"from": before, "to": name}, actor=actor,
        )

    def move_construct(
        self, construct_id: int, theme_id: int, *, actor: str | None = None
    ) -> None:
        """Put a construct in a different theme, as a reviewer regrouping."""
        actor = actor or self.audit.actor
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM theme_member WHERE construct_id = ?", (construct_id,)
            )
            self.db.execute(
                "INSERT OR IGNORE INTO theme_member(theme_id, construct_id) "
                "VALUES(?, ?)",
                (theme_id, construct_id),
            )
        self.audit.log(
            "framework", "construct_moved", entity="theme", entity_id=theme_id,
            detail={"construct_id": construct_id}, actor=actor,
        )

    # -- propositions ------------------------------------------------------

    def draft_propositions(self, *, actor: str | None = None) -> StepOutcome:
        """Draft propositions, keeping only those whose citations all resolve.

        A proposition citing any link that is not in the graded inventory is
        discarded whole. Deleting the offending citation and keeping the rest
        would leave an argument that no longer says what the model meant, and
        would hide that the model cited something that does not exist.
        """
        actor = actor or self.audit.actor
        links = self._require_inventory()
        protocol = self._protocol()
        themes = [t for t in self.themes() if t["status"] != "rejected"]
        baseline = self._baseline()
        outcome = StepOutcome(step="propositions")

        known = {link["id"] for link in links}
        grades = [g for g in GRADE_ORDER if any(l["grade"] == g for l in links)]
        theories = self._theories()

        try:
            result = self.client.ask(
                "framework:propositions",
                system=proposition_system(protocol, theories, themes, grades),
                user=proposition_user(links),
                schema=PropositionAnswer,
                entity="proposition",
                max_tokens=self.settings.max_tokens,
            )
            returned = result.value.propositions
            call_id = result.ai_call_id
        except BudgetExceeded as exc:
            outcome.status = "paused"
            outcome.messages.append(str(exc))
            return self._apply(outcome, baseline)

        by_theme = {normalise_title(t["name"]): t["id"] for t in themes}
        grade_of = {link["id"]: link["grade"] for link in links}
        n_kept = n_rejected = n_weak = 0

        with self.db.transaction():
            for proposition in returned:
                cited = proposition.link_ids()
                missing = [i for i in cited if i not in known]
                needs_evidence = proposition.element_kind == "evidenced_link"

                reason = ""
                if missing:
                    reason = (
                        "cites "
                        + ", ".join(f"L{i}" for i in missing)
                        + ", which "
                        + ("is" if len(missing) == 1 else "are")
                        + " not in the graded inventory"
                    )
                elif needs_evidence and not cited:
                    reason = (
                        "is put forward as an evidenced link but cites no link"
                    )

                if reason:
                    n_rejected += 1
                    self.db.insert(
                        "proposition",
                        {
                            "number": 0,
                            "statement": proposition.statement,
                            "mechanism": proposition.mechanism,
                            "theories": json.dumps(proposition.theories),
                            "moderators": proposition.moderators,
                            "boundary_conditions": proposition.boundary_conditions,
                            "falsifying_test": proposition.falsifying_test,
                            "element_kind": proposition.element_kind,
                            "status": "rejected",
                            "status_reason": f"Rejected automatically: {reason}.",
                            "ai_call_id": call_id,
                            "created_at": utc_now(),
                        },
                    )
                    outcome.messages.append(
                        f"Rejected a proposition that {reason}."
                    )
                    continue

                n_kept += 1
                weak = bool(cited) and all(
                    grade_of[i] in self.settings.weak_grades for i in cited
                )
                n_weak += weak
                proposition_id = self.db.insert(
                    "proposition",
                    {
                        "number": n_kept,
                        "theme_id": by_theme.get(normalise_title(proposition.theme)),
                        "statement": proposition.statement,
                        "mechanism": proposition.mechanism,
                        "theories": json.dumps(proposition.theories),
                        "moderators": proposition.moderators,
                        "boundary_conditions": proposition.boundary_conditions,
                        "falsifying_test": proposition.falsifying_test,
                        "element_kind": proposition.element_kind,
                        "ai_call_id": call_id,
                        "created_at": utc_now(),
                    },
                )
                for link_id in cited:
                    self.db.insert(
                        "proposition_link",
                        {
                            "proposition_id": proposition_id,
                            "graded_link_id": link_id,
                        },
                    )
                self._store_element(proposition, proposition_id, cited, links)

        outcome.counts.update(
            n_returned=len(returned),
            n_kept=n_kept,
            n_rejected=n_rejected,
            n_weak=n_weak,
        )
        if n_weak:
            outcome.messages.append(
                f"{n_weak} proposition(s) rest only on tentative, contested or "
                "asserted links. Check that each says so in its boundary "
                "conditions."
            )
        self.audit.log(
            "framework", "propositions_drafted", entity="proposition",
            detail={
                "n_returned": len(returned),
                "n_kept": n_kept,
                "n_rejected": n_rejected,
            },
            actor=actor,
        )
        return self._apply(outcome, baseline)

    def _theories(self) -> list[str]:
        """Theories the reviewer listed, plus those the extraction recorded."""
        info = self.project.info()
        listed = [
            name.strip()
            for name in (info.get("theories") or "").replace(";", "\n").splitlines()
            if name.strip()
        ]
        if listed:
            return listed
        found: list[str] = []
        for row in self.db.query(
            "SELECT theories FROM report_extraction WHERE coder_role = 'ai'"
        ):
            try:
                entries = json.loads(row["theories"] or "[]")
            except (TypeError, ValueError):
                continue
            for entry in entries:
                name = (
                    entry.get("name") if isinstance(entry, dict) else str(entry)
                ) or ""
                name = name.strip()
                if name and name not in found:
                    found.append(name)
        return found

    def _store_element(
        self,
        proposition,
        proposition_id: int,
        cited: list[int],
        links: list[dict[str, Any]],
    ) -> None:
        """Record what the integrated diagram should draw for this proposition.

        An evidenced link points at the graded link it stands for; the other
        two kinds carry no link, which is what keeps them visibly apart.
        """
        by_id = {link["id"]: link for link in links}
        if proposition.element_kind == "evidenced_link":
            for link_id in cited:
                link = by_id[link_id]
                self.db.insert(
                    "framework_element",
                    {
                        "kind": "evidenced_link",
                        "source_label": link["predictor_name"],
                        "target_label": link["outcome_name"],
                        "graded_link_id": link_id,
                        "proposition_id": proposition_id,
                        "direction": link["direction"],
                        "label": f"L{link_id}",
                        "created_at": utc_now(),
                    },
                )
            return
        # A mechanism or an untested pathway names its own endpoints; where
        # the model gave none, the statement itself is the label.
        self.db.insert(
            "framework_element",
            {
                "kind": proposition.element_kind,
                "source_label": proposition.theme or "framework",
                "target_label": proposition.statement[:80],
                "proposition_id": proposition_id,
                "direction": "unclear",
                "label": "",
                "note": proposition.mechanism,
                "created_at": utc_now(),
            },
        )

    def decide_proposition(
        self,
        proposition_id: int,
        *,
        accepted: bool,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        actor = actor or self.audit.actor
        self.db.execute(
            "UPDATE proposition SET status = ?, status_reason = ?, "
            "decided_by = ?, decided_at = ? WHERE id = ?",
            (
                "accepted" if accepted else "rejected",
                reason,
                actor,
                utc_now(),
                proposition_id,
            ),
        )
        self.audit.log(
            "framework",
            "proposition_accepted" if accepted else "proposition_rejected",
            entity="proposition",
            entity_id=proposition_id,
            detail={"reason": reason},
            actor=actor,
        )

    def edit_proposition(
        self, proposition_id: int, *, actor: str | None = None, **fields: str
    ) -> None:
        """Rewrite a proposition, keeping what the model wrote beside it."""
        actor = actor or self.audit.actor
        allowed = {
            "statement", "mechanism", "moderators", "boundary_conditions",
            "falsifying_test",
        }
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            return
        before = self.db.query_one(
            "SELECT * FROM proposition WHERE id = ?", (proposition_id,)
        )
        if before is None:
            raise FrameworkError(f"No proposition {proposition_id}.")
        original = before["edited_from"] or json.dumps(
            {key: before[key] for key in sorted(allowed)}, indent=1
        )
        assignments = ", ".join(f"{key} = :{key}" for key in changes)
        self.db.execute(
            f"UPDATE proposition SET {assignments}, status = 'edited', "
            "edited_from = :edited_from, decided_by = :decided_by, "
            "decided_at = :decided_at WHERE id = :id",
            {
                **changes,
                "edited_from": original,
                "decided_by": actor,
                "decided_at": utc_now(),
                "id": proposition_id,
            },
        )
        self.audit.log(
            "framework", "proposition_edited", entity="proposition",
            entity_id=proposition_id, detail={"fields": sorted(changes)},
            actor=actor,
        )

    def add_element(
        self,
        kind: str,
        source_label: str,
        target_label: str,
        *,
        note: str = "",
        actor: str | None = None,
    ) -> int:
        """Add a mechanism or an untested pathway the reviewer wants drawn."""
        actor = actor or self.audit.actor
        if kind == "evidenced_link":
            raise FrameworkError(
                "An evidenced link cannot be added by hand; it comes from the "
                "graded inventory, which is the point of grading."
            )
        element_id = self.db.insert(
            "framework_element",
            {
                "kind": kind,
                "source_label": source_label,
                "target_label": target_label,
                "direction": "unclear",
                "note": note,
                "created_at": utc_now(),
            },
        )
        self.audit.log(
            "framework", "element_added", entity="framework_element",
            entity_id=element_id, detail={"kind": kind}, actor=actor,
        )
        return element_id

    # -- figures -----------------------------------------------------------

    def draw_evidence_maps(self, *, actor: str | None = None) -> StepOutcome:
        """One figure per approved theme, from graded links only."""
        actor = actor or self.audit.actor
        links = self._require_inventory()
        themes = self._require_themes()
        outcome = StepOutcome(step="evidence_maps")

        approved = [t for t in themes if t["status"] != "rejected"]
        written = write_figures(
            self.project, self, approved, links, kind="evidence_map"
        )
        drawn = sum(f["n_links"] for f in written if f["format"] == "svg")
        missing = self.links_not_drawn()
        outcome.counts.update(
            n_themes=len(approved),
            n_figures=len(written),
            n_links=drawn,
            n_not_drawn=len(missing),
        )
        if not written:
            outcome.messages.append(
                "No figure was drawn: every theme is rejected, or no theme "
                "holds a graded link."
            )
        if missing:
            examples = ", ".join(
                f"L{link['id']} ({link['predictor_name']} → "
                f"{link['outcome_name']})"
                for link in missing[:4]
            )
            outcome.messages.append(
                f"{len(missing)} graded link(s) appear in no evidence map, "
                "because their two constructs are in different themes: "
                f"{examples}"
                + (", and others." if len(missing) > 4 else ".")
                + " Move the constructs into one theme if you want a figure "
                "to show them; the integrated diagram shows them regardless."
            )
        self.audit.log(
            "framework", "evidence_maps_drawn", entity="figure",
            detail={"n_figures": len(written)}, actor=actor,
        )
        return outcome

    def draw_integrated_diagram(self, *, actor: str | None = None) -> StepOutcome:
        """The whole framework in one figure, the three kinds kept apart."""
        actor = actor or self.audit.actor
        links = self._require_inventory()
        outcome = StepOutcome(step="integrated")

        written = write_figures(self.project, self, [], links, kind="integrated")
        # Counted from what the figure drew, not from the element table: with
        # no propositions drafted yet the diagram stands in for the inventory
        # itself, and reporting nothing drawn would be false.
        drawn: dict[str, int] = {}
        for figure in written:
            if figure["format"] == "svg":
                drawn = figure["drawn"]
        mechanisms = drawn.pop("proposed_mechanism", 0)
        untested = drawn.pop("untested_pathway", 0)
        outcome.counts.update(
            n_figures=len(written),
            n_evidenced=sum(drawn.values()),
            n_mechanisms=mechanisms,
            n_untested=untested,
        )
        self.audit.log(
            "framework", "integrated_diagram_drawn", entity="figure",
            detail={"n_figures": len(written)}, actor=actor,
        )
        return outcome

    # -- cost ---------------------------------------------------------------

    def estimate(self, step: str) -> CostEstimate:
        if step == "themes":
            return self.client.estimate(1, self.settings.max_tokens)
        if step == "propositions":
            return self.client.estimate(1, self.settings.max_tokens)
        return self.client.estimate(0, 0)
