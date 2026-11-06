"""The common shape every importer produces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.hashing import sha256_json
from .normalise import (
    clean_text,
    first_author_key,
    normalise_doc_type,
    normalise_doi,
    normalise_language,
    normalise_title,
    title_key,
)

__all__ = ["ParsedRecord", "ParseResult", "ParseError"]


@dataclass
class ParseError:
    position: int
    message: str
    excerpt: str = ""


@dataclass
class ParsedRecord:
    """One reference, normalised, with its source block preserved verbatim."""

    title: str = ""
    abstract: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doi: str | None = None
    issn: str = ""
    url: str = ""
    keywords: list[str] = field(default_factory=list)
    language: str = ""
    doc_type: str = ""
    publisher: str = ""
    accession: str = ""
    source_database: str = ""

    raw: str = ""
    raw_format: str = ""
    position_in_file: int = 0
    tags: dict[str, list[str]] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------

    @property
    def title_norm(self) -> str:
        return normalise_title(self.title)

    @property
    def title_block_key(self) -> str:
        return title_key(self.title)

    @property
    def first_author_norm(self) -> str:
        return first_author_key(self.authors)

    @property
    def has_abstract(self) -> bool:
        # A handful of databases emit "No abstract available" as the abstract.
        text = self.abstract.strip()
        if len(text) < 20:
            return False
        return "no abstract" not in text[:40].casefold()

    def flags(self) -> list[str]:
        """Quality signals the import screen shows the reviewer."""
        flags: list[str] = []
        if not self.title:
            flags.append("missing_title")
        if not self.has_abstract:
            flags.append("missing_abstract")
        if not self.doi:
            flags.append("missing_doi")
        if self.year is None:
            flags.append("missing_year")
        if not self.authors:
            flags.append("missing_authors")
        return flags

    def content_hash(self) -> str:
        """Stable identity for the reference as imported."""
        return sha256_json(
            {
                "title": self.title_norm,
                "doi": self.doi or "",
                "year": self.year,
                "authors": [a.casefold() for a in self.authors],
                "journal": normalise_title(self.journal),
            }
        )

    def tidy(self) -> ParsedRecord:
        """Apply the normalisers in place and return self.

        Importers fill the fields straight from their tags; this is the one
        place that cleans them, so every format gets identical treatment.
        """
        self.title = clean_text(self.title)
        self.abstract = clean_text(self.abstract)
        self.journal = clean_text(self.journal)
        self.volume = clean_text(self.volume)
        self.issue = clean_text(self.issue)
        self.pages = clean_text(self.pages)
        self.issn = clean_text(self.issn)
        self.url = clean_text(self.url)
        self.publisher = clean_text(self.publisher)
        self.accession = clean_text(self.accession)
        self.source_database = clean_text(self.source_database)
        self.authors = [clean_text(a) for a in self.authors if clean_text(a)]
        self.language = normalise_language(self.language)
        self.doc_type = normalise_doc_type(self.doc_type)
        self.doi = normalise_doi(self.doi) or normalise_doi(self.url)
        return self

    def to_row(self, import_file_id: int, created_at: str) -> dict[str, Any]:
        import json

        return {
            "import_file_id": import_file_id,
            "position_in_file": self.position_in_file,
            "title": self.title,
            "abstract": self.abstract,
            "authors": json.dumps(self.authors, ensure_ascii=False),
            "year": self.year,
            "journal": self.journal,
            "volume": self.volume,
            "issue": self.issue,
            "pages": self.pages,
            "doi": self.doi,
            "issn": self.issn,
            "url": self.url,
            "keywords": json.dumps(self.keywords, ensure_ascii=False),
            "language": self.language,
            "doc_type": self.doc_type,
            "publisher": self.publisher,
            "accession": self.accession,
            "source_database": self.source_database,
            "title_norm": self.title_norm,
            "title_key": self.title_block_key,
            "first_author_norm": self.first_author_norm,
            "raw": self.raw,
            "raw_format": self.raw_format,
            "record_hash": self.content_hash(),
            "has_abstract": 1 if self.has_abstract else 0,
            "flags": json.dumps(self.flags()),
            "created_at": created_at,
        }


@dataclass
class ParseResult:
    """Everything one file yielded, including what could not be read."""

    records: list[ParsedRecord] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    format: str = ""
    dialect: str = ""
    source_database: str = ""

    def __len__(self) -> int:
        return len(self.records)
