"""RIS reader with per-database dialect handling.

RIS is a loose convention rather than a standard. Every database bends it:
Scopus puts the DOI in `DO` but sometimes only in `UR`; Web of Science carries
its accession in `UT`; EBSCO names the database in `DB` and the accession in
`AN`; ProQuest wraps the abstract across `AB` continuation lines and keeps the
document type in `M3`. This module reads the file once, keeps every tag it
sees, then lets the detected dialect decide which tag wins for each field.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ParsedRecord, ParseError, ParseResult
from .normalise import clean_text, normalise_doi, parse_year, split_keywords

__all__ = ["parse_ris", "parse_ris_text", "detect_dialect", "RIS_TAG_RE"]

# Canonical RIS is `XX  - value`. Exports in the wild use one space, no space,
# or a tab, so the separator is forgiving -- but the tag itself must be two
# upper-case alphanumerics, which stops wrapped prose being read as a tag.
RIS_TAG_RE = re.compile(r"^([A-Z][A-Z0-9])[ \t]{0,3}-[ \t]?(.*)$")

_START_TAGS = ("TY",)
_END_TAG = "ER"

# Tag precedence per field, highest first.
_TITLE_TAGS = ("TI", "T1", "CT", "BT")
# J9/JI are Web of Science journal *abbreviations*, so they sit last: they are
# a fallback, never a replacement for the full title.
_JOURNAL_TAGS = ("JO", "JF", "JA", "J1", "J2", "T2", "SO", "J9", "JI")
_ABSTRACT_TAGS = ("AB", "N2")
_AUTHOR_TAGS = ("AU", "A1")
_KEYWORD_TAGS = ("KW", "DE", "ID")
_DOI_TAGS = ("DO", "DI")
_URL_TAGS = ("UR", "L1", "L2", "L3")
_YEAR_TAGS = ("PY", "Y1", "DA", "Y2")
_TYPE_TAGS = ("M3", "DT", "PT", "TY")
_ACCESSION_TAGS = ("UT", "AN", "ID", "C7")


def _read_text(path: str | Path) -> str:
    """Decode an export file, tolerating the encodings databases actually emit."""
    data = Path(path).read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def detect_dialect(text: str) -> tuple[str, str]:
    """Identify the exporting database.

    Returns ``(dialect, source_database)``. The dialect drives tag precedence;
    the database name is what PRISMA reports per source, and the reviewer can
    correct it on the import screen if a file was re-exported through another
    tool.
    """
    head = text[:200_000]
    scores: dict[str, int] = {}

    def add(dialect: str, points: int) -> None:
        scores[dialect] = scores.get(dialect, 0) + points

    if re.search(r"^UT\s{0,3}-\s?WOS:", head, re.MULTILINE):
        add("wos", 5)
    if re.search(r"scopus\.com/inward/record", head, re.IGNORECASE):
        add("scopus", 5)
    if re.search(r"^N1\s{0,3}-\s?(Export Date|Cited By)", head, re.MULTILINE):
        add("scopus", 3)
    if re.search(r"proquest\.com", head, re.IGNORECASE):
        add("proquest", 4)
    if re.search(r"^AN\s{0,3}-\s?\d", head, re.MULTILINE):
        add("ebsco", 2)
    if re.search(r"ebscohost|search\.ebscohost\.com", head, re.IGNORECASE):
        add("ebsco", 4)
    if re.search(r"^PMID\s*:|pubmed", head, re.IGNORECASE):
        add("pubmed", 2)

    # An explicit DB tag beats every heuristic above.
    db_match = re.search(r"^DB\s{0,3}-\s?(.+)$", head, re.MULTILINE)
    declared = clean_text(db_match.group(1)) if db_match else ""
    lowered = declared.casefold()
    if "scopus" in lowered:
        add("scopus", 6)
    elif "web of science" in lowered or lowered.startswith("wos"):
        add("wos", 6)
    elif "proquest" in lowered:
        add("proquest", 6)
    elif "pubmed" in lowered or "medline" in lowered:
        add("pubmed", 6)
    elif declared:
        # EBSCO is the only major exporter that names its *content* database
        # here ("Business Source Premier", "CINAHL"), so an unrecognised
        # non-empty DB tag is weak evidence for EBSCO.
        add("ebsco", 3)

    if not scores:
        return "generic", declared or "Unknown"

    dialect = max(scores, key=lambda key: scores[key])
    display = {
        "scopus": "Scopus",
        "wos": "Web of Science",
        "ebsco": declared or "EBSCO",
        "proquest": "ProQuest",
        "pubmed": "PubMed",
        "generic": declared or "Unknown",
    }[dialect]
    return dialect, display


def _split_records(text: str) -> list[tuple[int, str]]:
    """Cut the file into `TY ... ER` blocks, keeping each block verbatim.

    Returns `(position_in_file, block_text)`. Text before the first `TY` is
    discarded as a file header; a final block without `ER` is still returned,
    because a truncated download should surface as records plus a warning
    rather than as silence.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[int, str]] = []
    current: list[str] | None = None
    position = 0
    for line in lines:
        match = RIS_TAG_RE.match(line)
        tag = match.group(1) if match else None
        if tag in _START_TAGS:
            if current:
                position += 1
                blocks.append((position, "\n".join(current)))
            current = [line]
            continue
        if current is None:
            continue
        current.append(line)
        if tag == _END_TAG:
            position += 1
            blocks.append((position, "\n".join(current)))
            current = None
    if current:
        position += 1
        blocks.append((position, "\n".join(current)))
    return blocks


def _parse_tags(block: str) -> dict[str, list[str]]:
    """Collect every tag in a block, joining continuation lines.

    A line that does not start with a tag belongs to the tag above it; that is
    how long abstracts and wrapped titles arrive.
    """
    tags: dict[str, list[str]] = {}
    current_tag: str | None = None
    for line in block.split("\n"):
        match = RIS_TAG_RE.match(line)
        if match:
            current_tag, value = match.group(1), match.group(2).strip()
            tags.setdefault(current_tag, []).append(value)
        elif current_tag and line.strip():
            values = tags[current_tag]
            joiner = "" if values[-1].endswith("-") else " "
            values[-1] = f"{values[-1]}{joiner}{line.strip()}".strip()
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


def _pages(tags: dict[str, list[str]]) -> str:
    # BP ("beginning page") is the Web of Science spelling of SP.
    start = _first(tags, ("SP", "BP"))
    end = _first(tags, ("EP",))
    if start and end and start != end:
        return f"{start}-{end}"
    return start or end


def _record_from_tags(
    tags: dict[str, list[str]], dialect: str, source_database: str
) -> ParsedRecord:
    record = ParsedRecord(raw_format="ris", tags=tags)

    record.title = _first(tags, _TITLE_TAGS)
    record.abstract = _first(tags, _ABSTRACT_TAGS)
    record.authors = _all(tags, _AUTHOR_TAGS)
    record.year = parse_year(*_all(tags, _YEAR_TAGS))
    record.volume = _first(tags, ("VL",))
    record.issue = _first(tags, ("IS", "CP"))
    record.pages = _pages(tags)
    record.issn = _first(tags, ("SN",))
    record.url = _first(tags, _URL_TAGS)
    record.keywords = split_keywords(_all(tags, _KEYWORD_TAGS))
    record.language = _first(tags, ("LA",))
    record.publisher = _first(tags, ("PB", "PU"))

    journal = _first(tags, _JOURNAL_TAGS)
    ty = _first(tags, ("TY",)).upper()
    if not journal and ty in {"CHAP", "CONF", "CPAPER"}:
        journal = _first(tags, ("BT",))
    record.journal = journal

    # If BT supplied the title for a book chapter, the chapter title in TI/T1
    # should still win -- _TITLE_TAGS already orders it that way.
    doi = normalise_doi(_first(tags, _DOI_TAGS))
    if not doi:
        # Scopus and EBSCO sometimes leave the DOI only in a link or a note.
        for candidate in _all(tags, _URL_TAGS) + _all(tags, ("N1", "M3", "C3")):
            doi = normalise_doi(candidate)
            if doi:
                break
    record.doi = doi

    doc_type = _first(tags, ("M3", "DT", "PT"))
    record.doc_type = doc_type or ty

    accession = _first(tags, _ACCESSION_TAGS)
    if dialect == "wos" and not accession.upper().startswith("WOS:"):
        wos = _first(tags, ("UT",))
        accession = wos or accession
    record.accession = accession

    record.source_database = _first(tags, ("DB",)) or source_database
    return record.tidy()


def parse_ris_text(
    text: str, *, dialect: str | None = None, source_database: str | None = None
) -> ParseResult:
    """Parse RIS held in a string. The file reader below is a thin wrapper."""
    detected_dialect, detected_db = detect_dialect(text)
    dialect = dialect or detected_dialect
    source_database = source_database or detected_db

    result = ParseResult(format="ris", dialect=dialect, source_database=source_database)
    for position, block in _split_records(text):
        tags = _parse_tags(block)
        if not tags:
            continue
        try:
            record = _record_from_tags(tags, dialect, source_database)
        except Exception as exc:  # a malformed block must not lose the file
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
        if _END_TAG not in tags:
            record.tags.setdefault("_warnings", []).append("no ER terminator")
        result.records.append(record)
    return result


def parse_ris(
    path: str | Path, *, dialect: str | None = None, source_database: str | None = None
) -> ParseResult:
    return parse_ris_text(
        _read_text(path), dialect=dialect, source_database=source_database
    )
