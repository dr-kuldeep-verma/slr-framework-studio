"""Drafting, locking and amending the review protocol.

A protocol matters only if it was fixed *before* the data were seen. This
module makes that checkable rather than claimed: version 1 is drafted from the
form and locked with a timestamp and a content hash, and every later change
records its reason and whether it was made after the first record was
imported. The amendment log is what the Methods section reports.

Locking does not freeze the form. A reviewer can keep editing; what they
cannot do is change the protocol without it showing.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from .db import Database, utc_now
from .hashing import sha256_text

__all__ = [
    "ProtocolService",
    "ProtocolVersion",
    "ProtocolError",
    "draft_protocol",
    "framework_development_section",
    "parse_a_priori_constructs",
]


class ProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProtocolVersion:
    id: int
    version: int
    content: str
    content_hash: str
    locked_at: str
    locked_by: str

    @property
    def short_hash(self) -> str:
        return self.content_hash[:12]


_FRAMEWORK_HEADINGS = {
    "PCC": ("Population", "Concept", "Context"),
    "PICO": ("Population", "Intervention", "Comparator", "Outcome"),
    "SPIDER": ("Sample", "Phenomenon of interest", "Design", "Evaluation", "Research type"),
    "SOBC": ("Stimulus", "Organism", "Behaviour", "Consequence"),
}


def _framework_headings(framework: str, stages=None) -> tuple[str, ...]:
    """The parts a framework's criteria are organised against.

    The four with established wording keep it. Every other framework,
    built-in or the reviewer's own, is described by its own stages: before
    this, anything outside the four was described as Population, Concept and
    Context, so a CIMO or a reviewer-defined protocol misstated its own
    framework in the document that gets locked and hashed.
    """
    if framework in _FRAMEWORK_HEADINGS:
        return _FRAMEWORK_HEADINGS[framework]
    if not stages:
        from .frameworks import BUILT_IN

        stages = BUILT_IN.get(framework, ())
    if stages:
        return tuple(str(s)[:1].upper() + str(s)[1:] for s in stages)
    return _FRAMEWORK_HEADINGS["PCC"]


def _bullets(text: str) -> str:
    """Turn a free-text box into a list, leaving prose alone."""
    lines = [line.strip(" -*\t") for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return "_Not yet stated._"
    if len(lines) == 1:
        return lines[0]
    return "\n".join(f"- {line}" for line in lines)


def parse_a_priori_constructs(text: str) -> list[tuple[str, str]]:
    """Read the reviewer's declared constructs, one per line.

    A line is either a name, or a name and the reviewer's own definition
    separated by a dash: ``Trust — confidence that the authority will act
    fairly``. Either dash is accepted, because a reviewer typing into a box
    types whichever one their keyboard gives them, and an em dash inside the
    definition itself must not split it again.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•*").strip()
        if not line:
            continue
        name, definition = line, ""
        for dash in ("—", " – ", " - ", "–"):
            if dash in line:
                head, _, tail = line.partition(dash)
                if head.strip():
                    name, definition = head.strip(), tail.strip()
                break
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        out.append((name, definition))
    return out


def framework_development_section(info: dict[str, Any]) -> str:
    """The protocol's "Framework development" section, as text.

    Public because the synthesis engine compares it against the locked
    protocol before building a top-down codebook. If the two differ, the
    constructs the reviewer is about to code against are not the ones the
    protocol commits to, and "a priori" would be a claim about nothing.
    """
    return "\n".join(_framework_development(info)).strip()


def _framework_development(info: dict[str, Any]) -> list[str]:
    """The section stating how the framework will be built.

    It is in the protocol rather than only in the database because that is
    what makes a top-down review's constructs a priori: locking the
    protocol dates and hashes them, so revising them once the evidence is
    in becomes an amendment with a reason rather than a quiet edit.
    """
    approach = info.get("framework_approach") or "bottom_up"
    lines = ["", "## Framework development", ""]
    if approach == "top_down":
        constructs = parse_a_priori_constructs(info.get("a_priori_constructs", ""))
        theory = (info.get("a_priori_theory") or "").strip()
        citation = (info.get("a_priori_citation") or "").strip()
        source = f" from {theory}" if theory else " from theory"
        lines += [
            f"**Top-down (deductive).** The constructs below are specified in "
            f"advance,{source}. Extracted author labels are coded into "
            "them. A label that fits none of them is coded to the residual "
            "category and reported, rather than being forced into the "
            "nearest construct.",
            "",
        ]
        if theory:
            lines.append(f"- Source theory: **{theory}**")
            if citation:
                lines.append(f"- Citation: {citation}")
            lines.append("")
        if constructs:
            for name, definition in constructs:
                lines.append(f"- **{name}**" + (f" — {definition}" if definition else ""))
        else:
            lines.append(
                "_No constructs have been specified yet. A top-down review "
                "cannot be coded until they are._"
            )
    else:
        lines.append(
            "**Bottom-up (inductive).** No constructs are specified in "
            "advance. The codebook is derived from what the included studies "
            "turned out to measure, proposed by the model from the extracted "
            "author labels and then read, edited and approved by a human "
            "before anything is mapped to it."
        )
    return lines


def draft_protocol(
    info: dict[str, Any],
    exclusion_codes: list[dict[str, Any]],
    *,
    seeds: list[dict[str, Any]] | None = None,
    stages: tuple[str, ...] | list[str] | None = None,
) -> str:
    """Compose the protocol document from the form.

    A pure function of the form's contents, with no timestamp in the body.
    That is what makes the content hash mean something: the same form always
    produces the same document, so a changed hash is a changed protocol rather
    than a different minute. When the document was locked, and by whom, lives
    beside it in `protocol_version`.

    Nothing is invented to fill a gap -- an unanswered field says so.
    """
    framework = info.get("framework_type", "PCC")
    headings = _framework_headings(framework, stages)
    questions = info.get("research_questions") or []
    languages = info.get("languages") or []

    date_from, date_to = info.get("date_from"), info.get("date_to")
    if date_from and date_to:
        dates = f"{date_from} to {date_to}"
    elif date_from:
        dates = f"{date_from} onwards"
    elif date_to:
        dates = f"up to {date_to}"
    else:
        dates = "No date limit."

    lines: list[str] = [
        f"# Review protocol: {info.get('title') or 'Untitled review'}",
        "",
        "## Objectives",
        "",
        _bullets(info.get("objectives", "")),
        "",
        "## Research questions",
        "",
    ]
    if questions:
        lines += [f"{i}. {q}" for i, q in enumerate(questions, start=1)]
    else:
        lines.append("_Not yet stated._")

    joined = (
        headings[0] if len(headings) == 1
        else ", ".join(headings[:-1]) + f" and {headings[-1]}"
    )
    lines += [
        "",
        f"## Framework: {framework}",
        "",
        f"The eligibility criteria below are organised against {joined}.",
        "",
        "## Inclusion criteria",
        "",
        _bullets(info.get("inclusion_criteria", "")),
        "",
        "## Exclusion criteria",
        "",
        "Applied in the order below, cheapest and most objective first. A "
        "record is excluded under the *first* code it fails, and that code is "
        "what the PRISMA diagram reports.",
        "",
        "| Order | Code | Criterion | Definition |",
        "| --- | --- | --- | --- |",
    ]
    if exclusion_codes:
        for code in exclusion_codes:
            definition = (code.get("definition") or "").replace("|", "\\|")
            lines.append(
                f"| {code['position']} | {code['code']} | {code['label']} | {definition} |"
            )
    else:
        lines.append("| — | — | _No exclusion codes defined._ | |")

    lines += _framework_development(info)

    lines += [
        "",
        "## Limits",
        "",
        f"- Publication dates: {dates}",
        "- Languages: " + (", ".join(languages) if languages else "No language limit."),
        "",
        "## Seed studies",
        "",
        "Studies the search is expected to retrieve. A seed the search misses "
        "is evidence that the query is too narrow.",
        "",
    ]
    if seeds:
        for seed in seeds:
            note = f" — {seed['note']}" if seed.get("note") else ""
            lines.append(f"- {seed['doi']}{note}")
    else:
        lines.append("_None recorded._")

    lines += [
        "",
        "## Procedure",
        "",
        "1. Records are imported from database exports and deduplicated by "
        "DOI, then by normalised title and year, then by fuzzy title "
        "similarity above a recorded threshold.",
        "2. Titles and abstracts are screened by two independent AI screeners "
        "working from this protocol alone; a third call adjudicates "
        "disagreements, and a quality-control pass re-reads a reproducible "
        "random sample of joint exclusions.",
        "3. Every AI decision is logged with its model, prompt hash and input "
        "hash, and can be overridden by a human reviewer; the override is "
        "logged.",
        "4. Agreement between the two screeners is reported as percentage "
        "agreement, Cohen's κ and PABAK.",
        "",
        "## Changes to this protocol",
        "",
        "Amendments are recorded automatically with their date, reason and "
        "whether they were made after any records had been imported.",
        "",
    ]
    return "\n".join(lines)


class ProtocolService:
    def __init__(self, project) -> None:
        self.project = project
        self.db: Database = project.db
        self.audit = project.audit

    # -- drafting and locking ---------------------------------------------

    def draft(self) -> str:
        """The document the current form would produce, unlocked."""
        seeds = [dict(r) for r in self.db.query("SELECT doi, note FROM seed_study ORDER BY id")]
        return draft_protocol(
            self.project.info(), self.project.exclusion_codes(), seeds=seeds,
            stages=self.project.framework_stages(),
        )

    def current(self) -> ProtocolVersion | None:
        row = self.db.query_one(
            "SELECT * FROM protocol_version ORDER BY version DESC LIMIT 1"
        )
        return self._to_version(row) if row else None

    def versions(self) -> list[ProtocolVersion]:
        return [
            self._to_version(r)
            for r in self.db.query("SELECT * FROM protocol_version ORDER BY version")
        ]

    @staticmethod
    def _to_version(row: Any) -> ProtocolVersion:
        return ProtocolVersion(
            id=row["id"],
            version=row["version"],
            content=row["content"],
            content_hash=row["content_hash"],
            locked_at=row["locked_at"],
            locked_by=row["locked_by"],
        )

    def is_locked(self) -> bool:
        return self.current() is not None

    def lock(
        self,
        *,
        actor: str | None = None,
        reason: str = "",
        content: str | None = None,
    ) -> ProtocolVersion:
        """Freeze the current draft as the next numbered version.

        Locking an unchanged protocol is refused rather than silently creating
        an identical version: an amendment log full of no-op entries hides the
        changes that matter.
        """
        actor = actor or self.audit.actor
        content = content if content is not None else self.draft()
        content_hash = sha256_text(content)
        previous = self.current()

        if previous is not None and previous.content_hash == content_hash:
            raise ProtocolError(
                f"The protocol is unchanged since version {previous.version} "
                f"({previous.short_hash}). There is nothing to amend."
            )
        if previous is not None and not reason.strip():
            raise ProtocolError(
                "Amending a locked protocol needs a reason. It is recorded in "
                "the amendment log and reported in the Methods section."
            )

        version_number = (previous.version + 1) if previous else 1
        now = utc_now()
        with self.db.transaction():
            version_id = self.db.insert(
                "protocol_version",
                {
                    "version": version_number,
                    "content": content,
                    "content_hash": content_hash,
                    "locked_at": now,
                    "locked_by": actor,
                },
            )
            after_data = self.data_seen()
            if previous is not None:
                self.db.insert(
                    "amendment",
                    {
                        "from_version": previous.id,
                        "to_version": version_id,
                        "changed_at": now,
                        "reason": reason.strip(),
                        "after_data_seen": 1 if after_data else 0,
                        "diff": self.diff(previous.content, content),
                    },
                )
            self.audit.log(
                "project",
                "protocol_locked" if previous is None else "protocol_amended",
                entity="protocol_version",
                entity_id=version_id,
                detail={
                    "version": version_number,
                    "content_hash": content_hash,
                    "reason": reason.strip(),
                    "after_data_seen": after_data,
                },
                actor=actor,
            )
        return self._to_version(
            self.db.query_one("SELECT * FROM protocol_version WHERE id = ?", (version_id,))
        )

    # -- change detection --------------------------------------------------

    def data_seen(self) -> bool:
        """True once any record has been imported.

        This is what makes `after_data_seen` an observation rather than a
        self-report: a reviewer cannot amend a protocol mid-review and claim
        the change was pre-registered.
        """
        return bool(self.db.scalar("SELECT COUNT(*) FROM record") or 0)

    def has_unlocked_changes(self) -> bool:
        """True when the form no longer matches the locked version."""
        current = self.current()
        if current is None:
            return True
        return sha256_text(self.draft()) != current.content_hash

    @staticmethod
    def diff(before: str, after: str) -> str:
        return "\n".join(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="previous",
                tofile="amended",
                lineterm="",
                n=2,
            )
        )

    def pending_diff(self) -> str:
        """What locking now would record. Empty when nothing has changed."""
        current = self.current()
        if current is None:
            return ""
        return self.diff(current.content, self.draft())

    # -- the amendment log -------------------------------------------------

    def amendments(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT a.*, pv_from.version AS from_number, pv_to.version AS to_number "
            "FROM amendment a "
            "LEFT JOIN protocol_version pv_from ON pv_from.id = a.from_version "
            "LEFT JOIN protocol_version pv_to ON pv_to.id = a.to_version "
            "ORDER BY a.id"
        )
        return [dict(r) for r in rows]

    def verify(self) -> list[str]:
        """Recompute every stored content hash. Empty means nothing was edited."""
        problems: list[str] = []
        for version in self.versions():
            if sha256_text(version.content) != version.content_hash:
                problems.append(
                    f"Protocol version {version.version} does not match its "
                    "stored hash; the content has been altered."
                )
        return problems

    def export(self, path) -> Any:
        """Write the locked protocol to a Markdown file in the project."""
        from pathlib import Path

        current = self.current()
        if current is None:
            raise ProtocolError("The protocol has not been locked yet.")
        path = Path(path)
        header = (
            f"<!-- Locked {current.locked_at} by {current.locked_by}. "
            f"Version {current.version}. SHA-256 {current.content_hash} -->\n\n"
        )
        path.write_text(header + current.content, encoding="utf-8")
        self.audit.log(
            "project",
            "protocol_exported",
            entity="protocol_version",
            entity_id=current.id,
            detail={"path": path.name, "version": current.version},
        )
        return path

    def summary(self) -> dict[str, Any]:
        current = self.current()
        return {
            "locked": current is not None,
            "version": current.version if current else 0,
            "content_hash": current.content_hash if current else "",
            "locked_at": current.locked_at if current else "",
            "locked_by": current.locked_by if current else "",
            "has_unlocked_changes": self.has_unlocked_changes(),
            "data_seen": self.data_seen(),
            "n_amendments": len(self.amendments()),
        }
