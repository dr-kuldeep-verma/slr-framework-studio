"""Project lifecycle: the folder on disk and the database inside it."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .db import Database, utc_now

__all__ = ["Project", "DEFAULT_EXCLUSION_CODES", "ProjectError"]

PROJECT_DB_NAME = "project.slr"

#: A starter ladder of exclusion codes, ordered cheapest and most objective
#: first so screening spends the fewest tokens on the easiest rejections. The
#: protocol form in stage 2 is where the reviewer rewrites these for the review
#: at hand; they are a scaffold, not a recommendation.
DEFAULT_EXCLUSION_CODES: tuple[tuple[str, str, str], ...] = (
    ("E1", "Not a research report", "Editorial, note, erratum, conference abstract only."),
    ("E2", "Out of date range", "Published outside the protocol's date limits."),
    ("E3", "Language", "Not written in a protocol language."),
    ("E4", "Wrong population", "Participants do not match the protocol population."),
    ("E5", "Wrong context", "Setting does not match the protocol context."),
    ("E6", "Wrong concept", "Does not address the protocol concept or phenomenon."),
    ("E7", "No usable outcome", "Reports no outcome or relationship the review can use."),
    ("E8", "Full text unavailable", "No retrievable full text after a documented search."),
)


class ProjectError(RuntimeError):
    pass


@dataclass
class Project:
    """An open review: its folder, its database and its audit log."""

    root: Path
    db: Database
    audit: AuditLog

    # -- construction ------------------------------------------------------

    @classmethod
    def create(
        cls,
        root: str | Path,
        title: str,
        *,
        actor: str = "human:unknown",
        objectives: str = "",
        research_questions: list[str] | None = None,
        framework_type: str = "PCC",
        inclusion_criteria: str = "",
        contact_email: str = "",
        exclusion_codes: list[tuple[str, str, str]] | None = None,
    ) -> Project:
        root = Path(root)
        db_path = root / PROJECT_DB_NAME
        if db_path.exists():
            raise ProjectError(f"A project already exists at {db_path}")
        for sub in ("imports", "pdfs", "exports", "figures"):
            (root / sub).mkdir(parents=True, exist_ok=True)

        db = Database(db_path, create=True)
        audit = AuditLog(db, actor=actor)
        now = utc_now()
        with db.transaction():
            db.insert(
                "project",
                {
                    "id": 1,
                    "title": title,
                    "objectives": objectives,
                    "research_questions": json.dumps(research_questions or []),
                    "framework_type": framework_type,
                    "inclusion_criteria": inclusion_criteria,
                    "languages": json.dumps([]),
                    "contact_email": contact_email,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            for position, (code, label, definition) in enumerate(
                exclusion_codes or DEFAULT_EXCLUSION_CODES, start=1
            ):
                db.insert(
                    "exclusion_code",
                    {
                        "code": code,
                        "label": label,
                        "definition": definition,
                        "position": position,
                    },
                )
            audit.log(
                "project",
                "project_created",
                entity="project",
                entity_id=1,
                detail={"title": title, "framework_type": framework_type},
            )
        return cls(root=root, db=db, audit=audit)

    @classmethod
    def open(cls, path: str | Path, *, actor: str = "human:unknown") -> Project:
        """Open by project folder or by the `project.slr` file itself."""
        path = Path(path)
        db_path = path / PROJECT_DB_NAME if path.is_dir() else path
        if not db_path.exists():
            raise ProjectError(f"No project database at {db_path}")
        db = Database(db_path)
        return cls(root=db_path.parent, db=db, audit=AuditLog(db, actor=actor))

    # -- paths -------------------------------------------------------------

    @property
    def imports_dir(self) -> Path:
        return self._sub("imports")

    @property
    def pdfs_dir(self) -> Path:
        return self._sub("pdfs")

    @property
    def exports_dir(self) -> Path:
        return self._sub("exports")

    @property
    def figures_dir(self) -> Path:
        return self._sub("figures")

    def _sub(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def archive_import(self, source: str | Path) -> Path:
        """Keep a verbatim copy of an imported file inside the project.

        Exports and the audit trail refer to this copy, so the review stays
        reproducible even if the reviewer tidies up their Downloads folder.
        """
        source = Path(source)
        target = self.imports_dir / source.name
        counter = 1
        while target.exists() and target.read_bytes() != source.read_bytes():
            target = self.imports_dir / f"{source.stem}({counter}){source.suffix}"
            counter += 1
        if not target.exists():
            shutil.copy2(source, target)
        return target

    # -- project record ----------------------------------------------------

    def info(self) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM project WHERE id = 1")
        if row is None:
            raise ProjectError("Project row missing from the database")
        data = dict(row)
        data["research_questions"] = json.loads(data["research_questions"] or "[]")
        data["languages"] = json.loads(data["languages"] or "[]")
        return data

    def update_info(self, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "title",
            "objectives",
            "research_questions",
            "framework_type",
            "framework_approach",
            "a_priori_constructs",
            "a_priori_theory",
            "a_priori_citation",
            "inclusion_criteria",
            "date_from",
            "date_to",
            "languages",
            "contact_email",
            "searched_to",
            "theories",
            "stimulus_attributes",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ProjectError(f"Unknown project fields: {sorted(unknown)}")
        payload = dict(fields)
        if "framework_type" in payload:
            # What the dropped CHECK constraint used to do, done where it can
            # also see the reviewer's own frameworks. Accepting a name the
            # catalogue has no stages for would silently switch off
            # stage-transition gap detection.
            self._check_framework_name(str(payload["framework_type"]))
        for key in ("research_questions", "languages"):
            if key in payload and not isinstance(payload[key], str):
                payload[key] = json.dumps(payload[key])
        payload["updated_at"] = utc_now()
        assignments = ", ".join(f"{k} = :{k}" for k in payload)
        with self.db.transaction():
            self.db.execute(f"UPDATE project SET {assignments} WHERE id = 1", payload)
            self.audit.log(
                "project",
                "project_updated",
                entity="project",
                entity_id=1,
                detail={"fields": sorted(fields)},
            )

    # -- question frameworks -----------------------------------------------

    def frameworks(self) -> list[dict[str, Any]]:
        """Every framework this project may use, built-in and its own."""
        from .frameworks import BUILT_IN

        out: list[dict[str, Any]] = [
            {"name": name, "stages": stages, "is_built_in": True, "note": ""}
            for name, stages in BUILT_IN.items()
        ]
        out.extend(
            {
                "name": row["name"],
                "stages": tuple(json.loads(row["stages"])),
                "is_built_in": False,
                "note": row["note"],
            }
            for row in self.db.query(
                "SELECT * FROM framework_definition ORDER BY name"
            )
        )
        return out

    def define_framework(
        self,
        name: str,
        stages: str | list[str] | tuple[str, ...],
        *,
        note: str = "",
        actor: str | None = None,
    ) -> tuple[str, ...]:
        """Record a question framework of the reviewer's own.

        Audited, not stored as a preference: it changes which transitions
        the Gap Finder looks for, and therefore what the review reports.
        """
        from .frameworks import normalise_name, parse_stages, validate_definition

        clean = " ".join((name or "").split())
        ordered = (
            parse_stages(stages)
            if isinstance(stages, str)
            else parse_stages("\n".join(stages))
        )
        existing = [
            row["name_norm"]
            for row in self.db.query("SELECT name_norm FROM framework_definition")
        ]
        validate_definition(clean, ordered, existing=existing)

        with self.db.transaction():
            self.db.insert(
                "framework_definition",
                {
                    "name": clean,
                    "name_norm": normalise_name(clean),
                    "stages": json.dumps(list(ordered)),
                    "note": note,
                    "created_at": utc_now(),
                },
            )
            self.audit.log(
                "project",
                "define_framework",
                entity="project",
                entity_id=1,
                detail={"name": clean, "stages": list(ordered)},
                actor=actor,
            )
        return ordered

    def framework_stages(self, name: str | None = None) -> tuple[str, ...]:
        """The ordered stages of *name*, or of the project's own framework.

        Empty for a name nothing defines, which is a state the Gap Finder
        reports rather than treating as "no gaps".
        """
        from .frameworks import BUILT_IN, normalise_name

        wanted = name if name is not None else self.info()["framework_type"]
        for built_in, stages in BUILT_IN.items():
            if normalise_name(built_in) == normalise_name(wanted):
                return stages
        row = self.db.query_one(
            "SELECT stages FROM framework_definition WHERE name_norm = ?",
            (normalise_name(wanted),),
        )
        return tuple(json.loads(row["stages"])) if row else ()

    def _check_framework_name(self, name: str) -> None:
        from .frameworks import FrameworkError

        if self.framework_stages(name):
            return
        known = ", ".join(sorted(f["name"] for f in self.frameworks()))
        raise FrameworkError(
            f"{name!r} is not a framework this review has defined, so the "
            "Gap Finder would have no stages to look for transitions "
            f"between. Define it with its stages first, or use one of: "
            f"{known}."
        )

    @property
    def protocol(self):
        """The protocol service for this project (drafting, locking, amendments)."""
        from .protocol import ProtocolService

        if getattr(self, "_protocol", None) is None:
            self._protocol = ProtocolService(self)
        return self._protocol

    def exclusion_codes(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM exclusion_code WHERE active = 1 ORDER BY position"
            )
        ]

    # -- counts used by the UI and the PRISMA diagram ----------------------

    def counts(self) -> dict[str, int]:
        row = self.db.query_one("SELECT * FROM v_prisma_identification")
        return dict(row) if row else {}

    def counts_per_database(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.db.query(
                "SELECT * FROM v_records_per_database ORDER BY source_database"
            )
        ]

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Project:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
