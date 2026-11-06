"""Format detection and dispatch.

A reviewer drops a folder of exports onto the import screen without knowing or
caring which is RIS and which is nbib. The sniffing here looks at the content
first and the extension second, because exports are routinely saved with the
wrong suffix (`.txt` from Web of Science, `.ris` from a converter).
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import ParseResult
from .csv_import import parse_csv
from .endnote_xml import parse_endnote_xml
from .nbib import parse_nbib
from .ris import _read_text, parse_ris

__all__ = ["detect_format", "parse_file", "SUPPORTED_SUFFIXES", "UnknownFormatError"]

SUPPORTED_SUFFIXES = (".ris", ".txt", ".nbib", ".xml", ".csv", ".tsv", ".enw")

_RIS_START_RE = re.compile(r"^TY\s{0,3}-\s?\w", re.MULTILINE)
_NBIB_START_RE = re.compile(r"^(PMID|OWN|STAT)\s*-\s", re.MULTILINE)


class UnknownFormatError(ValueError):
    pass


def detect_format(path: str | Path, text: str | None = None) -> str:
    """Return one of: ris, nbib, endnote_xml, csv."""
    path = Path(path)
    if text is None:
        text = _read_text(path)
    head = text[:50_000]
    stripped = head.lstrip()

    if stripped.startswith("<?xml") or "<records>" in head or "<record>" in head:
        return "endnote_xml"
    if _NBIB_START_RE.search(head):
        return "nbib"
    if _RIS_START_RE.search(head):
        return "ris"

    suffix = path.suffix.casefold()
    if suffix in (".csv", ".tsv"):
        return "csv"
    if suffix == ".nbib":
        return "nbib"
    if suffix == ".xml":
        return "endnote_xml"
    if suffix in (".ris", ".enw"):
        return "ris"

    # A first line with several delimiters and no RIS tags is a spreadsheet.
    first_line = head.split("\n", 1)[0]
    if sum(first_line.count(d) for d in ",;\t") >= 2:
        return "csv"

    raise UnknownFormatError(
        f"{path.name} is not a format this app recognises. Supported: "
        "RIS, PubMed .nbib, EndNote XML and CSV/TSV."
    )


def parse_file(
    path: str | Path, *, source_database: str | None = None, fmt: str | None = None
) -> ParseResult:
    """Read one export file into normalised records."""
    path = Path(path)
    text = _read_text(path)
    fmt = fmt or detect_format(path, text)
    if fmt == "ris":
        return parse_ris(path, source_database=source_database)
    if fmt == "nbib":
        return parse_nbib(path, source_database=source_database or "PubMed")
    if fmt == "endnote_xml":
        return parse_endnote_xml(path, source_database=source_database or "EndNote")
    if fmt == "csv":
        return parse_csv(path, source_database=source_database)
    raise UnknownFormatError(f"Unsupported format: {fmt}")
