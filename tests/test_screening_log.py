"""The Excel screening log, and the schema migration that made it possible."""

from __future__ import annotations

import json
import sqlite3

import pytest
from openpyxl import load_workbook

from slr_studio import SCHEMA_VERSION
from slr_studio.ai import AIClient, MockProvider, ScriptedProvider
from slr_studio.core.db import Database
from slr_studio.core.schema import BASE_SQL, FULLTEXT_SQL, SCREENING_SQL
from slr_studio.exports import write_screening_log
from slr_studio.screening import ScreeningEngine, ScreeningSettings
from conftest import migrations_from


@pytest.fixture
def screened(imported_project):
    from slr_studio.dedup import DedupEngine

    DedupEngine(imported_project).run()
    imported_project.update_info(
        objectives="Map resident participation in tourism planning.",
        inclusion_criteria="Empirical studies of resident participation.",
    )
    imported_project.protocol.lock(actor="human:test")
    engine = ScreeningEngine(
        imported_project,
        AIClient(imported_project, MockProvider(), stage="screen"),
        ScreeningSettings(),
    )
    engine.waive_pilot("test fixture")
    engine.run_full(actor="human:test")
    engine.run_qc(actor="human:test")
    return imported_project, engine


def sheet_rows(sheet) -> list[list]:
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


class TestScreeningLog:
    @pytest.fixture
    def workbook(self, screened):
        project, _ = screened
        return load_workbook(write_screening_log(project))

    def test_the_four_sheets_are_present(self, workbook):
        assert workbook.sheetnames == [
            "Screening log", "Agreement", "Exclusions", "AI use"
        ]

    def test_one_row_per_active_record_and_no_blank_rows(self, workbook, screened):
        project, _ = screened
        sheet = workbook["Screening log"]
        expected = project.db.scalar("SELECT COUNT(*) FROM record WHERE active = 1")
        assert sheet.max_row == expected + 1
        assert all(row[0] is not None for row in sheet.iter_rows(min_row=2, max_col=1,
                                                                values_only=True))

    def test_every_reviewer_has_its_own_columns(self, workbook):
        headers = [cell.value for cell in workbook["Screening log"][1]]
        for label in ("Screener A", "Screener B", "Adjudicator", "Quality control"):
            assert f"{label}: decision" in headers
            assert f"{label}: reason" in headers

    def test_the_settled_decision_is_recorded_with_how_it_was_reached(self, workbook):
        sheet = workbook["Screening log"]
        headers = [cell.value for cell in sheet[1]]
        column = headers.index("Decided how") + 1
        values = {sheet.cell(row=r, column=column).value for r in range(2, sheet.max_row + 1)}
        assert values <= {"agreement", "adjudication", "qc", "human", "unresolved"}

    def test_the_source_databases_of_merged_records_survive(self, workbook):
        sheet = workbook["Screening log"]
        headers = [cell.value for cell in sheet[1]]
        column = headers.index("Source database(s)") + 1
        values = [sheet.cell(row=r, column=column).value for r in range(2, sheet.max_row + 1)]
        assert any(v and ";" in v for v in values)

    def test_an_unverified_quotation_is_marked_in_the_cell(self, imported_project):
        from slr_studio.dedup import DedupEngine

        DedupEngine(imported_project).run()
        imported_project.protocol.lock(actor="human:test")
        liar = ScriptedProvider(
            default=json.dumps({
                "decision": "include", "reason": "on topic",
                "quote": "words that are nowhere in this record",
            })
        )
        engine = ScreeningEngine(
            imported_project,
            AIClient(imported_project, liar, stage="screen"),
            ScreeningSettings(),
        )
        engine.waive_pilot("test")
        engine.run_full(actor="human:test", limit=2)
        sheet = load_workbook(write_screening_log(imported_project))["Screening log"]
        text = " ".join(
            str(cell.value or "")
            for row in sheet.iter_rows(min_row=2)
            for cell in row
        )
        assert "[NOT FOUND IN RECORD]" in text

    def test_the_agreement_sheet_reports_all_three_figures(self, workbook):
        rows = sheet_rows(workbook["Agreement"])
        assert rows[0][:6] == [
            "Comparison", "Records", "Agreed", "Percentage agreement",
            "Cohen's κ", "PABAK",
        ]
        assert any(row[0] == "full:a_vs_b" for row in rows[1:])

    def test_the_exclusions_sheet_matches_the_prisma_counts(self, workbook, screened):
        project, engine = screened
        rows = sheet_rows(workbook["Exclusions"])
        flat = {str(row[0]): row[2] for row in rows if row[0]}
        assert flat["Records screened"] == engine.counts()["n_to_screen"]
        assert flat["Included"] == engine.counts()["n_included"]

    def test_the_ai_use_sheet_is_built_from_the_call_log(self, workbook, screened):
        project, _ = screened
        rows = sheet_rows(workbook["AI use"])
        assert rows[0][:4] == ["Stage", "Task", "Provider", "Model"]
        tasks = {row[1] for row in rows[1:] if row[1]}
        assert "screen:screener_a" in tasks
        total_calls = sum(row[5] for row in rows[1:] if isinstance(row[5], int))
        assert total_calls == project.db.scalar("SELECT COUNT(*) FROM ai_call")

    def test_writing_the_log_is_itself_logged(self, screened):
        project, _ = screened
        write_screening_log(project)
        assert any(
            e.action == "screening_log_exported" for e in project.audit.entries()
        )
        assert project.audit.verify() == []

    def test_it_lands_in_the_project_exports_folder_by_default(self, screened):
        project, _ = screened
        path = write_screening_log(project)
        assert path.parent == project.exports_dir
        assert path.name == "screening-log.xlsx"


class TestMigration:
    def test_a_version_one_project_is_brought_forward(self, tmp_path):
        # Build a database with only the v1 schema, as the previous release
        # would have written it.
        path = tmp_path / "old.slr"
        connection = sqlite3.connect(path)
        connection.executescript(BASE_SQL)
        connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
        )
        connection.commit()
        connection.close()

        db = Database(path)
        # A v1 project is brought all the way forward, one version at a time.
        assert db.applied_migrations == migrations_from(1)
        assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        tables = {
            r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"screening_run", "screening_decision", "screening_status"} <= tables
        assert {"report", "report_page", "fulltext_status"} <= tables
        db.close()

    def test_migration_keeps_what_the_old_version_wrote(self, tmp_path):
        path = tmp_path / "old.slr"
        connection = sqlite3.connect(path)
        connection.executescript(BASE_SQL)
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version', '1')")
        connection.execute(
            "INSERT INTO import_file(filename, original_path, file_sha256, format, "
            "dialect, source_database, imported_at) "
            "VALUES('old.ris', 'old.ris', 'abc', 'ris', 'scopus', 'Scopus', '2020-01-01T00:00:00Z')"
        )
        connection.commit()
        connection.close()

        db = Database(path)
        assert db.scalar("SELECT filename FROM import_file") == "old.ris"
        db.close()

    def test_opening_a_current_project_migrates_nothing(self, project):
        assert project.db.applied_migrations == []

    def test_a_released_v2_project_gains_the_full_text_tables(self, tmp_path):
        """The queue is rebuilt, and the reviewer's items survive it."""
        path = tmp_path / "v2.slr"
        connection = sqlite3.connect(path)
        connection.executescript(BASE_SQL)
        # review_queue exactly as the v2 release wrote it: no stage column,
        # a narrower CHECK, and a uniqueness rule without the stage.
        connection.executescript(
            "CREATE TABLE review_queue ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " record_id INTEGER NOT NULL REFERENCES record(id),"
            " reason TEXT NOT NULL CHECK (reason IN ('conflict','unclear',"
            "   'qc_rescue','random_audit','quote_failed')),"
            " detail TEXT NOT NULL DEFAULT '', run_id INTEGER,"
            " created_at TEXT NOT NULL, resolved_at TEXT,"
            " resolution TEXT NOT NULL DEFAULT '');"
            "CREATE UNIQUE INDEX ux_review_queue_open"
            "  ON review_queue(record_id, reason) WHERE resolved_at IS NULL;"
            "CREATE TABLE screening_run (id INTEGER PRIMARY KEY);"
            "CREATE TABLE screening_status (record_id INTEGER PRIMARY KEY,"
            " decision TEXT NOT NULL, exclusion_code TEXT DEFAULT '');"
        )
        connection.execute(
            "INSERT INTO import_file(filename, original_path, file_sha256, format, "
            "dialect, source_database, imported_at) "
            "VALUES('old.ris','old.ris','abc','ris','scopus','Scopus','2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO record(id, import_file_id, position_in_file, raw, raw_format, "
            "record_hash, created_at, title) "
            "VALUES(7, 1, 1, 'raw', 'ris', 'h', '2026-01-01T00:00:00Z', 'A record')"
        )
        connection.execute(
            "INSERT INTO review_queue(record_id, reason, detail, created_at) "
            "VALUES(7, 'conflict', 'an item the reviewer had not got to', "
            "'2026-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','2')")
        connection.commit()
        connection.close()

        db = Database(path)
        try:
            assert db.applied_migrations == migrations_from(2)
            kept = db.query_one("SELECT record_id, reason, detail, stage FROM review_queue")
            assert kept["detail"] == "an item the reviewer had not got to"
            assert kept["stage"] == "screen"
            # The same record can now be queued again at full text.
            db.insert(
                "review_queue",
                {"record_id": 7, "reason": "conflict", "stage": "fulltext",
                 "created_at": "2026-02-01T00:00:00Z"},
            )
            assert db.scalar("SELECT COUNT(*) FROM review_queue") == 2
            assert list(db.query("PRAGMA foreign_key_check")) == []
            tables = {
                r[0] for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {"report", "report_page", "fulltext_status", "oa_location"} <= tables
        finally:
            db.close()

    def test_the_queue_is_rebuilt_once_more_for_extraction(self, tmp_path):
        """A v3 project gains the reasons extraction needs, keeping its rows."""
        path = tmp_path / "v3.slr"
        connection = sqlite3.connect(path)
        connection.executescript(BASE_SQL + SCREENING_SQL + FULLTEXT_SQL)
        # Put the queue back into the shape the v3 release wrote: the stage
        # column, but none of the reasons extraction needs.
        connection.executescript(
            "DROP VIEW v_prisma_screening;"
            "DROP VIEW v_prisma_fulltext;"
            "DROP TABLE review_queue;"
            "CREATE TABLE review_queue ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " record_id INTEGER NOT NULL REFERENCES record(id),"
            " reason TEXT NOT NULL CHECK (reason IN ('conflict','unclear',"
            "   'qc_rescue','random_audit','quote_failed','confirm_inclusion')),"
            " detail TEXT NOT NULL DEFAULT '', run_id INTEGER,"
            " stage TEXT NOT NULL DEFAULT 'screen',"
            " created_at TEXT NOT NULL, resolved_at TEXT,"
            " resolution TEXT NOT NULL DEFAULT '');"
            "CREATE UNIQUE INDEX ux_review_queue_open"
            "  ON review_queue(record_id, reason, stage) WHERE resolved_at IS NULL;"
        )
        connection.execute(
            "INSERT INTO import_file(filename, original_path, file_sha256, format, "
            "dialect, source_database, imported_at) "
            "VALUES('f.ris','f.ris','h','ris','scopus','Scopus','2026-01-01T00:00:00Z')"
        )
        connection.execute(
            "INSERT INTO record(id, import_file_id, position_in_file, raw, raw_format, "
            "record_hash, created_at, title) "
            "VALUES(5, 1, 1, 'raw', 'ris', 'h', '2026-01-01T00:00:00Z', 'A record')"
        )
        connection.execute(
            "INSERT INTO review_queue(record_id, reason, detail, stage, created_at) "
            "VALUES(5, 'conflict', 'an open item', 'fulltext', '2026-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','3')")
        connection.commit()
        connection.close()

        db = Database(path)
        try:
            assert db.applied_migrations == migrations_from(3)
            kept = db.query_one(
                "SELECT detail, stage FROM review_queue WHERE record_id = 5"
            )
            assert kept["detail"] == "an open item" and kept["stage"] == "fulltext"
            # An extraction reason is now allowed, and the views still work.
            db.insert(
                "review_queue",
                {"record_id": 5, "reason": "direction_mismatch", "stage": "extract",
                 "created_at": "2026-02-01T00:00:00Z"},
            )
            assert db.query_one("SELECT * FROM v_prisma_screening") is not None
            assert db.query_one("SELECT * FROM v_prisma_fulltext") is not None
            assert db.query_one("SELECT * FROM v_extraction_progress") is not None
        finally:
            db.close()
