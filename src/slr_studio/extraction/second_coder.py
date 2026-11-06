"""The second coder, and how closely the two agree.

A review that reports "data were extracted by one reviewer and checked by a
second" is making a claim about reliability. This is how that claim becomes a
number: a reproducible sample of reports is coded independently, and agreement
is reported field by field rather than as one figure.

Field by field matters. Two coders almost always agree about the country and
often disagree about the design family, and a single pooled percentage hides
exactly that. Reporting per field tells a reviewer which part of the coding
frame is the ambiguous one.

The comparison is between the first coding and the second, whoever did each.
In the usual case the first is the AI's and the second a person's, but the
arithmetic does not care, and a project where two people code is handled by
the same code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..core.db import utc_now
from ..core.hashing import sha256_text
from ..screening.stats import AgreementResult, agreement

__all__ = ["SecondCoder", "CoderComparison", "REPORT_FIELDS", "RELATIONSHIP_FIELDS"]

#: The report fields compared. Free text (findings, limitations, notes) is left
#: out: comparing prose word for word would report disagreement wherever two
#: people wrote the same thing differently, which is not what the number is
#: supposed to mean.
REPORT_FIELDS = (
    "country",
    "design_family",
    "appraisal_instrument",
    "appraisal_concern",
    "sample_size",
    "time_horizon",
)

#: Compared for relationships that both coders recorded between the same pair
#: of labels.
RELATIONSHIP_FIELDS = (
    "direction",
    "relationship_type",
    "significance",
    "evidence_basis",
)


@dataclass
class CoderComparison:
    n_reports: int = 0
    n_reports_compared: int = 0
    report_fields: dict[str, AgreementResult] = field(default_factory=dict)
    relationship_fields: dict[str, AgreementResult] = field(default_factory=dict)
    n_relationships_matched: int = 0
    n_only_first: int = 0
    n_only_second: int = 0
    #: Times one coder recorded the same pair of labels more than once. Only
    #: the first is compared, so a reviewer should know it happened.
    n_duplicate_pairs: int = 0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_reports": self.n_reports,
            "n_reports_compared": self.n_reports_compared,
            "n_relationships_matched": self.n_relationships_matched,
            "n_only_first": self.n_only_first,
            "n_only_second": self.n_only_second,
            "n_duplicate_pairs": self.n_duplicate_pairs,
            "report_fields": {
                name: result.as_dict() for name, result in self.report_fields.items()
            },
            "relationship_fields": {
                name: result.as_dict()
                for name, result in self.relationship_fields.items()
            },
            "note": self.note,
        }

    def describe(self) -> str:
        if not self.n_reports_compared:
            return "No report has been coded twice yet."
        lines = [
            f"{self.n_reports_compared} report(s) coded independently by both."
        ]
        for name, result in self.report_fields.items():
            lines.append(f"  {name}: {result.describe()}")
        if self.relationship_fields:
            lines.append(
                f"{self.n_relationships_matched} relationship(s) recorded by both "
                f"({self.n_only_first} only by the first, "
                f"{self.n_only_second} only by the second)."
            )
            if self.n_duplicate_pairs:
                lines.append(
                    f"  {self.n_duplicate_pairs} further entry(ies) repeat a pair "
                    "of labels already counted and were left out of the "
                    "comparison."
                )
            for name, result in self.relationship_fields.items():
                lines.append(f"  {name}: {result.describe()}")
        return "\n".join(lines)


def _key(predictor: str, outcome: str) -> str:
    """How two coders' relationships are paired up.

    On the authors' labels, normalised for case and spacing only. Pairing on
    anything looser would quietly match relationships the coders did not
    actually both record, which would make agreement look better than it is.
    """
    return (
        " ".join(str(predictor or "").split()).casefold()
        + " → "
        + " ".join(str(outcome or "").split()).casefold()
    )


class SecondCoder:
    def __init__(self, project) -> None:
        self.project = project
        self.db = project.db
        self.audit = project.audit

    # -- choosing the sample -----------------------------------------------

    def _rng(self, scope: str):
        import random

        info = self.project.info()
        seed = f"{info['created_at']}|{info['title']}|{scope}"
        return random.Random(int(sha256_text(seed)[:16], 16))

    def choose_sample(
        self, fraction: float = 0.2, minimum: int = 3, *, actor: str | None = None
    ) -> list[int]:
        """Pick which reports a second coder will code, reproducibly.

        Same project, same sample, on any machine — so "a random 20% was
        double-coded" is a claim somebody else could check.
        """
        actor = actor or self.audit.actor
        extracted = [
            row["record_id"]
            for row in self.db.query(
                "SELECT record_id FROM report_extraction WHERE coder_role = 'ai' "
                "ORDER BY record_id"
            )
        ]
        if not extracted:
            return []
        n = min(len(extracted), max(minimum, round(len(extracted) * fraction)))
        chosen = sorted(self._rng("second_coder").sample(extracted, n))
        now = utc_now()
        with self.db.transaction():
            for record_id in chosen:
                self.db.execute(
                    "INSERT INTO second_coding_sample(record_id, chosen_at) "
                    "VALUES(?, ?) ON CONFLICT(record_id) DO NOTHING",
                    (record_id, now),
                )
            self.audit.log(
                "extract", "second_coder_sample",
                detail={"n_chosen": len(chosen), "fraction": fraction,
                        "record_ids": chosen},
                actor=actor,
            )
        return chosen

    def sample(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT s.*, r.title, r.year, "
                "       (SELECT COUNT(*) FROM report_extraction e "
                "        WHERE e.record_id = s.record_id "
                "          AND e.coder_role = 'second_coder') AS coded "
                "FROM second_coding_sample s JOIN record r ON r.id = s.record_id "
                "ORDER BY s.record_id"
            )
        ]

    def mark_coded(self, record_id: int) -> None:
        self.db.execute(
            "UPDATE second_coding_sample SET coded_at = ? WHERE record_id = ?",
            (utc_now(), record_id),
        )

    # -- recording the second coding ---------------------------------------

    def record_report(
        self,
        record_id: int,
        extraction,
        *,
        coder: str,
        relationships=None,
    ) -> None:
        """Store a second person's coding of one report.

        It goes in beside the first rather than over it: the point of a second
        coding is the comparison, which needs both.
        """
        from ..ai.client import AIClient
        from ..ai.mock import MockProvider
        from ..fulltext.pdf import PdfPage

        from .engine import ExtractionEngine

        # The engine's storage helpers do the quotation checks and apply the
        # design rule. No model is called on this path, so the provider handed
        # to the client is never used.
        engine = ExtractionEngine(
            self.project, AIClient(self.project, MockProvider(), stage="extract")
        )
        page_objects = [
            PdfPage(page_no=row["page_no"], text=row["text"])
            for row in self.db.query(
                "SELECT page_no, text FROM report_page WHERE record_id = ? "
                "ORDER BY page_no",
                (record_id,),
            )
        ]
        with self.db.transaction():
            engine._store_report(
                None, record_id, "second_coder", coder, extraction, None, page_objects
            )
            self.db.execute(
                "DELETE FROM relationship WHERE record_id = ? "
                "AND coder_role = 'second_coder'",
                (record_id,),
            )
            for position, relationship in enumerate(relationships or [], start=1):
                engine._store_relationship(
                    None, record_id, "second_coder", coder, relationship,
                    position, None, page_objects,
                )
            self.mark_coded(record_id)
            self.audit.log(
                "extract", "second_coding", entity="record", entity_id=record_id,
                detail={"coder": coder, "n_relationships": len(relationships or [])},
                actor=coder,
            )

    # -- comparing ---------------------------------------------------------

    def compare(self, *, store: bool = True, actor: str | None = None) -> CoderComparison:
        """Agreement between the first coding and the second, field by field."""
        actor = actor or self.audit.actor
        pairs = self.db.query(
            "SELECT a.record_id FROM report_extraction a "
            "JOIN report_extraction b ON b.record_id = a.record_id "
            "  AND b.coder_role = 'second_coder' "
            "WHERE a.coder_role = 'ai' ORDER BY a.record_id"
        )
        record_ids = [row["record_id"] for row in pairs]
        comparison = CoderComparison(
            n_reports=int(
                self.db.scalar("SELECT COUNT(*) FROM second_coding_sample") or 0
            ),
            n_reports_compared=len(record_ids),
        )
        if not record_ids:
            comparison.note = (
                "No report has been coded by both yet, so there is nothing to "
                "compare."
            )
            return comparison

        first_rows = {
            row["record_id"]: dict(row)
            for row in self.db.query(
                "SELECT * FROM report_extraction WHERE coder_role = 'ai'"
            )
        }
        second_rows = {
            row["record_id"]: dict(row)
            for row in self.db.query(
                "SELECT * FROM report_extraction WHERE coder_role = 'second_coder'"
            )
        }

        for name in REPORT_FIELDS:
            first = [_as_label(first_rows[r].get(name)) for r in record_ids]
            second = [_as_label(second_rows[r].get(name)) for r in record_ids]
            comparison.report_fields[name] = agreement(first, second)

        matched: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for record_id in record_ids:
            first, duplicates_first = self._by_pair(record_id, "ai")
            second, duplicates_second = self._by_pair(record_id, "second_coder")
            comparison.n_duplicate_pairs += duplicates_first + duplicates_second
            shared = set(first) & set(second)
            comparison.n_only_first += len(set(first) - shared)
            comparison.n_only_second += len(set(second) - shared)
            for key in sorted(shared):
                matched.append((first[key], second[key]))

        comparison.n_relationships_matched = len(matched)
        if matched:
            for name in RELATIONSHIP_FIELDS:
                comparison.relationship_fields[name] = agreement(
                    [_as_label(a.get(name)) for a, _ in matched],
                    [_as_label(b.get(name)) for _, b in matched],
                )
        else:
            comparison.note = (
                "The two coders recorded no relationship between the same pair "
                "of labels, so only the report fields could be compared."
            )

        if store:
            self._store(comparison, actor)
        return comparison

    def _by_pair(
        self, record_id: int, coder_role: str
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """One relationship per pair of labels, and how many were set aside.

        A coder can record the same pair twice — once from the abstract and
        once from the results, say. Only the first can be compared, so the
        rest are counted and reported rather than silently dropped.
        """
        by_pair: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for row in self.db.query(
            "SELECT * FROM relationship WHERE record_id = ? AND coder_role = ? "
            "ORDER BY position, id",
            (record_id, coder_role),
        ):
            key = _key(row["predictor_label"], row["outcome_label"])
            if key in by_pair:
                duplicates += 1
                continue
            by_pair[key] = dict(row)
        return by_pair, duplicates

    def _store(self, comparison: CoderComparison, actor: str) -> None:
        now = utc_now()
        with self.db.transaction():
            for prefix, results in (
                ("report", comparison.report_fields),
                ("relationship", comparison.relationship_fields),
            ):
                for name, result in results.items():
                    self.db.insert(
                        "coder_agreement",
                        {
                            "scope": f"{prefix}:{name}",
                            "n": result.n,
                            "n_agree": result.n_agree,
                            "percent_agreement": result.percent_agreement,
                            "kappa": result.kappa,
                            "pabak": result.pabak,
                            "detail": json.dumps(result.as_dict()),
                            "created_at": now,
                        },
                    )
            self.audit.log(
                "extract", "coder_agreement", detail=comparison.as_dict(), actor=actor
            )

    def reports(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query("SELECT * FROM coder_agreement ORDER BY id")
        ]

    def latest(self) -> list[dict[str, Any]]:
        """The most recent comparison, one row per field."""
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM coder_agreement WHERE created_at = "
                "(SELECT MAX(created_at) FROM coder_agreement) ORDER BY id"
            )
        ]


def _as_label(value: Any) -> str:
    """Everything compared becomes a string, so blanks compare equal.

    A missing sample size and an empty one are the same answer — "not
    recorded" — and should not count as a disagreement.
    """
    if value is None:
        return ""
    return " ".join(str(value).split()).casefold()
