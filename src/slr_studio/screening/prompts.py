"""The prompts, and what each reviewer is allowed to see.

Three things are deliberate.

**Blinding.** A screener is given the protocol and one record. It is not told
what the other screener said, what the database was, how many records have
been included so far, or what the reviewer hopes to find. Two screeners differ
because their instructions differ, not because the sampler did -- temperature
is 0 throughout.

**No database name.** The source database is withheld from the screeners.
Knowing a record came from Scopus rather than a dissertations index invites a
prestige judgement that the protocol does not authorise.

**A version string.** `PROMPT_VERSION` is stored with every run and bumped
whenever the wording below changes, because a run screened under different
wording is not comparable with one screened under this.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = [
    "PROMPT_VERSION",
    "PERSONAS",
    "screener_system",
    "adjudicator_system",
    "qc_system",
    "second_look_system",
    "second_look_user",
    "render_record",
    "screening_user",
    "adjudication_user",
]

#: 1.1 (10 Nov 2026): "unclear" narrowed. In the generative-AI-in-travel case
#: study, 12% of records came back unclear, many of them clearly off-topic
#: records that failed an early code. Screeners now exclude on any clearly
#: failed code first and must name the criterion an unclear answer is about.
PROMPT_VERSION = "screen-1.1"

_SHARED_RULES = """
Rules you must follow:
- Judge only what is in the record below. Do not use outside knowledge about
  the authors, the journal or the study.
- Work through the exclusion codes in order. If the record clearly fails one,
  exclude with that code, even when other details are not reported: a record
  that is plainly not about the review's topic is excluded, not unclear.
- Answer "unclear" only when no code is clearly failed AND the record leaves a
  specific criterion undecidable (for example, the population is not stated).
  Name that criterion in your reason. Do not guess, and do not use "unclear"
  for a detail that eligible studies normally report only in the full text.
- To exclude, name the FIRST code in the ordered list that the record fails.
- Quote at most fifteen words from the title or abstract to support your
  answer. Never invent a quotation. If nothing in the record supports your
  answer, leave the quote empty.
- Give one sentence of reasoning.

Answer with a single JSON object and nothing else:
{"decision": "include" | "exclude" | "unclear",
 "exclusion_code": "<code, or empty when including>",
 "reason": "<one sentence>",
 "quote": "<up to fifteen words from the record, or empty>"}
""".strip()

#: Two reviewer stances. They differ in what they do with an ambiguous record,
#: which is where screeners genuinely disagree, rather than in tone.
PERSONAS: dict[str, dict[str, str]] = {
    "screener_a": {
        "label": "Inclusive screener",
        "stance": (
            "You are the first of two independent screeners. At title and "
            "abstract your job is to avoid losing an eligible study: an "
            "irrelevant record you let through is caught later at full text, "
            "but a relevant one you reject is never seen again. When the "
            "record is genuinely ambiguous, prefer to include it rather than "
            "exclude it. Exclude only when the record clearly fails a "
            "criterion."
        ),
    },
    "screener_b": {
        "label": "Exacting screener",
        "stance": (
            "You are the second of two independent screeners, working "
            "separately from the first. Your job is to hold the protocol's "
            "criteria strictly: a record earns inclusion by showing it meets "
            "them, not by failing to contradict them. When the record is "
            "genuinely ambiguous, answer \"unclear\" rather than assuming in "
            "its favour."
        ),
    },
}


def _criteria_block(protocol_text: str, exclusion_codes: list[dict[str, Any]]) -> str:
    lines = ["THE PROTOCOL", "", protocol_text.strip(), "", "EXCLUSION CODES, IN ORDER", ""]
    if exclusion_codes:
        for code in exclusion_codes:
            definition = code.get("definition") or ""
            lines.append(f"{code['position']}. {code['code']} — {code['label']}: {definition}")
    else:
        lines.append("(none defined)")
    return "\n".join(lines)


def screener_system(
    role: str, protocol_text: str, exclusion_codes: list[dict[str, Any]]
) -> str:
    persona = PERSONAS.get(role) or PERSONAS["screener_a"]
    return "\n\n".join(
        [
            "You are screening titles and abstracts for a systematic review "
            "that follows PRISMA 2020.",
            persona["stance"],
            _criteria_block(protocol_text, exclusion_codes),
            _SHARED_RULES,
        ]
    )


def adjudicator_system(protocol_text: str, exclusion_codes: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            "You are adjudicating a disagreement between two independent "
            "screeners on a systematic review that follows PRISMA 2020.",
            "Both screeners saw the same protocol and the same record, and "
            "neither saw the other's answer. Decide the record yourself "
            "against the protocol. Do not split the difference, and do not "
            "defer to a screener because it sounded confident. If the record "
            "genuinely does not settle the question, answer \"unclear\" and "
            "it will go to a human.",
            _criteria_block(protocol_text, exclusion_codes),
            _SHARED_RULES,
            'Add one more field: {"sided_with": "screener_a" | "screener_b" | "neither"}.',
        ]
    )


def second_look_system(protocol_text: str, exclusion_codes: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            "You are taking a second look at a record that was left unclear at "
            "title and abstract screening, for a systematic review that "
            "follows PRISMA 2020.",
            "An unclear record goes on to full text, which costs the reviewer "
            "a document to find and read. Read the record afresh against the "
            "protocol. If it clearly fails an exclusion code, exclude it with "
            "the first code it fails. If it clearly meets the criteria, include "
            "it. Keep it unclear only when a specific criterion genuinely "
            "cannot be judged from the title and abstract, and name that "
            "criterion. Earlier answers are shown for context; do not defer "
            "to them.",
            _criteria_block(protocol_text, exclusion_codes),
            _SHARED_RULES,
        ]
    )


def second_look_user(record: dict[str, Any], earlier: list[dict[str, Any]]) -> str:
    lines = [render_record(record), "EARLIER ANSWERS"]
    for answer in earlier:
        code = answer.get("exclusion_code") or "—"
        lines.append(
            f"{answer.get('label', 'A reviewer')}: {answer.get('decision')} (code {code}); "
            f"{answer.get('reason', '')}"
        )
    if len(lines) == 2:
        lines.append("(none recorded)")
    lines.append("Decide this record yourself against the protocol.")
    return "\n\n".join(lines)


def qc_system(protocol_text: str, exclusion_codes: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            "You are running a quality-control check on a systematic review "
            "that follows PRISMA 2020.",
            "Two screeners both excluded the record below. Exclusions are "
            "never revisited once screening is finished, so a wrong one is "
            "permanent. Read the record afresh and say whether excluding it "
            "was right. Answer \"include\" or \"unclear\" if you have any "
            "doubt; it will be sent to a human, not acted on automatically.",
            _criteria_block(protocol_text, exclusion_codes),
            _SHARED_RULES,
        ]
    )


def render_record(record: dict[str, Any]) -> str:
    """What a screener sees of one record.

    The source database, the deduplication history and the record's internal
    id are all withheld; none of them bears on eligibility.
    """
    authors = record.get("authors") or []
    if isinstance(authors, str):
        try:
            authors = json.loads(authors)
        except ValueError:
            authors = [authors]
    keywords = record.get("keywords") or []
    if isinstance(keywords, str):
        try:
            keywords = json.loads(keywords)
        except ValueError:
            keywords = [keywords]

    abstract = (record.get("abstract") or "").strip()
    parts = [
        "RECORD",
        "",
        f"Title: {record.get('title') or '(no title)'}",
        f"Year: {record.get('year') or 'not stated'}",
        f"Publication: {record.get('journal') or 'not stated'}",
        f"Type: {record.get('doc_type') or 'not stated'}",
        f"Language: {record.get('language') or 'not stated'}",
    ]
    if authors:
        shown = "; ".join(str(a) for a in authors[:8])
        if len(authors) > 8:
            shown += f"; and {len(authors) - 8} more"
        parts.append(f"Authors: {shown}")
    if keywords:
        parts.append("Keywords: " + "; ".join(str(k) for k in keywords[:15]))
    parts += ["", "Abstract:", abstract if abstract else "(no abstract in this record)"]
    return "\n".join(parts)


def screening_user(record: dict[str, Any]) -> str:
    return render_record(record) + "\n\nScreen this record against the protocol."


def adjudication_user(
    record: dict[str, Any], answer_a: dict[str, Any], answer_b: dict[str, Any]
) -> str:
    def describe(label: str, answer: dict[str, Any]) -> str:
        code = answer.get("exclusion_code") or "—"
        return (
            f"{label} said: {answer.get('decision')} (code {code})\n"
            f"  reason: {answer.get('reason', '')}\n"
            f"  quote: {answer.get('quote', '') or '—'}"
        )

    return "\n\n".join(
        [
            render_record(record),
            "THE TWO SCREENERS",
            describe("Screener A", answer_a),
            describe("Screener B", answer_b),
            "Decide this record yourself against the protocol.",
        ]
    )
