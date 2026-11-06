"""The eligibility prompt, and how a report is laid before the assessor.

A full text is far longer than an abstract, so two decisions matter here.

**Pages are labelled.** The report is presented page by page, each one marked
with its number in the PDF, so a quotation can say where it came from and the
app can check it. An unlabelled wall of text makes page citation a guess.

**Long reports are trimmed, visibly.** A fifty-page paper with appendices
costs more to send than the eligibility question needs. The opening pages and
a sample of the rest are sent, and the prompt says plainly that pages were
omitted and which ones, so the assessor can answer "unclear" rather than
assume the missing pages held nothing.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PROMPT_VERSION",
    "assessor_system",
    "render_report",
    "eligibility_user",
]

PROMPT_VERSION = "fulltext-1.0"

_RULES = """
Rules you must follow:
- Judge only this report against the protocol. Do not use outside knowledge
  about the authors, the journal or the study.
- Work down the exclusion codes in order and name the FIRST one the report
  fails. Do not skip to a later code because it feels more important.
- Every decision must rest on something in the report. Copy up to twenty-five
  words from it as `quote`, and give the PDF page number you copied them from
  as `quote_page`. Pages are labelled in the text below.
- The quotation is checked against that page automatically. Copy it exactly;
  do not tidy it, paraphrase it, or reconstruct it from memory. A quotation
  that cannot be found is treated as a failure of the assessment.
- If the report does not settle the question, or if the pages you would need
  were not included, answer "unclear" and say so. Do not guess.

Answer with a single JSON object and nothing else:
{"decision": "include" | "exclude" | "unclear",
 "exclusion_code": "<code, or empty when including>",
 "reason": "<one or two sentences>",
 "quote": "<up to twenty-five words copied exactly from the report>",
 "quote_page": <the PDF page number the quotation is on>}
""".strip()


def _criteria_block(protocol_text: str, exclusion_codes: list[dict[str, Any]]) -> str:
    lines = ["THE PROTOCOL", "", protocol_text.strip(), "", "EXCLUSION CODES, IN ORDER", ""]
    if exclusion_codes:
        for code in exclusion_codes:
            definition = code.get("definition") or ""
            lines.append(
                f"{code['position']}. {code['code']} — {code['label']}: {definition}"
            )
    else:
        lines.append("(none defined)")
    return "\n".join(lines)


def assessor_system(protocol_text: str, exclusion_codes: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        [
            "You are assessing the full text of a report for eligibility in a "
            "systematic review that follows PRISMA 2020.",
            "This is the last stage at which a study can be excluded. An "
            "exclusion here is final and must be defensible: it will be "
            "reported, with its reason, in the PRISMA flow diagram.",
            _criteria_block(protocol_text, exclusion_codes),
            _RULES,
        ]
    )


def render_report(
    pages: list[dict[str, Any]],
    *,
    max_chars: int = 60_000,
    head_pages: int = 6,
) -> tuple[str, list[int]]:
    """Lay out the report for the assessor.

    Returns the text and the list of page numbers actually included. The
    opening pages carry the title, abstract, methods and usually enough to
    decide eligibility, so they are kept whole; later pages are added until
    the budget runs out. Whatever is dropped is announced in the text.
    """
    if not pages:
        return "(no text could be read from this report)", []

    blocks: list[tuple[int, str]] = []
    budget = max_chars

    ordered = sorted(pages, key=lambda p: p["page_no"])
    head = ordered[:head_pages]
    tail = ordered[head_pages:]

    for page in head + tail:
        text = (page.get("text") or "").strip()
        if not text:
            continue
        marker = f"[PDF page {page['page_no']}"
        if page.get("ocr"):
            marker += ", read by OCR and possibly imperfect"
        marker += "]"
        block = f"{marker}\n{text}"
        if len(block) > budget:
            if page["page_no"] in [p["page_no"] for p in head]:
                # Never silently truncate an opening page: say so in place.
                block = block[:budget] + "\n[... the rest of this page was omitted]"
            else:
                continue
        blocks.append((page["page_no"], block))
        budget -= len(block)
        if budget <= 0:
            break

    blocks.sort(key=lambda pair: pair[0])
    included = [page_no for page_no, _ in blocks]
    omitted = [p["page_no"] for p in ordered if p["page_no"] not in included]
    header = ["THE REPORT"]
    if omitted:
        header.append(
            f"Note: this report has {len(ordered)} pages and "
            f"{len(included)} are shown below. Pages "
            f"{', '.join(str(p) for p in omitted)} were omitted for length. If "
            "you would need them to decide, answer \"unclear\" and say which "
            "pages you would need."
        )
    # Pages are emitted in reading order so a page number means what it says.
    body = (
        "\n\n".join(block for _, block in blocks)
        if blocks
        else "(no text could be read from this report)"
    )
    return "\n\n".join(header) + "\n\n" + body, included


def eligibility_user(record: dict[str, Any], report_text: str) -> str:
    bibliographic = [
        "THE RECORD AS INDEXED",
        f"Title: {record.get('title') or '(no title)'}",
        f"Year: {record.get('year') or 'not stated'}",
        f"Publication: {record.get('journal') or 'not stated'}",
        f"Type: {record.get('doc_type') or 'not stated'}",
    ]
    return "\n".join(bibliographic) + "\n\n" + report_text + (
        "\n\nAssess this report against the protocol."
    )
