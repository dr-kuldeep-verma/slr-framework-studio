"""Prompts for the synthesis stage.

The organising constraint is that this stage never reads a report. It reads
labels, counts and the codebook the human approved. That is deliberate: by
the time the synthesis runs, what each report said has been extracted,
quoted and page-checked, and re-reading it here would be a second,
unrecorded extraction whose disagreements with the first nobody would see.

So the prompts below are narrow. The codebook call sees the objectives and
the list of labels. The mapping call sees one label and the approved
codebook. The linkage call sees two study descriptions. The contradiction
call sees the counts and the study characteristics on each side. None of
them is in a position to invent a study, which is the point.
"""

from __future__ import annotations

from typing import Any, Sequence

from .schemas import RESIDUAL_CONSTRUCT

__all__ = [
    "PROMPT_VERSION",
    "codebook_system",
    "codebook_user",
    "mapping_system",
    "mapping_user",
    "linkage_system",
    "linkage_user",
    "contradiction_system",
    "contradiction_user",
    "render_codebook",
]

PROMPT_VERSION = "synth-1.0"


# -- the codebook ----------------------------------------------------------

_CODEBOOK_RULES = """
You are building a construct codebook for a systematic review.

A construct is a thing the studies measured. The labels below are the words
each set of authors used; several of them may be the same construct under
different names, and two identical names may be different constructs.

Rules:
- Group labels by WHAT WAS MEASURED, not by what they are called. "Perceived
  fairness of benefit distribution" and "distributive justice perceptions"
  are one construct. "Participation" meaning attendance at meetings and
  "participation" meaning decision-making power are two.
- Give every construct a definition, at least one `includes` entry saying
  what counts, and at least one `excludes` entry saying what does not. A
  construct that excludes nothing will swallow every label.
- Prefer the level of abstraction the review's objectives need. Too fine and
  nothing will ever accumulate enough datasets to grade; too coarse and the
  grades will be meaningless.
- Propose between 4 and 25 constructs. Do not propose a construct for a
  label that appears once unless it is central to the objectives.
- Do not propose a residual "other" category. One exists already.
- You are proposing, not deciding. A human reads and edits this before
  anything is mapped to it.
"""


# The deductive variant. The reviewer has already decided what the
# constructs are; the model is writing the boundaries of constructs it did
# not choose, which is a narrower job and has to be described as one. The
# engine does not rely on these instructions being followed -- it discards
# anything that is not on the reviewer's list -- but a model that is told
# the list is closed invents far less, and what it does invent is a fact
# worth recording rather than a surprise.
_A_PRIORI_RULES = """
You are writing definitions for a construct codebook that has already been
decided. The reviewer specified these constructs in advance, from theory,
and committed to them in the review's locked protocol.

Rules:
- The list of constructs is CLOSED. Do not add a construct. Do not drop,
  rename, merge or split one. Return exactly the names you were given,
  spelled the way you were given them.
- For each construct, write a definition, at least one `includes` entry
  saying what counts as an instance of it, and at least one `excludes`
  entry saying what does not. Write them so that another coder reading
  only your text would place the same label in the same construct.
- Where the reviewer supplied their own definition, keep its meaning. You
  may sharpen the boundary; you may not substitute a different construct
  for the one they described.
- Use the label inventory to draw the boundaries where the evidence
  actually falls, and to see what is likely to be contested.
- Some labels will fit none of these constructs. That is an expected and
  reportable outcome of a deductive review: those labels go to the
  residual category. Do not widen a construct to swallow them, and do not
  propose a new construct to house them.
- You are proposing definitions, not deciding them. A human reads and
  edits this before anything is mapped to it.
"""


def codebook_system(
    protocol: str,
    objectives: str,
    *,
    a_priori: Sequence[tuple[str, str]] | None = None,
) -> str:
    """The system prompt for the codebook step.

    With *a_priori* the review is deductive and the constructs are the
    reviewer's; without it the review is inductive and the constructs are
    derived from the labels. The two prompts are deliberately different
    documents rather than one with a clause bolted on, because the job is
    not the same job.
    """
    if a_priori:
        declared = "\n".join(
            f"- {name}" + (f" — the reviewer's definition: {definition}" if definition else "")
            for name, definition in a_priori
        )
        return (
            "You are writing the definitions for a construct codebook that "
            "the reviewer has already specified.\n"
            f"{_A_PRIORI_RULES}\n"
            f"The {len(a_priori)} constructs you must return, and only "
            f"these:\n{declared}\n\n"
            "The review's objectives:\n"
            f"{objectives or '(none recorded)'}\n\n"
            "The review protocol, which fixes its scope:\n"
            f"{protocol}\n"
        )
    return (
        "You are proposing a construct codebook for a systematic review.\n"
        f"{_CODEBOOK_RULES}\n"
        "The review's objectives:\n"
        f"{objectives or '(none recorded)'}\n\n"
        "The review protocol, which fixes its scope:\n"
        f"{protocol}\n"
    )


def codebook_user(
    labels: Sequence[dict[str, Any]],
    *,
    max_labels: int = 400,
    a_priori: bool = False,
) -> str:
    """The label inventory, commonest first so truncation drops the rarest."""
    ordered = sorted(labels, key=lambda r: (-int(r.get("n_uses", 0)), str(r["label"])))
    shown = ordered[:max_labels]
    lines = [
        f"- {row['label']} ({row['role']}, used {row['n_uses']}x)" for row in shown
    ]
    text = "\n".join(lines)
    if len(ordered) > len(shown):
        text += f"\n- ... and {len(ordered) - len(shown)} rarer labels"
    instruction = (
        "Write the definition, includes and excludes for each of the "
        "constructs you were given. Return those constructs and no others."
        if a_priori
        else "Propose the construct codebook."
    )
    return (
        f"The {len(ordered)} distinct labels the extraction recorded:\n\n"
        f"{text}\n\n"
        f"{instruction}"
    )


# -- mapping ---------------------------------------------------------------

_MAPPING_RULES = """
You are mapping author labels onto an approved construct codebook.

Rules:
- Map by WHAT WAS MEASURED, not by vocabulary. A label that shares a word
  with a construct does not belong to it for that reason; a label that shares
  no word may belong to it.
- `measured_as` is your reading of what the authors actually measured, in
  your own words. The reviewer checking this reads that, not the label.
- You may only use a construct from the codebook below, or "other". Do not
  invent a construct. If nothing fits, answer "other" and say why in the
  rationale.
- Set `contestable` to true, with a reason, whenever a reasonable reviewer
  could place the label differently: when it straddles two constructs, when
  the label is too vague to tell, or when you are choosing between two
  constructs on a fine distinction. Flagging it sends it to a human. Not
  flagging a doubtful mapping is the costlier mistake, because an unflagged
  mapping is counted as though it were certain.
"""


def render_codebook(constructs: Sequence[dict[str, Any]]) -> str:
    import json

    blocks = []
    for row in constructs:
        includes = row.get("includes") or "[]"
        excludes = row.get("excludes") or "[]"
        if isinstance(includes, str):
            includes = json.loads(includes or "[]")
        if isinstance(excludes, str):
            excludes = json.loads(excludes or "[]")
        block = [f"### {row['name']}", row.get("definition", "")]
        if includes:
            block.append("Includes: " + "; ".join(includes))
        if excludes:
            block.append("Excludes: " + "; ".join(excludes))
        blocks.append("\n".join(p for p in block if p))
    return "\n\n".join(blocks)


def mapping_system(constructs: Sequence[dict[str, Any]]) -> str:
    return (
        "You are mapping labels onto a construct codebook.\n"
        f"{_MAPPING_RULES}\n"
        "The approved codebook:\n\n"
        f"{render_codebook(constructs)}\n\n"
        f'The residual code "{RESIDUAL_CONSTRUCT}" is also available.\n'
    )


def mapping_user(labels: Sequence[dict[str, Any]]) -> str:
    lines = []
    for row in labels:
        context = row.get("example") or ""
        line = f"- {row['label']} (used as {row['role']}, {row['n_uses']}x)"
        if context:
            line += f"\n  seen in: {context}"
        lines.append(line)
    return (
        "Map each of these labels:\n\n"
        + "\n".join(lines)
        + "\n\nReturn one mapping per label, using the label exactly as given."
    )


# -- dataset linkage -------------------------------------------------------

_LINKAGE_RULES = """
You are judging whether two reports describe the same body of fieldwork.

This matters because a research team that publishes one survey four times
would otherwise count as four independent studies and out-vote four genuinely
separate ones.

Rules:
- Say "yes" only when the two reports plainly describe one data collection:
  the same participants, the same site, the same period. Overlapping authors
  alone is not enough — teams run many studies. The same country alone is not
  enough.
- Say "no" when they are clearly different fieldwork, even by the same team.
- Say "unclear" when the reports do not give you enough to tell. "Unclear" is
  a useful answer here; a human decides either way afterwards.
- List the specific signals you matched on in `shared_signals`.
"""


def linkage_system() -> str:
    return (
        "You are checking whether two studies share a dataset.\n"
        f"{_LINKAGE_RULES}"
    )


def _study_block(study: dict[str, Any], heading: str) -> str:
    fields = [
        ("Title", study.get("title")),
        ("Authors", study.get("authors")),
        ("Year", study.get("year")),
        ("Country", study.get("country")),
        ("Setting", study.get("setting")),
        ("Population", study.get("population")),
        ("Design", study.get("design")),
        ("Sampling", study.get("sampling")),
        ("Sample size", study.get("sample_size") or study.get("sample_size_text")),
        ("Time horizon", study.get("time_horizon")),
    ]
    lines = [f"{name}: {value}" for name, value in fields if value not in (None, "")]
    return f"{heading}\n" + "\n".join(lines)


def linkage_user(a: dict[str, Any], b: dict[str, Any]) -> str:
    return (
        _study_block(a, "STUDY A")
        + "\n\n"
        + _study_block(b, "STUDY B")
        + "\n\nDo these two reports draw on the same dataset?"
    )


# -- contradictions --------------------------------------------------------

_CONTRADICTION_RULES = """
You are explaining why the studies behind one link disagree.

You are given the two sides as they were counted: which datasets found a
positive relationship, which found a negative one, and what is known about
each study. You are NOT given the reports themselves.

Rules:
- Explain the disagreement from the study characteristics you can see:
  `context` (different places, populations or periods), `measurement`
  (the construct was operationalised differently), `method` (different
  designs or analyses), `sample` (size, sampling or composition), or
  `unclear` when the characteristics shown do not explain it.
- "unclear" is a legitimate answer and often the honest one. Do not
  manufacture an explanation out of a difference that happens to be visible.
- You may suggest ONE remedy: a `moderator` that would explain when the link
  holds, or a `construct_split` if the two sides are really measuring two
  different things. Suggest `none` if neither is warranted.
- Never refer to a study, finding or statistic that is not in the data below.
"""


def contradiction_system() -> str:
    return (
        "You are explaining a contested finding in a systematic review.\n"
        f"{_CONTRADICTION_RULES}"
    )


def contradiction_user(
    predictor: str,
    outcome: str,
    positive: Sequence[dict[str, Any]],
    negative: Sequence[dict[str, Any]],
    null: Sequence[dict[str, Any]] = (),
) -> str:
    def side(rows: Sequence[dict[str, Any]], heading: str) -> str:
        if not rows:
            return f"{heading}: none"
        lines = []
        for row in rows:
            bits = [
                str(row.get("country") or "country not stated"),
                str(row.get("design_family") or "design unclear"),
                f"n = {row['sample_size']}" if row.get("sample_size") else "",
                f"measured as: {row['measured_as']}" if row.get("measured_as") else "",
                f"appraisal concern: {row['appraisal_concern']}"
                if row.get("appraisal_concern")
                else "",
            ]
            lines.append("- " + "; ".join(b for b in bits if b))
        return f"{heading} ({len(rows)}):\n" + "\n".join(lines)

    return (
        f"The link: {predictor} → {outcome}\n\n"
        + side(positive, "Datasets finding a POSITIVE relationship")
        + "\n\n"
        + side(negative, "Datasets finding a NEGATIVE relationship")
        + "\n\n"
        + side(null, "Datasets finding NO relationship")
        + "\n\nWhy do they disagree?"
    )
