"""Question frameworks, built in and reviewer-defined.

A question framework is the shape a review's question and eligibility
criteria are written in: PICO for an effectiveness review, PCC for a
scoping review, SPIDER for a qualitative one. It is a different thing from
the *theoretical* framework the review builds, which is the reviewer's own
and is never chosen from a list.

This is a catalogue rather than a dropdown because of what the name is
used for. The Gap Finder reads a framework's stages and reports which
transitions between them no graded link covers -- for SOBC, whether
anything connects stimulus to organism, organism to behaviour, and so on.
A framework the catalogue does not know has no stages, so that whole class
of gap comes back empty. Before this module the column had a CHECK
constraint holding it to four names, which made the empty case
unreachable; allowing reviewers their own frameworks makes it reachable,
so a reviewer-defined framework must carry its stages, and a framework
without them has to be reported rather than silently detecting less.

A note on what "transition" means here. For SOBC and CIMO the stages are a
causal chain and a missing transition is a real theoretical gap. For PICO
and PCC they are the facets of a question, and a missing transition means
the review has evidence about one facet and not the pair -- a coverage
finding rather than a causal one. The application has always treated them
the same way; the wording of the gap says which stages are uncovered
rather than asserting a mechanism, which is true of both readings.
"""

from __future__ import annotations

import re

__all__ = [
    "BUILT_IN",
    "FrameworkError",
    "built_in_names",
    "format_stages",
    "normalise_name",
    "parse_stages",
    "validate_definition",
]


class FrameworkError(ValueError):
    """A framework definition that cannot be stored or used."""


#: The frameworks the application knows without being told. Stages are in
#: the order the framework states them, folded to lower case because that is
#: the form reports are coded in and a stage that does not match is a stage
#: that is never found.
#:
#: The first four are the original set and their stages are unchanged: there
#: are reviews in progress whose coded reports were matched against exactly
#: these strings.
BUILT_IN: dict[str, tuple[str, ...]] = {
    # Scoping reviews (JBI).
    "PCC": ("population", "concept", "context"),
    # Effectiveness reviews and their variants.
    "PICO": ("population", "intervention", "comparison", "outcome"),
    "PICOS": (
        "population", "intervention", "comparison", "outcome", "study design",
    ),
    "PICOT": (
        "population", "intervention", "comparison", "outcome", "time",
    ),
    # Exposure rather than intervention: epidemiology, environmental health.
    "PECO": ("population", "exposure", "comparison", "outcome"),
    "PEO": ("population", "exposure", "outcome"),
    # Qualitative and mixed-method reviews.
    "SPIDER": (
        "sample", "phenomenon", "design", "evaluation", "research type",
    ),
    # Services and policy.
    "SPICE": (
        "setting", "perspective", "intervention", "comparison", "evaluation",
    ),
    "ECLIPSE": (
        "expectation", "client group", "location", "impact", "professionals",
        "service",
    ),
    # Management and organisational research: an explicit causal chain.
    "CIMO": ("context", "intervention", "mechanism", "outcome"),
    # Behavioural science: the chain the original four were built around.
    "SOBC": ("stimulus", "organism", "behaviour", "consequence"),
    # Prevalence reviews (JBI).
    "CoCoPop": ("condition", "context", "population"),
    # Diagnostic test accuracy.
    "PIRD": (
        "population", "index test", "reference test", "diagnosis of interest",
    ),
}

#: What a reviewer might type between two stages.
_SEPARATORS = re.compile(r"\s*(?:->|→|⇒|>|\||,|;|\n)\s*")


def built_in_names() -> list[str]:
    return list(BUILT_IN)


def normalise_name(name: str) -> str:
    """The form two framework names are compared in.

    Case and surrounding space only. Nothing cleverer: "CoCoPop" and
    "cocopop" are the same framework, but a reviewer who writes
    "Resident response" and "Resident responses" means two different
    things and should be allowed both.
    """
    return " ".join((name or "").split()).casefold()


def parse_stages(text: str) -> tuple[str, ...]:
    """Read an ordered list of stages out of whatever the reviewer typed.

    Arrows, commas, semicolons, pipes or one per line all work, because a
    person asked for "antecedent → mechanism → outcome" types the arrow if
    their keyboard has one and a comma if it does not, and being told off
    for the difference teaches them nothing.

    Order is preserved, since the order *is* the framework. A stage named
    twice is kept once, at its first position.
    """
    out: list[str] = []
    seen: set[str] = set()
    for piece in _SEPARATORS.split(text or ""):
        stage = " ".join(piece.split()).casefold().strip("-–—→ ")
        if not stage or stage in seen:
            continue
        seen.add(stage)
        out.append(stage)
    return tuple(out)


def format_stages(stages: tuple[str, ...] | list[str]) -> str:
    """How a framework's stages are shown back to a reviewer."""
    return " → ".join(stages)


def validate_definition(
    name: str,
    stages: tuple[str, ...] | list[str],
    *,
    existing: tuple[str, ...] | list[str] = (),
) -> None:
    """Refuse a definition that would not work, and say why.

    *existing* is the normalised names already defined on this project.
    """
    clean = " ".join((name or "").split())
    if not clean:
        raise FrameworkError("A framework needs a name.")
    if normalise_name(clean) in {normalise_name(n) for n in BUILT_IN}:
        raise FrameworkError(
            f"{clean} is a built-in framework and cannot be redefined. If "
            "yours is a variant of it, give it a name of its own so that a "
            "reader of the protocol can tell the two apart."
        )
    if normalise_name(clean) in {normalise_name(n) for n in existing}:
        raise FrameworkError(f"A framework called {clean} is already defined.")
    if len(stages) < 2:
        raise FrameworkError(
            "A framework needs at least two stages: the Gap Finder reports "
            "the transitions between them, and one stage has no transition. "
            "Write them in order, for example: "
            "antecedent → mechanism → outcome"
        )
