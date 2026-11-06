"""Getting reports into the project and tying them to records.

Three things happen here. The retrieval list is built, with a resolver link
and any open-access locations Unpaywall knows about. PDFs the reviewer drops
in are matched to records — by the DOI printed in the file, then by title —
and stored inside the project. And every record that was sought is tracked as
retrieved, not retrieved or abstract only, with a reason, because PRISMA asks
for the reports not retrieved and for why.

Matching is deliberately conservative. A file that matches nothing, or that
matches weakly, goes to a manual screen rather than being attached to the
closest record: a PDF filed against the wrong study is worse than one filed
against none, because it will be read, assessed and quoted as if it were the
right paper.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.db import utc_now
from ..core.hashing import sha256_file
from ..dedup.rules import title_similarity
from ..importers.normalise import normalise_doi, normalise_title
from .pdf import PdfError, extract_pdf
from .unpaywall import UnpaywallClient, doi_url

__all__ = [
    "RetrievalService",
    "MatchResult",
    "AttachOutcome",
    "RetrievalError",
    "AUTO_MATCH_TITLE_SCORE",
    "SUGGEST_TITLE_SCORE",
]

#: A title similarity at or above this attaches the file without asking.
AUTO_MATCH_TITLE_SCORE = 0.93
#: Below the auto threshold but at or above this, the file is sent to the
#: manual screen with the best candidate already selected.
SUGGEST_TITLE_SCORE = 0.70

STATUSES = ("pending", "retrieved", "not_retrieved", "abstract_only")


class RetrievalError(RuntimeError):
    pass


@dataclass
class MatchResult:
    record_id: int | None
    matched_by: str  # doi | title | none
    score: float
    detail: str = ""
    #: The closest record even when it was not close enough to attach, so the
    #: manual screen can open with it already selected.
    best_candidate_id: int | None = None


@dataclass
class AttachOutcome:
    filename: str
    record_id: int | None = None
    matched_by: str = "none"
    score: float = 0.0
    n_pages: int = 0
    n_pages_ocr: int = 0
    message: str = ""
    unmatched_id: int | None = None

    @property
    def matched(self) -> bool:
        return self.record_id is not None


class RetrievalService:
    def __init__(self, project) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit

    # -- which records need a report ---------------------------------------

    def sought_records(self) -> list[dict[str, Any]]:
        """Records that came through screening and so need a full text.

        Records left unclear at screening are sought too: carrying them to
        full text is how an unresolved title-and-abstract decision gets
        settled, rather than quietly disappearing.
        """
        return [
            dict(r)
            for r in self.db.query(
                "SELECT r.id, r.title, r.authors, r.year, r.journal, r.doi, "
                "       r.doc_type, r.source_database, s.decision AS screening_decision, "
                "       rep.status, rep.reason, rep.filename, rep.n_pages, "
                "       rep.matched_by, rep.match_score "
                "FROM record r "
                "JOIN screening_status s ON s.record_id = r.id "
                "LEFT JOIN report rep ON rep.record_id = r.id "
                "WHERE r.active = 1 AND s.decision IN ('include', 'unclear') "
                "ORDER BY r.id"
            )
        ]

    def _ensure_report(self, record_id: int) -> None:
        self.db.execute(
            "INSERT INTO report(record_id, status) VALUES(?, 'pending') "
            "ON CONFLICT(record_id) DO NOTHING",
            (record_id,),
        )

    # -- open-access lookups -----------------------------------------------

    def retrieval_list(self) -> list[dict[str, Any]]:
        """Everything the reviewer needs to go and find each report."""
        out: list[dict[str, Any]] = []
        for record in self.sought_records():
            locations = [
                dict(r)
                for r in self.db.query(
                    "SELECT url, host_type, version, license, is_best "
                    "FROM oa_location WHERE record_id = ? "
                    "ORDER BY is_best DESC, id",
                    (record["id"],),
                )
            ]
            lookup = self.db.query_one(
                "SELECT * FROM oa_lookup WHERE record_id = ?", (record["id"],)
            )
            try:
                authors = json.loads(record.get("authors") or "[]")
            except ValueError:
                authors = []
            out.append(
                record
                | {
                    "authors": authors,
                    "doi_link": doi_url(record["doi"] or ""),
                    "oa_locations": locations,
                    "looked_up": lookup is not None,
                    "oa_status": lookup["oa_status"] if lookup else "",
                    "lookup_error": lookup["error"] if lookup else "",
                    "status": record.get("status") or "pending",
                }
            )
        return out

    def find_open_access(
        self,
        *,
        client: UnpaywallClient | None = None,
        only_missing: bool = True,
        on_progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        actor: str | None = None,
    ) -> dict[str, int]:
        """Ask Unpaywall where a free copy of each sought report lives."""
        actor = actor or self.audit.actor
        email = (self.project.info().get("contact_email") or "").strip()
        client = client or UnpaywallClient(email)

        records = [r for r in self.sought_records() if r.get("doi")]
        if only_missing:
            done = {
                row["record_id"]
                for row in self.db.query("SELECT record_id FROM oa_lookup")
            }
            records = [r for r in records if r["id"] not in done]

        stats = {"n_looked_up": 0, "n_open_access": 0, "n_locations": 0, "n_errors": 0}
        for index, record in enumerate(records, start=1):
            if should_stop is not None and should_stop():
                break
            result = client.lookup(record["doi"])
            now = utc_now()
            with self.db.transaction():
                self.db.execute(
                    "INSERT INTO oa_lookup(record_id, doi, queried_at, is_oa, "
                    "  oa_status, n_locations, error) VALUES(?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(record_id) DO UPDATE SET queried_at = excluded.queried_at, "
                    "  is_oa = excluded.is_oa, oa_status = excluded.oa_status, "
                    "  n_locations = excluded.n_locations, error = excluded.error",
                    (
                        record["id"], record["doi"], now, 1 if result.is_oa else 0,
                        result.oa_status, len(result.locations), result.error,
                    ),
                )
                for location in result.locations:
                    self.db.execute(
                        "INSERT INTO oa_location(record_id, source, url, host_type, "
                        "  version, license, is_best, found_at) "
                        "VALUES(?, 'unpaywall', ?, ?, ?, ?, ?, ?) "
                        "ON CONFLICT(record_id, url) DO UPDATE SET "
                        "  is_best = excluded.is_best",
                        (
                            record["id"], location.url, location.host_type,
                            location.version, location.license,
                            1 if location.is_best else 0, now,
                        ),
                    )
            stats["n_looked_up"] += 1
            stats["n_locations"] += len(result.locations)
            if result.is_oa:
                stats["n_open_access"] += 1
            if result.error:
                stats["n_errors"] += 1
            if on_progress:
                on_progress(index, len(records), f"Looked up {index} of {len(records)}")

        self.audit.log(
            "retrieve", "open_access_lookup",
            detail=stats | {"source": "unpaywall"}, actor=actor,
        )
        return stats

    # -- matching a file to a record ---------------------------------------

    def _candidates(self) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "doi": r["doi"], "title_norm": r["title_norm"],
             "title": r["title"]}
            for r in self.db.query(
                "SELECT r.id, r.doi, r.title, r.title_norm FROM record r "
                "JOIN screening_status s ON s.record_id = r.id "
                "WHERE r.active = 1 AND s.decision IN ('include', 'unclear')"
            )
        ]

    def match(self, doi: str, title: str) -> MatchResult:
        """Tie a file to a record by DOI, then by title.

        DOI first and alone: a DOI printed in a PDF identifies the work, so no
        title check can improve on it or should be able to override it.
        """
        candidates = self._candidates()
        normalised_doi = normalise_doi(doi)
        if normalised_doi:
            for candidate in candidates:
                if candidate["doi"] and candidate["doi"] == normalised_doi:
                    return MatchResult(candidate["id"], "doi", 1.0,
                                       f"DOI {normalised_doi} printed in the file")

        normalised_title = normalise_title(title)
        if not normalised_title:
            return MatchResult(None, "none", 0.0, "no DOI or title could be read")

        best, best_score = None, 0.0
        for candidate in candidates:
            score = title_similarity(normalised_title, candidate["title_norm"])
            if score > best_score:
                best, best_score = candidate, score
        if best is None:
            return MatchResult(None, "none", 0.0, "there are no records to match against")
        if best_score >= AUTO_MATCH_TITLE_SCORE:
            return MatchResult(best["id"], "title", best_score,
                               f"title matches record {best['id']}",
                               best_candidate_id=best["id"])
        return MatchResult(
            None, "none", best_score,
            f"closest record is {best['id']} at {best_score:.0%} title similarity",
            best_candidate_id=best["id"],
        )

    # -- attaching -----------------------------------------------------------

    def attach_pdf(
        self, path: str | Path, *, record_id: int | None = None, actor: str | None = None
    ) -> AttachOutcome:
        """Store a PDF, match it to a record, and extract its pages.

        ``record_id`` forces the match, which is what the manual screen does.
        """
        actor = actor or self.audit.actor
        path = Path(path)
        if not path.is_file():
            raise RetrievalError(f"No file at {path}")
        outcome = AttachOutcome(filename=path.name)

        try:
            document = extract_pdf(path)
        except PdfError as exc:
            outcome.message = str(exc)
            return outcome

        outcome.n_pages = document.n_pages
        outcome.n_pages_ocr = document.n_pages_ocr
        file_hash = sha256_file(path)

        if record_id is not None:
            result = MatchResult(record_id, "manual", 1.0, "matched by the reviewer")
        else:
            result = self.match(document.doi_guess, document.title_guess)

        if result.record_id is None:
            outcome.unmatched_id = self._store_unmatched(
                path, file_hash, document, result
            )
            outcome.score = result.score
            outcome.message = (
                f"{path.name} could not be matched to a record: {result.detail}. "
                "It is waiting on the manual matching screen."
            )
            return outcome

        existing = self.db.query_one(
            "SELECT record_id, filename FROM report WHERE file_sha256 = ? "
            "AND record_id != ?",
            (file_hash, result.record_id),
        )
        if existing is not None:
            outcome.message = (
                f"{path.name} is already attached to record "
                f"{existing['record_id']} as {existing['filename']}."
            )
            return outcome

        stored = self._store_file(path, result.record_id)
        with self.db.transaction():
            self._ensure_report(result.record_id)
            self.db.execute(
                "UPDATE report SET status = 'retrieved', reason = '', filename = ?, "
                "  stored_path = ?, file_sha256 = ?, n_pages = ?, n_pages_ocr = ?, "
                "  has_text_layer = ?, matched_by = ?, match_score = ?, added_at = ?, "
                "  extracted_at = ? WHERE record_id = ?",
                (
                    path.name, str(stored.relative_to(self.project.root)), file_hash,
                    document.n_pages, document.n_pages_ocr,
                    1 if document.has_text_layer else 0,
                    result.matched_by, result.score, utc_now(), utc_now(),
                    result.record_id,
                ),
            )
            self._store_pages(result.record_id, document)
            self.audit.log(
                "retrieve", "report_attached", entity="record",
                entity_id=result.record_id,
                detail={
                    "filename": path.name,
                    "file_sha256": file_hash,
                    "matched_by": result.matched_by,
                    "match_score": round(result.score, 4),
                    "n_pages": document.n_pages,
                    "n_pages_ocr": document.n_pages_ocr,
                    "warnings": document.warnings[:5],
                },
                actor=actor,
            )
        outcome.record_id = result.record_id
        outcome.matched_by = result.matched_by
        outcome.score = result.score
        outcome.message = (
            f"Matched to record {result.record_id} by {result.matched_by}; "
            f"{document.n_pages} page(s) read"
            + (f", {document.n_pages_ocr} by OCR" if document.n_pages_ocr else "")
            + "."
        )
        return outcome

    def _store_file(self, path: Path, record_id: int) -> Path:
        target = self.project.pdfs_dir / f"{record_id:05d}_{path.name}"
        if target.resolve() != path.resolve():
            shutil.copy2(path, target)
        return target

    def _store_pages(self, record_id: int, document) -> None:
        self.db.execute("DELETE FROM report_page WHERE record_id = ?", (record_id,))
        self.db.executemany(
            "INSERT INTO report_page(record_id, page_no, text, n_chars, ocr) "
            "VALUES(?, ?, ?, ?, ?)",
            [
                (record_id, page.page_no, page.text, page.n_chars, 1 if page.ocr else 0)
                for page in document.pages
            ],
        )

    def _store_unmatched(self, path: Path, file_hash: str, document, result) -> int:
        existing = self.db.query_one(
            "SELECT id FROM unmatched_pdf WHERE file_sha256 = ?", (file_hash,)
        )
        if existing is not None:
            return int(existing["id"])
        stored = self.project.pdfs_dir / "unmatched" / path.name
        stored.parent.mkdir(parents=True, exist_ok=True)
        if stored.resolve() != path.resolve():
            shutil.copy2(path, stored)
        # Only suggest a record when the title was at least somewhat close.
        # Pre-selecting a wild guess on the matching screen invites a reviewer
        # to accept it.
        best_guess = (
            result.best_candidate_id if result.score >= SUGGEST_TITLE_SCORE else None
        )
        return self.db.insert(
            "unmatched_pdf",
            {
                "filename": path.name,
                "stored_path": str(stored.relative_to(self.project.root)),
                "file_sha256": file_hash,
                "detected_doi": document.doi_guess,
                "detected_title": document.title_guess,
                "best_guess_record_id": best_guess,
                "best_guess_score": result.score,
                "added_at": utc_now(),
            },
        )

    def attach_many(
        self,
        paths: Iterable[str | Path],
        *,
        actor: str | None = None,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> list[AttachOutcome]:
        paths = list(paths)
        outcomes: list[AttachOutcome] = []
        for index, path in enumerate(paths, start=1):
            try:
                outcomes.append(self.attach_pdf(path, actor=actor))
            except Exception as exc:
                outcomes.append(AttachOutcome(filename=Path(path).name, message=str(exc)))
            if on_progress:
                on_progress(index, len(paths), f"Added {index} of {len(paths)}")
        return outcomes

    # -- the manual matching screen ----------------------------------------

    def unmatched(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT u.*, r.title AS guess_title FROM unmatched_pdf u "
                "LEFT JOIN record r ON r.id = u.best_guess_record_id "
                "WHERE u.resolved_at IS NULL ORDER BY u.id"
            )
        ]

    def resolve_unmatched(
        self, unmatched_id: int, record_id: int, *, actor: str | None = None
    ) -> AttachOutcome:
        """Attach a file the reviewer has matched by hand."""
        actor = actor or self.audit.actor
        row = self.db.query_one(
            "SELECT * FROM unmatched_pdf WHERE id = ?", (unmatched_id,)
        )
        if row is None:
            raise RetrievalError(f"No unmatched file with id {unmatched_id}")
        path = self.project.root / row["stored_path"]
        outcome = self.attach_pdf(path, record_id=record_id, actor=actor)
        if outcome.matched:
            with self.db.transaction():
                self.db.execute(
                    "UPDATE unmatched_pdf SET resolved_at = ? WHERE id = ?",
                    (utc_now(), unmatched_id),
                )
                self.audit.log(
                    "retrieve", "pdf_matched_by_hand", entity="record",
                    entity_id=record_id,
                    detail={"filename": row["filename"], "unmatched_id": unmatched_id},
                    actor=actor,
                )
        return outcome

    def discard_unmatched(
        self, unmatched_id: int, reason: str = "", *, actor: str | None = None
    ) -> None:
        """Set a file aside as belonging to no record in this review."""
        actor = actor or self.audit.actor
        with self.db.transaction():
            self.db.execute(
                "UPDATE unmatched_pdf SET resolved_at = ? WHERE id = ?",
                (utc_now(), unmatched_id),
            )
            self.audit.log(
                "retrieve", "pdf_discarded", entity="unmatched_pdf",
                entity_id=unmatched_id, detail={"reason": reason}, actor=actor,
            )

    # -- tracking ----------------------------------------------------------

    def set_status(
        self,
        record_id: int,
        status: str,
        *,
        reason: str = "",
        actor: str | None = None,
    ) -> None:
        """Record a report as not retrieved, or as abstract only, with why.

        PRISMA asks for the number of reports not retrieved. A reason is
        required for anything other than 'retrieved', because "not retrieved"
        without one cannot be reported or defended.
        """
        if status not in STATUSES:
            raise RetrievalError(f"{status!r} is not one of {', '.join(STATUSES)}")
        if status in ("not_retrieved", "abstract_only") and not reason.strip():
            raise RetrievalError(
                "Say why the report could not be retrieved. PRISMA asks for "
                "the reports not retrieved, and a count without reasons cannot "
                "be defended."
            )
        actor = actor or self.audit.actor
        with self.db.transaction():
            self._ensure_report(record_id)
            self.db.execute(
                "UPDATE report SET status = ?, reason = ? WHERE record_id = ?",
                (status, reason.strip(), record_id),
            )
            self.audit.log(
                "retrieve", "report_status", entity="record", entity_id=record_id,
                detail={"status": status, "reason": reason.strip()}, actor=actor,
            )

    def pages(self, record_id: int) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT page_no, text, n_chars, ocr FROM report_page "
                "WHERE record_id = ? ORDER BY page_no",
                (record_id,),
            )
        ]

    def report(self, record_id: int) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM report WHERE record_id = ?", (record_id,))
        return dict(row) if row else None

    def counts(self) -> dict[str, int]:
        row = self.db.query_one("SELECT * FROM v_prisma_fulltext")
        return dict(row) if row else {}

    def summary(self) -> dict[str, Any]:
        counts = self.counts()
        sought = self.sought_records()
        return counts | {
            "n_pending": sum(
                1 for r in sought if (r.get("status") or "pending") == "pending"
            ),
            "n_sought": len(sought),
            "contact_email": self.project.info().get("contact_email") or "",
        }
