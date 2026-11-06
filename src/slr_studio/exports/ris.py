"""Writing records back out as RIS.

The point of this export is that another tool can read it, so the test that
matters is the round trip: the file goes back through this application's own
importer, which is the strictest reader to hand, and comes out with the same
titles and DOIs.

One detail does the real work. RIS is line-oriented, and a field containing a
newline -- which abstracts routinely do -- produces lines that no parser can
tell from a new tag. They are folded onto continuation lines instead, which is
what the format expects and what the importer reads back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

__all__ = ["STAGES", "write_ris"]

#: Which set of records each stage writes, as a SQL condition over `record r`.
STAGES: dict[str, tuple[str, str]] = {
    "all": (
        "r.active = 1",
        "every record left after deduplication",
    ),
    "screened_in": (
        "r.active = 1 AND EXISTS (SELECT 1 FROM screening_status s "
        "WHERE s.record_id = r.id AND s.decision IN ('include','unclear'))",
        "records that passed title and abstract screening",
    ),
    "included": (
        "EXISTS (SELECT 1 FROM fulltext_status f WHERE f.record_id = r.id "
        "AND f.decision = 'include')",
        "studies included in the review",
    ),
    "excluded_fulltext": (
        "EXISTS (SELECT 1 FROM fulltext_status f WHERE f.record_id = r.id "
        "AND f.decision = 'exclude')",
        "reports excluded at full text, with their reasons",
    ),
}

#: How a record's columns map onto RIS tags, in the order they are written.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("TI", "title"),
    ("T2", "journal"),
    ("PY", "year"),
    ("VL", "volume"),
    ("IS", "issue"),
    ("SP", "pages"),
    ("DO", "doi"),
    ("SN", "issn"),
    ("UR", "url"),
    ("LA", "language"),
    ("PB", "publisher"),
    ("AB", "abstract"),
)

#: RIS reference types, from the document type the importer recorded.
_TYPES = {
    "article": "JOUR",
    "journal article": "JOUR",
    "conference paper": "CPAPER",
    "conference": "CPAPER",
    "book": "BOOK",
    "book chapter": "CHAP",
    "chapter": "CHAP",
    "thesis": "THES",
    "report": "RPRT",
    "review": "JOUR",
}


def _type_for(doc_type: str | None) -> str:
    return _TYPES.get((doc_type or "").strip().casefold(), "JOUR")


def _fold(tag: str, value: str) -> list[str]:
    """One RIS field, with newlines folded onto continuation lines.

    A raw newline inside a value would look to any reader like the start of
    an untagged line, which is how an abstract ends up truncated or spilling
    into the next record.
    """
    lines = [part.strip() for part in str(value).splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return []
    out = [f"{tag}  - {lines[0]}"]
    out.extend(f"      {line}" for line in lines[1:])
    return out


def _authors(raw: str | None) -> Iterable[str]:
    for part in (raw or "").replace(";", "\n").splitlines():
        name = part.strip()
        if name:
            yield name


def _keywords(raw: str | None) -> Iterable[str]:
    import json

    try:
        entries = json.loads(raw or "[]")
    except (TypeError, ValueError):
        entries = [k.strip() for k in (raw or "").split(";")]
    for entry in entries:
        text = str(entry).strip()
        if text:
            yield text


def _record_to_ris(row: dict[str, Any]) -> list[str]:
    lines = [f"TY  - {_type_for(row.get('doc_type'))}"]
    for name in _authors(row.get("authors")):
        lines.extend(_fold("AU", name))
    for tag, column in _FIELDS:
        value = row.get(column)
        if value in (None, ""):
            continue
        lines.extend(_fold(tag, value))
    for keyword in _keywords(row.get("keywords")):
        lines.extend(_fold("KW", keyword))
    if row.get("source_database"):
        lines.extend(_fold("DB", row["source_database"]))
    lines.append("ER  - ")
    lines.append("")
    return lines


def write_ris(project, path: str | Path, *, stage: str = "included") -> Path:
    """Write one stage of the flow as a RIS file."""
    if stage not in STAGES:
        raise ValueError(
            f"Unknown stage {stage!r}. The stages are: "
            + ", ".join(sorted(STAGES))
            + "."
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    condition, _description = STAGES[stage]

    lines: list[str] = []
    for row in project.db.query(
        f"SELECT * FROM record r WHERE {condition} ORDER BY r.id"
    ):
        lines.extend(_record_to_ris(dict(row)))

    # A trailing newline and no BOM: the format is plain text, and a BOM is
    # the commonest reason another tool rejects the first record.
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
