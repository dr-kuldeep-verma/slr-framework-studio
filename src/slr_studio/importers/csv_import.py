"""CSV and tab-delimited reader for database exports.

Scopus, Web of Science and most repositories offer a spreadsheet export whose
columns are human-readable but inconsistently named ("Source title" vs
"Journal", "Publication Year" vs "Year"). The alias table below maps the
common headings; anything unrecognised is kept in the raw record so nothing is
silently dropped.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from .base import ParsedRecord, ParseError, ParseResult
from .normalise import clean_text, normalise_doi, parse_year, split_keywords
from .ris import _read_text

__all__ = ["parse_csv", "parse_csv_text", "COLUMN_ALIASES"]

#: field -> candidate headings, best first. Matching is case-insensitive and
#: ignores punctuation and whitespace.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "title": ("title", "articletitle", "documenttitle", "ti"),
    "abstract": ("abstract", "ab", "summary"),
    "authors": ("authors", "author", "authorfullnames", "authornames", "au", "af"),
    "year": ("year", "publicationyear", "publishedyear", "pubyear", "date", "py"),
    "journal": (
        "sourcetitle", "journal", "journaltitle", "publicationtitle",
        "source", "so", "fulljournaltitle",
    ),
    "volume": ("volume", "vol", "vl"),
    "issue": ("issue", "number", "is"),
    "pages": ("pages", "pagerange"),
    "start_page": ("pagestart", "startpage", "beginningpage", "sp", "firstpage"),
    "end_page": ("pageend", "endpage", "ep", "lastpage"),
    "doi": ("doi", "di", "digitalobjectidentifier"),
    "issn": ("issn", "sn", "eissn"),
    "url": ("link", "url", "doilink", "documenturl"),
    "keywords": (
        "authorkeywords", "keywords", "indexkeywords", "de", "id", "keywordsplus",
    ),
    "language": ("language", "languageoforiginaldocument", "la"),
    "doc_type": ("documenttype", "doctype", "publicationtype", "type", "dt"),
    "publisher": ("publisher", "pu"),
    "accession": (
        "eid", "uniquewosid", "utuniquewosid", "ut", "accessionnumber",
        "pmid", "scopusid",
    ),
    "source_database": ("database", "db", "source database"),
}

_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_header(name: str) -> str:
    return _NORM_RE.sub("", (name or "").casefold())


def _build_map(fieldnames: list[str]) -> dict[str, str]:
    """Map our field names onto this file's actual column names."""
    available = {_norm_header(name): name for name in fieldnames if name}
    mapping: dict[str, str] = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            key = _norm_header(alias)
            if key in available:
                mapping[field] = available[key]
                break
    return mapping


def _sniff_dialect(sample: str) -> type[csv.Dialect] | csv.Dialect:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _split_authors(value: str) -> list[str]:
    """Split an author cell.

    Scopus uses `Smith J., Doe A.`, Web of Science uses `Smith, J; Doe, A`,
    and some exports use ` and `. Semicolons are unambiguous; commas are only
    treated as separators when no semicolon is present and the cell does not
    look like a single `Surname, Initials` pair.
    """
    text = clean_text(value)
    if not text:
        return []
    if ";" in text:
        parts = text.split(";")
    elif re.search(r"\band\b", text) and "," not in text:
        parts = re.split(r"\band\b", text)
    elif text.count(",") >= 2:
        # `Smith J., Doe A., Roe B.` -- split on commas that follow initials.
        parts = re.split(r",\s*(?=[A-Z])", text)
    else:
        parts = [text]
    return [p.strip(" ,;") for p in parts if p.strip(" ,;")]


def parse_csv_text(
    text: str, *, source_database: str | None = None, filename: str = ""
) -> ParseResult:
    sample = text[:8192]
    dialect = _sniff_dialect(sample)
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    fieldnames = list(reader.fieldnames or [])
    result = ParseResult(
        format="csv",
        dialect="csv",
        source_database=source_database or _guess_database(fieldnames, filename),
    )
    if not fieldnames:
        result.errors.append(ParseError(0, "File has no header row"))
        return result

    mapping = _build_map(fieldnames)
    if "title" not in mapping:
        result.errors.append(
            ParseError(0, "No title column found; expected one of: title, article title")
        )
        return result

    def cell(row: dict[str, str], field: str) -> str:
        column = mapping.get(field)
        return clean_text(row.get(column, "")) if column else ""

    for position, row in enumerate(reader, start=1):
        try:
            record = ParsedRecord(raw_format="csv")
            record.title = cell(row, "title")
            record.abstract = cell(row, "abstract")
            record.authors = _split_authors(cell(row, "authors"))
            record.year = parse_year(cell(row, "year"))
            record.journal = cell(row, "journal")
            record.volume = cell(row, "volume")
            record.issue = cell(row, "issue")
            pages = cell(row, "pages")
            if not pages:
                start, end = cell(row, "start_page"), cell(row, "end_page")
                pages = f"{start}-{end}" if start and end else (start or end)
            record.pages = pages
            record.issn = cell(row, "issn")
            record.url = cell(row, "url")
            record.keywords = split_keywords(cell(row, "keywords"))
            record.language = cell(row, "language")
            record.doc_type = cell(row, "doc_type")
            record.publisher = cell(row, "publisher")
            record.accession = cell(row, "accession")
            record.doi = normalise_doi(cell(row, "doi")) or normalise_doi(record.url)
            record.source_database = cell(row, "source_database") or result.source_database
            record.tidy()
            record.raw = "\n".join(
                f"{k}: {v}" for k, v in row.items() if k and str(v or "").strip()
            )
            record.position_in_file = position
        except Exception as exc:
            result.errors.append(ParseError(position, f"Could not parse row: {exc}"))
            continue
        if not record.title and not record.doi:
            result.errors.append(ParseError(position, "Row has neither a title nor a DOI"))
            continue
        result.records.append(record)
    return result


def _guess_database(fieldnames: list[str], filename: str) -> str:
    headers = {_norm_header(f) for f in fieldnames}
    if "eid" in headers or "sourcetitle" in headers:
        return "Scopus"
    if any(h.startswith("utunique") for h in headers) or "keywordsplus" in headers:
        return "Web of Science"
    if "pmid" in headers:
        return "PubMed"
    stem = Path(filename).stem.casefold() if filename else ""
    for name, label in (
        ("scopus", "Scopus"),
        ("wos", "Web of Science"),
        ("savedrecs", "Web of Science"),
        ("ebsco", "EBSCO"),
        ("proquest", "ProQuest"),
        ("pubmed", "PubMed"),
    ):
        if name in stem:
            return label
    return "Unknown"


def parse_csv(path: str | Path, *, source_database: str | None = None) -> ParseResult:
    return parse_csv_text(
        _read_text(path), source_database=source_database, filename=str(path)
    )
