"""What the framework builder asks the model, and what it shows it.

The model is shown the graded inventory and nothing else that could be
mistaken for evidence: no abstracts, no quotations, no ungraded relationships.
It is told that the only links it may cite are the ones in front of it, and
the engine checks afterwards regardless, because a prompt is a request and a
check is a guarantee.
"""

from __future__ import annotations

from typing import Any, Sequence

__all__ = [
    "PROMPT_VERSION",
    "proposition_system",
    "proposition_user",
    "render_inventory",
    "theme_system",
    "theme_user",
]

#: Bumped when a prompt changes, so cached answers from an older wording are
#: not reused.
PROMPT_VERSION = "framework-1"


def theme_system(protocol: str, objectives: str) -> str:
    return f"""You group constructs into themes for a systematic review's
evidence maps. One figure will be drawn per theme, so a theme is a set of
constructs that belong on the same page: related closely enough that a reader
sees one argument, few enough that the figure stays legible.

Rules:
- Use only the construct names given. Do not invent, rename or merge them.
- Every construct must appear in exactly one theme.
- Aim for three to six constructs per theme. Prefer more themes to a crowded
  one.
- Name a theme for what it is about, not "Theme 1".

The review's protocol:
{protocol}

Its objectives:
{objectives}

Answer as JSON: {{"themes": [{{"name": ..., "description": ...,
"constructs": [...]}}]}}"""


def theme_user(constructs: Sequence[dict[str, Any]]) -> str:
    lines = ["The constructs in the graded inventory:", ""]
    for construct in constructs:
        definition = (construct.get("definition") or "").strip()
        lines.append(
            f"- {construct['name']}"
            + (f" — {definition}" if definition else "")
        )
    return "\n".join(lines)


def proposition_system(
    protocol: str,
    theories: Sequence[str],
    themes: Sequence[dict[str, Any]],
    grades: Sequence[str],
) -> str:
    theory_list = (
        "\n".join(f"- {name}" for name in theories)
        if theories
        else "- (none listed; say so rather than inventing one)"
    )
    theme_list = (
        "\n".join(f"- {t['name']}: {t.get('description', '')}" for t in themes)
        if themes
        else "- (none)"
    )
    return f"""You draft propositions for the theoretical framework of a
systematic review. A proposition is a claim the review's evidence supports,
stated so that it could be shown to be wrong.

Each proposition must have:
- a statement, in one sentence;
- a mechanism saying *why* the relationship holds, drawn from the theories
  listed below and named as such;
- the moderators that would change its strength;
- the boundary conditions outside which it is not claimed to hold;
- a falsifying test: an observation that would show it to be false;
- the links it rests on, cited by id from the inventory you are given.

Hard rules:
- Cite only link ids that appear in the inventory below. A proposition citing
  anything else is discarded in full, not corrected.
- Do not cite a study, a record, a page or an author. Only link ids.
- Do not state a relationship the inventory does not contain. If you want to
  propose one anyway, set element_kind to "untested_pathway" and cite nothing;
  it will be drawn as untested rather than as a finding.
- Use element_kind "proposed_mechanism" for an explanatory path you are
  suggesting rather than one the evidence shows.
- An "evidenced_link" proposition must cite at least one link.

Theories the reviewer listed:
{theory_list}

The themes:
{theme_list}

Grades present in the inventory, strongest first: {", ".join(grades) or "none"}.
A tentative or contested link is weak evidence and a proposition resting only
on those should say so in its boundary conditions.

The review's protocol:
{protocol}

Answer as JSON: {{"propositions": [{{"statement": ..., "mechanism": ...,
"theories": [...], "moderators": ..., "boundary_conditions": ...,
"falsifying_test": ..., "element_kind": ..., "cited_links": ["L1"],
"theme": ...}}]}}"""


def render_inventory(links: Sequence[dict[str, Any]]) -> str:
    """The graded inventory as the model sees it: ids, constructs, grades."""
    lines = [
        "The graded inventory. These link ids are the only evidence you may",
        "cite.",
        "",
    ]
    for link in links:
        interval = ""
        if link.get("ci_low") is not None:
            interval = (
                f", {round(link['ci_low'] * 100)}–{round(link['ci_high'] * 100)}%"
            )
        lines.append(
            f"- L{link['id']}: {link['predictor_name']} → {link['outcome_name']}"
            f" | {link['direction']} | {link['grade']}"
            f" | {link['n_datasets']} dataset(s)"
            f", {link.get('n_countries', 0)} country(ies){interval}"
        )
    return "\n".join(lines)


def proposition_user(links: Sequence[dict[str, Any]], theme_name: str = "") -> str:
    heading = (
        f"Draft propositions for the theme {theme_name!r}."
        if theme_name
        else "Draft propositions for the framework."
    )
    return f"{heading}\n\n{render_inventory(links)}"
