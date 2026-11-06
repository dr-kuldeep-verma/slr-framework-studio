"""EndNote XML reader.

EndNote wraps nearly every text node in `<style>` elements carrying font and
colour, so the text of a title is often split across several children. Every
read here uses `itertext`, which walks those children and ignores the styling.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .base import ParsedRecord, ParseError, ParseResult
from .normalise import clean_text, normalise_doi, parse_year, split_keywords

__all__ = ["parse_endnote_xml", "parse_endnote_xml_text"]


def _text(element: ET.Element | None) -> str:
    """All text under an element, with EndNote's `<style>` wrappers flattened."""
    if element is None:
        return ""
    return clean_text(" ".join(element.itertext()))


def _find_text(parent: ET.Element, *paths: str) -> str:
    for path in paths:
        value = _text(parent.find(path))
        if value:
            return value
    return ""


def _find_all_text(parent: ET.Element, path: str) -> list[str]:
    return [t for t in (_text(el) for el in parent.findall(path)) if t]


def parse_endnote_xml_text(
    text: str, *, source_database: str = "EndNote"
) -> ParseResult:
    result = ParseResult(
        format="endnote_xml", dialect="endnote", source_database=source_database
    )
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        result.errors.append(ParseError(0, f"Invalid EndNote XML: {exc}"))
        return result

    records = root.findall(".//record")
    for position, node in enumerate(records, start=1):
        try:
            record = ParsedRecord(raw_format="endnote_xml")
            record.title = _find_text(node, "titles/title", "titles/short-title")
            record.abstract = _find_text(node, "abstract")
            record.authors = _find_all_text(node, "contributors/authors/author")
            record.year = parse_year(
                _find_text(node, "dates/year"), _find_text(node, "dates/pub-dates/date")
            )
            record.journal = _find_text(
                node,
                "periodical/full-title",
                "titles/secondary-title",
                "titles/tertiary-title",
            )
            record.volume = _find_text(node, "volume")
            record.issue = _find_text(node, "number", "issue")
            record.pages = _find_text(node, "pages")
            record.issn = _find_text(node, "isbn")
            record.language = _find_text(node, "language")
            record.publisher = _find_text(node, "publisher")
            record.doc_type = _find_text(node, "ref-type")
            ref_type = node.find("ref-type")
            if ref_type is not None and ref_type.get("name"):
                record.doc_type = ref_type.get("name", "")
            record.accession = _find_text(node, "accession-num")
            record.keywords = split_keywords(_find_all_text(node, "keywords/keyword"))
            record.url = _find_text(
                node, "urls/related-urls/url", "urls/web-urls/url", "urls/pdf-urls/url"
            )
            record.doi = normalise_doi(
                _find_text(node, "electronic-resource-num")
            ) or normalise_doi(record.url)
            db_node = node.find("database")
            declared = (db_node.get("name") if db_node is not None else "") or _text(db_node)
            record.source_database = declared or source_database
            record.tidy()
            record.raw = ET.tostring(node, encoding="unicode")
            record.position_in_file = position
        except Exception as exc:
            result.errors.append(ParseError(position, f"Could not parse record: {exc}"))
            continue
        if not record.title and not record.doi:
            result.errors.append(
                ParseError(position, "Record has neither a title nor a DOI")
            )
            continue
        result.records.append(record)
    return result


def parse_endnote_xml(
    path: str | Path, *, source_database: str = "EndNote"
) -> ParseResult:
    from .ris import _read_text

    return parse_endnote_xml_text(_read_text(path), source_database=source_database)
