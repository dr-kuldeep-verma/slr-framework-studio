"""PubMed / MEDLINE `.nbib` reader.

NBIB looks like RIS but is not: tags run up to four characters, continuation
lines are indented rather than untagged, records are separated by a blank line
instead of an `ER` terminator, and the DOI hides in `AID` or `LID` behind a
`[doi]` suffix.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ParsedRecord, ParseError, ParseResult
from .normalise import normalise_doi, parse_year, split_keywords
from .ris import _read_text

__all__ = ["parse_nbib", "parse_nbib_text", "NBIB_TAG_RE"]

NBIB_TAG_RE = re.compile(r"^([A-Z]{2,4}|[A-Z]{2,4}\d?)\s*-\s?(.*)$")
_DOI_SUFFIX_RE = re.compile(r"\[doi\]\s*$", re.IGNORECASE)


def _split_records(text: str) -> list[tuple[int, str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[int, str]] = []
    current: list[str] = []
    position = 0
    for line in lines:
        if not line.strip():
            if current:
                position += 1
                blocks.append((position, "\n".join(current)))
                current = []
            continue
        current.append(line)
    if current:
        position += 1
        blocks.append((position, "\n".join(current)))
    return blocks


def _parse_tags(block: str) -> dict[str, list[str]]:
    tags: dict[str, list[str]] = {}
    current: str | None = None
    for line in block.split("\n"):
        # Continuation lines are indented; a tagged line never is.
        if line.startswith((" ", "\t")) and current:
            tags[current][-1] = f"{tags[current][-1]} {line.strip()}".strip()
            continue
        match = NBIB_TAG_RE.match(line)
        if match:
            current = match.group(1)
            tags.setdefault(current, []).append(match.group(2).strip())
        elif current and line.strip():
            tags[current][-1] = f"{tags[current][-1]} {line.strip()}".strip()
    return tags


def _first(tags: dict[str, list[str]], names: tuple[str, ...]) -> str:
    for name in names:
        for value in tags.get(name, []):
            if value.strip():
                return value.strip()
    return ""


def _all(tags: dict[str, list[str]], names: tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for name in names:
        out.extend(v.strip() for v in tags.get(name, []) if v.strip())
    return out


def _doi_from(tags: dict[str, list[str]]) -> str | None:
    # AID and LID both carry identifiers tagged by kind: `10.x/y [doi]`,
    # `S0140-6736(20)30183-5 [pii]`. Only the [doi] ones are DOIs.
    for value in _all(tags, ("AID", "LID")):
        if _DOI_SUFFIX_RE.search(value):
            doi = normalise_doi(_DOI_SUFFIX_RE.sub("", value).strip())
            if doi:
                return doi
    for value in _all(tags, ("AID", "LID", "SO")):
        doi = normalise_doi(value)
        if doi:
            return doi
    return None


def parse_nbib_text(text: str, *, source_database: str = "PubMed") -> ParseResult:
    result = ParseResult(format="nbib", dialect="pubmed", source_database=source_database)
    for position, block in _split_records(text):
        tags = _parse_tags(block)
        if not tags or not any(key in tags for key in ("PMID", "TI", "AB", "JT")):
            continue
        try:
            record = ParsedRecord(raw_format="nbib", tags=tags)
            record.title = _first(tags, ("TI", "BTI", "CTI"))
            record.abstract = _first(tags, ("AB",))
            # FAU holds "Surname, Given"; AU holds "Surname GI". Prefer FAU.
            record.authors = _all(tags, ("FAU",)) or _all(tags, ("AU",))
            record.year = parse_year(_first(tags, ("DP",)), _first(tags, ("DEP", "EDAT")))
            record.journal = _first(tags, ("JT", "TA"))
            record.volume = _first(tags, ("VI",))
            record.issue = _first(tags, ("IP",))
            record.pages = _first(tags, ("PG",))
            record.issn = _first(tags, ("IS",))
            record.language = _first(tags, ("LA",))
            record.doc_type = _first(tags, ("PT",))
            record.publisher = _first(tags, ("PB",))
            record.accession = _first(tags, ("PMID",))
            record.keywords = split_keywords(_all(tags, ("OT", "MH")))
            record.doi = _doi_from(tags)
            record.source_database = source_database
            record.tidy()
        except Exception as exc:
            result.errors.append(
                ParseError(position, f"Could not parse record: {exc}", block[:200])
            )
            continue
        record.raw = block
        record.position_in_file = position
        if not record.title and not record.doi:
            result.errors.append(
                ParseError(position, "Record has neither a title nor a DOI", block[:200])
            )
            continue
        result.records.append(record)
    return result


def parse_nbib(path: str | Path, *, source_database: str = "PubMed") -> ParseResult:
    return parse_nbib_text(_read_text(path), source_database=source_database)
