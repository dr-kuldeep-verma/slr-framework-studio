"""The project database, its schema and the audit chain."""

from __future__ import annotations

import sqlite3

import pytest

from slr_studio import SCHEMA_VERSION
from slr_studio.core.audit import AuditLog
from slr_studio.core.db import Database, MigrationError
from slr_studio.core.hashing import canonical_json, sha256_json, sha256_text
from slr_studio.core.project import DEFAULT_EXCLUSION_CODES, Project, ProjectError


class TestHashing:
    def test_canonical_json_is_key_order_independent(self):
        assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})

    def test_equal_objects_hash_equally(self):
        assert sha256_json({"a": [1, 2]}) == sha256_json({"a": [1, 2]})

    def test_different_objects_hash_differently(self):
        assert sha256_json({"a": 1}) != sha256_json({"a": 2})

    def test_unicode_is_normalised_before_hashing(self):
        # Composed and decomposed forms of the same text must agree, or the
        # response cache would miss depending on which editor typed the input.
        assert sha256_text("café") == sha256_text("café")


class TestDatabase:
    def test_creation_records_the_schema_version(self, tmp_path):
        db = Database(tmp_path / "project.slr", create=True)
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        db.close()

    def test_opening_a_missing_file_fails_clearly(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Database(tmp_path / "nothing.slr")

    def test_a_newer_schema_is_refused_rather_than_guessed_at(self, tmp_path):
        path = tmp_path / "project.slr"
        db = Database(path, create=True)
        db.set_meta("schema_version", str(SCHEMA_VERSION + 5))
        db.close()
        with pytest.raises(MigrationError, match="newer version"):
            Database(path)

    def test_foreign_keys_are_enforced(self, tmp_path):
        db = Database(tmp_path / "project.slr", create=True)
        with pytest.raises(sqlite3.IntegrityError):
            db.insert(
                "record_source",
                {
                    "record_id": 999,
                    "source_database": "Scopus",
                    "import_file_id": 999,
                    "origin_record_id": 999,
                },
            )
        db.close()

    def test_a_failed_transaction_leaves_nothing_behind(self, tmp_path):
        db = Database(tmp_path / "project.slr", create=True)
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.insert("exclusion_code", {"code": "X1", "label": "x", "position": 1})
                raise RuntimeError("something went wrong half way")
        assert db.scalar("SELECT COUNT(*) FROM exclusion_code") == 0
        db.close()


class TestProject:
    def test_creation_lays_out_the_folder(self, tmp_path):
        project = Project.create(tmp_path / "review", "A review")
        for sub in ("imports", "pdfs", "exports", "figures"):
            assert (project.root / sub).is_dir()
        assert (project.root / "project.slr").is_file()
        project.close()

    def test_creation_seeds_the_exclusion_ladder_in_order(self, project):
        codes = project.exclusion_codes()
        assert len(codes) == len(DEFAULT_EXCLUSION_CODES)
        assert [c["position"] for c in codes] == sorted(c["position"] for c in codes)
        assert codes[0]["code"] == "E1"

    def test_creating_over_an_existing_project_is_refused(self, tmp_path):
        Project.create(tmp_path / "review", "A review").close()
        with pytest.raises(ProjectError, match="already exists"):
            Project.create(tmp_path / "review", "A review")

    def test_reopening_reads_back_what_was_saved(self, tmp_path):
        project = Project.create(tmp_path / "review", "A review")
        project.update_info(objectives="Find out what works")
        project.close()
        reopened = Project.open(tmp_path / "review")
        assert reopened.info()["objectives"] == "Find out what works"
        reopened.close()

    def test_unknown_field_names_are_rejected(self, project):
        with pytest.raises(ProjectError, match="Unknown project fields"):
            project.update_info(conclusion="tourism is good")

    def test_archiving_an_import_keeps_a_copy(self, project, fixtures):
        archived = project.archive_import(fixtures / "scopus_sample.ris")
        assert archived.parent == project.imports_dir
        assert archived.read_bytes() == (fixtures / "scopus_sample.ris").read_bytes()

    def test_archiving_a_different_file_of_the_same_name_does_not_overwrite(
        self, project, tmp_path
    ):
        first = tmp_path / "export.ris"
        first.write_text("TY  - JOUR\nTI  - One\nER  - \n", encoding="utf-8")
        a = project.archive_import(first)
        first.write_text("TY  - JOUR\nTI  - Two\nER  - \n", encoding="utf-8")
        b = project.archive_import(first)
        assert a != b
        assert "One" in a.read_text()
        assert "Two" in b.read_text()


class TestAuditLog:
    def test_entries_are_written_in_order(self, project):
        project.audit.log("import", "first")
        project.audit.log("import", "second")
        actions = [e.action for e in project.audit.entries(stage="import")]
        assert actions == ["first", "second"]

    def test_a_fresh_project_already_verifies(self, project):
        assert project.audit.verify() == []

    def test_the_chain_still_verifies_after_many_entries(self, project):
        for i in range(25):
            project.audit.log("dedup", "step", detail={"i": i})
        assert project.audit.verify() == []

    def test_editing_history_breaks_the_chain(self, project):
        project.audit.log("import", "file_imported", detail={"n_records": 100})
        row_id = project.db.scalar("SELECT MAX(id) FROM audit_log")
        project.db.execute(
            "UPDATE audit_log SET detail = ? WHERE id = ?",
            ('{"n_records":999}', row_id),
        )
        breaks = project.audit.verify()
        assert breaks and breaks[0].entry_id == row_id
        assert "does not match its hash" in breaks[0].reason

    def test_deleting_history_breaks_the_chain(self, project):
        for i in range(3):
            project.audit.log("import", f"step{i}")
        middle = project.db.scalar(
            "SELECT id FROM audit_log ORDER BY id DESC LIMIT 1 OFFSET 1"
        )
        project.db.execute("DELETE FROM audit_log WHERE id = ?", (middle,))
        assert project.audit.verify() != []

    def test_an_override_is_recorded_in_both_places(self, project):
        project.audit.log_override(
            "dedup", "dedup_pair", 7, "status", "uncertain", "human_distinct",
            reason="different samples", actor="human:tester",
        )
        row = project.db.query_one("SELECT * FROM human_override")
        assert row["field"] == "status"
        assert row["actor"] == "human:tester"
        assert row["reason"] == "different samples"
        assert any(e.action == "human_override" for e in project.audit.entries())

    def test_actor_defaults_to_the_log_owner(self, project):
        log = AuditLog(project.db, actor="ai:claude-test")
        log.log("screen", "screened")
        assert project.audit.entries()[-1].actor == "ai:claude-test"
