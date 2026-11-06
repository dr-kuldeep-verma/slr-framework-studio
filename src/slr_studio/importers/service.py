"""Turning parsed files into stored records.

Parsing and storing are kept apart on purpose: the parsers are pure functions
over text, which makes them cheap to unit-test per dialect, and everything
that touches the database and the audit trail lives here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.db import utc_now
from ..core.hashing import sha256_file
from ..core.project import Project
from .base import ParseResult
from .normalise import normalise_doi, normalise_title
from .registry import parse_file

__all__ = ["ImportService", "ImportSummary", "DuplicateFileError"]


class DuplicateFileError(RuntimeError):
    """Raised when the identical file has already been imported."""

    def __init__(self, path: Path, import_file_id: int, imported_at: str) -> None:
        super().__init__(
            f"{path.name} was already imported on {imported_at} "
            f"(import #{import_file_id}). Importing it again would inflate the "
            "PRISMA identification count."
        )
        self.import_file_id = import_file_id


@dataclass
class ImportSummary:
    import_file_id: int
    filename: str
    source_database: str
    format: str
    dialect: str
    n_records: int = 0
    n_errors: int = 0
    n_missing_abstract: int = 0
    n_missing_doi: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "import_file_id": self.import_file_id,
            "filename": self.filename,
            "source_database": self.source_database,
            "format": self.format,
            "dialect": self.dialect,
            "n_records": self.n_records,
            "n_errors": self.n_errors,
            "n_missing_abstract": self.n_missing_abstract,
            "n_missing_doi": self.n_missing_doi,
        }


class ImportService:
    def __init__(self, project: Project) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit

    # -- importing ---------------------------------------------------------

    def import_file(
        self,
        path: str | Path,
        *,
        source_database: str | None = None,
        fmt: str | None = None,
        allow_reimport: bool = False,
    ) -> ImportSummary:
        """Parse one export file and store its records.

        The file is hashed first. Re-importing the same bytes is refused unless
        explicitly allowed, because the commonest way to corrupt a PRISMA count
        is to drop the same download in twice.
        """
        path = Path(path)
        file_hash = sha256_file(path)
        existing = self.db.query_one(
            "SELECT id, imported_at FROM import_file WHERE file_sha256 = ?", (file_hash,)
        )
        if existing and not allow_reimport:
            raise DuplicateFileError(path, existing["id"], existing["imported_at"])

        result = parse_file(path, source_database=source_database, fmt=fmt)
        archived = self.project.archive_import(path)
        return self._store(result, path, archived, file_hash, source_database)

    def _store(
        self,
        result: ParseResult,
        original: Path,
        archived: Path,
        file_hash: str,
        source_database_override: str | None,
    ) -> ImportSummary:
        now = utc_now()
        database = source_database_override or result.source_database or "Unknown"

        with self.db.transaction():
            import_file_id = self.db.insert(
                "import_file",
                {
                    "filename": original.name,
                    "original_path": str(original),
                    "file_sha256": file_hash,
                    "format": result.format,
                    "dialect": result.dialect,
                    "source_database": database,
                    "n_records": len(result.records),
                    "n_parse_errors": len(result.errors),
                    "imported_at": now,
                    "notes": f"archived as {archived.name}",
                },
            )

            summary = ImportSummary(
                import_file_id=import_file_id,
                filename=original.name,
                source_database=database,
                format=result.format,
                dialect=result.dialect,
                n_records=len(result.records),
                n_errors=len(result.errors),
                errors=[f"record {e.position}: {e.message}" for e in result.errors[:50]],
            )

            for record in result.records:
                # Every record takes the file's database label, not whatever
                # its own DB tag said. PRISMA counts per database *searched*,
                # and one ProQuest export naming two collections internally is
                # still one search of ProQuest. The record's own tag is never
                # lost: it stays in the raw block.
                record.source_database = database
                row = record.to_row(import_file_id, now)
                record_id = self.db.insert("record", row)
                self._store_keywords(record_id, record)
                self.db.insert(
                    "record_source",
                    {
                        "record_id": record_id,
                        "source_database": record.source_database,
                        "import_file_id": import_file_id,
                        "origin_record_id": record_id,
                        "accession": record.accession,
                    },
                )
                if not record.has_abstract:
                    summary.n_missing_abstract += 1
                if not record.doi:
                    summary.n_missing_doi += 1

            self.audit.log(
                "import",
                "file_imported",
                entity="import_file",
                entity_id=import_file_id,
                detail={
                    "filename": original.name,
                    "file_sha256": file_hash,
                    "format": result.format,
                    "dialect": result.dialect,
                    "source_database": database,
                    "n_records": summary.n_records,
                    "n_parse_errors": summary.n_errors,
                    "n_missing_abstract": summary.n_missing_abstract,
                },
            )
        self.check_seeds()
        return summary

    def _store_keywords(self, record_id: int, record) -> None:
        """Index the record's keywords one row each.

        The JSON on the record stays as it arrived; this is the queryable
        form, which topic clustering and keyword co-occurrence need and which
        deduplication later adds to rather than replaces.
        """
        rows = []
        seen: set[str] = set()
        for keyword in record.keywords:
            text = str(keyword).strip()
            normalised = normalise_title(text)
            if not normalised or normalised in seen:
                continue
            seen.add(normalised)
            rows.append(
                (record_id, text, normalised, record_id, record.source_database)
            )
        if rows:
            self.db.executemany(
                "INSERT OR IGNORE INTO record_keyword"
                "(record_id, keyword, keyword_norm, from_record_id, source_database) "
                "VALUES(?, ?, ?, ?, ?)",
                rows,
            )

    def import_many(
        self, paths: list[str | Path], *, allow_reimport: bool = False
    ) -> tuple[list[ImportSummary], list[tuple[str, str]]]:
        """Import several files, collecting failures rather than stopping."""
        summaries: list[ImportSummary] = []
        failures: list[tuple[str, str]] = []
        for path in paths:
            try:
                summaries.append(self.import_file(path, allow_reimport=allow_reimport))
            except Exception as exc:
                failures.append((Path(path).name, str(exc)))
        return summaries, failures

    # -- reporting ---------------------------------------------------------

    def files(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.query(
            "SELECT f.*, "
            "  (SELECT COUNT(*) FROM record r WHERE r.import_file_id = f.id "
            "     AND r.has_abstract = 0) AS n_missing_abstract "
            "FROM import_file f ORDER BY f.id"
        )]

    def counts_per_database(self) -> list[dict[str, Any]]:
        return self.project.counts_per_database()

    # -- seed studies ------------------------------------------------------

    def add_seed(self, doi: str, note: str = "") -> int:
        normalised = normalise_doi(doi)
        if not normalised:
            raise ValueError(f"{doi!r} does not contain a DOI")
        existing = self.db.scalar("SELECT id FROM seed_study WHERE doi = ?", (normalised,))
        if existing:
            return int(existing)
        with self.db.transaction():
            seed_id = self.db.insert("seed_study", {"doi": normalised, "note": note})
            self.audit.log(
                "project", "seed_added", entity="seed_study", entity_id=seed_id,
                detail={"doi": normalised},
            )
        return seed_id

    def check_seeds(self) -> list[dict[str, Any]]:
        """Report which seed studies the searches actually found.

        A seed the search misses is the earliest warning that the query is too
        narrow, which is why this runs after every import rather than on
        request.
        """
        seeds = self.db.query("SELECT * FROM seed_study ORDER BY id")
        if not seeds:
            return []
        out: list[dict[str, Any]] = []
        with self.db.transaction():
            for seed in seeds:
                match = self.db.query_one(
                    "SELECT id, title, source_database FROM record "
                    "WHERE doi = ? ORDER BY active DESC, id LIMIT 1",
                    (seed["doi"],),
                )
                found_id = match["id"] if match else None
                self.db.execute(
                    "UPDATE seed_study SET found_record_id = ?, checked_at = ? WHERE id = ?",
                    (found_id, utc_now(), seed["id"]),
                )
                out.append(
                    {
                        "id": seed["id"],
                        "doi": seed["doi"],
                        "found": found_id is not None,
                        "record_id": found_id,
                        "title": match["title"] if match else "",
                        "source_database": match["source_database"] if match else "",
                    }
                )
            found = sum(1 for s in out if s["found"])
            self.audit.log(
                "import",
                "seed_check",
                detail={"n_seeds": len(out), "n_found": found,
                        "missing": [s["doi"] for s in out if not s["found"]]},
            )
        return out
