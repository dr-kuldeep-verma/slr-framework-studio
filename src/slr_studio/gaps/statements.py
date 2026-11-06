"""Writing a gap down, with the qualifier that makes it honest.

Every claim of absence this stage makes is an absence *within the included
corpus, searched to a date*. Saying so is not a disclaimer: a review that
reports "nobody has studied X" when it means "nothing in my 89 included
studies measured X" has overstated its own reach, and that is one of the
commonest ways a systematic review misleads.

So the qualifier is added here, in the one function that turns a candidate
into prose, and the same function refuses to produce prose at all when the
counts or the records behind the claim are missing. A reviewer pastes these
sentences into a manuscript; there must be no path that yields one without
its evidence.
"""

from __future__ import annotations

from typing import Any

from .detect import GapCandidate

__all__ = ["UnsupportedGap", "qualifier", "write_statement"]


class UnsupportedGap(ValueError):
    """Raised when a gap has no counts, or no records where it needs them."""


#: Gap types that are claims about studies that exist rather than about their
#: absence. These must cite the records they are talking about: a
#: contradiction with no studies behind it is not a contradiction, and an
#: author-stated gap with no source is an unattributed quotation.
_MUST_CITE_RECORDS = frozenset({"contradiction", "author_stated"})


def qualifier(searched_to: str) -> str:
    return f"Within the included corpus, searched to {searched_to}"


def write_statement(gap: GapCandidate, *, searched_to: str) -> str:
    """The sentence a reviewer may quote, or an error explaining why not."""
    if not searched_to.strip():
        raise UnsupportedGap(
            f"The {gap.gap_type} gap cannot be written down because the "
            "project does not record the date the searches were run. Without "
            "it the statement claims an absence in the literature rather than "
            "in this corpus. Set the search date on the Project page."
        )

    if not _has_a_number(gap.counts):
        raise UnsupportedGap(
            f"The {gap.gap_type} gap {gap.statement.body[:60]!r} has no "
            "supporting counts. A gap statement may not be produced without "
            "the counts that support it."
        )

    if gap.gap_type in _MUST_CITE_RECORDS and not gap.record_ids:
        raise UnsupportedGap(
            f"The {gap.gap_type} gap {gap.statement.body[:60]!r} is a claim "
            "about studies in the corpus but cites none of them. A gap "
            "statement may not be produced without the records that support "
            "it."
        )

    body = gap.statement.body.strip()
    if "within the included corpus" in body.casefold():
        return body
    # Lower-case the claim's first letter so it reads on from the qualifier,
    # unless it starts with a proper noun or an identifier.
    first = body[:1]
    rest = body[1:]
    if first.isupper() and not rest[:1].isupper():
        body = first.lower() + rest
    return f"{qualifier(searched_to)}, {body}"


def _has_a_number(counts: dict[str, Any]) -> bool:
    """Whether the counts actually count something.

    A dictionary of labels -- which construct, which design -- describes the
    gap but does not evidence it. At least one value has to be a number.
    """
    for value in counts.values():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return True
        if isinstance(value, (list, tuple)) and value:
            return True
    return False
