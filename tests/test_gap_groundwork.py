"""The Gap Finder's groundwork: keywords, future-research rows, gap register.

None of this is filled in yet — the Gap Finder arrives in a later build. What
is tested here is that the data it will need is being recorded *now*, because
retrofitting it after a review has been coded would mean coding it again.
"""

from __future__ import annotations

import sqlite3

import pytest

from slr_studio import SCHEMA_VERSION
from slr_studio.core.db import Database
from slr_studio.core.db import utc_now
from slr_studio.core.schema import (
    BASE_SQL,
    EXTRACTION_SQL,
    FULLTEXT_SQL,
    SCREENING_SQL,
)
from slr_studio.dedup import DedupEngine
from slr_studio.importers.service import ImportService
from conftest import migrations_from


# -- keywords --------------------------------------------------------------


class TestKeywordIndex:
    def test_keywords_are_indexed_at_import(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        rows = project.db.query(
            "SELECT keyword, keyword_norm FROM record_keyword ORDER BY id"
        )
        assert rows
        assert all(row["keyword_norm"] for row in rows)

    def test_the_raw_json_on_the_record_is_left_alone(self, project, fixtures):
        import json

        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        stored = json.loads(
            project.db.scalar(
                "SELECT keywords FROM record WHERE doi = ?",
                ("10.1080/jsts.2021.004512",),
            )
        )
        assert "community participation" in stored

    def test_keywords_are_normalised_for_matching(self, project, fixtures):
        ImportService(project).import_file(fixtures / "wos_sample.ris")
        norms = {
            row["keyword_norm"]
            for row in project.db.query("SELECT keyword_norm FROM record_keyword")
        }
        # Web of Science shouts its keywords; the normalised form does not.
        assert "community participation" in norms

    def test_a_record_repeating_a_keyword_indexes_it_once(self, project, tmp_path):
        path = tmp_path / "repeats.ris"
        path.write_text(
            "TY  - JOUR\nTI  - A study of trust\nPY  - 2020\n"
            "KW  - Trust\nKW  - trust\nKW  - TRUST\nER  - \n",
            encoding="utf-8",
        )
        ImportService(project).import_file(path)
        assert project.db.scalar("SELECT COUNT(*) FROM record_keyword") == 1

    def test_a_merged_record_gives_its_keywords_to_the_survivor(
        self, project, fixtures
    ):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        service.import_file(fixtures / "wos_sample.ris")
        DedupEngine(project).run()

        kept = project.db.scalar(
            "SELECT id FROM record WHERE doi = ? AND active = 1",
            ("10.1080/jsts.2021.004512",),
        )
        sources = project.db.scalar(
            "SELECT COUNT(DISTINCT from_record_id) FROM record_keyword "
            "WHERE record_id = ?",
            (kept,),
        )
        # The survivor carries keywords from both databases' copies.
        assert sources == 2

    def test_rerunning_deduplication_does_not_multiply_keywords(
        self, project, fixtures
    ):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        service.import_file(fixtures / "wos_sample.ris")
        DedupEngine(project).run()
        after_first = project.db.scalar("SELECT COUNT(*) FROM record_keyword")
        DedupEngine(project).run()
        DedupEngine(project).run()
        assert project.db.scalar("SELECT COUNT(*) FROM record_keyword") == after_first

    def test_undoing_a_merge_takes_the_borrowed_keywords_back(
        self, project, fixtures
    ):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        service.import_file(fixtures / "wos_sample.ris")
        engine = DedupEngine(project)
        engine.run()
        borrowed = project.db.scalar(
            "SELECT COUNT(*) FROM record_keyword WHERE record_id != from_record_id"
        )
        assert borrowed > 0
        engine._reset_merges()
        assert project.db.scalar(
            "SELECT COUNT(*) FROM record_keyword WHERE record_id != from_record_id"
        ) == 0


class TestQuickScanInput:
    @pytest.fixture
    def scanned(self, project, fixtures):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        service.import_file(fixtures / "wos_sample.ris")
        DedupEngine(project).run()
        return project

    def test_it_lists_only_records_still_in_play(self, scanned):
        assert scanned.db.scalar("SELECT COUNT(*) FROM v_quick_scan_input") == (
            scanned.db.scalar("SELECT COUNT(*) FROM record WHERE active = 1")
        )

    def test_it_carries_the_text_to_cluster(self, scanned):
        row = scanned.db.query_one(
            "SELECT * FROM v_quick_scan_input WHERE keywords IS NOT NULL LIMIT 1"
        )
        assert row["title"] and row["keywords"]

    def test_it_carries_the_year_to_trend(self, scanned):
        years = [
            row["year"]
            for row in scanned.db.query("SELECT year FROM v_quick_scan_input")
        ]
        assert years and all(y is None or 1900 < y < 2200 for y in years)

    def test_a_record_with_no_year_is_listed_not_hidden(self, project, tmp_path):
        path = tmp_path / "undated.ris"
        path.write_text(
            "TY  - JOUR\nTI  - An undated study of trust\nKW  - trust\nER  - \n",
            encoding="utf-8",
        )
        ImportService(project).import_file(path)
        row = project.db.query_one("SELECT * FROM v_quick_scan_input")
        assert row is not None and row["year"] is None

    def test_each_keyword_appears_once_whatever_its_spelling(self, scanned):
        row = scanned.db.query_one(
            "SELECT keywords, n_keywords FROM v_quick_scan_input "
            "WHERE n_keywords > 1 ORDER BY n_keywords DESC LIMIT 1"
        )
        listed = [k.strip() for k in (row["keywords"] or "").split(";") if k.strip()]
        assert len(listed) == row["n_keywords"]
        assert len({k.casefold() for k in listed}) == len(listed)


# -- the gap register ------------------------------------------------------


class TestGapRegister:
    def test_a_gap_can_be_stored_with_its_counts_and_records(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        record_id = project.db.scalar("SELECT id FROM record LIMIT 1")
        gap_id = project.db.insert(
            "gap_register",
            {
                "gap_type": "evidence",
                "statement": "No dataset measures efficacy together with "
                             "climate concern (0 of 89 datasets).",
                "corpus_searched_to": "2026-03-01",
                "supporting_counts": '{"n_datasets": 0, "n_total": 89}',
                "cell_key": "efficacy x climate concern",
                "created_at": utc_now(),
            },
        )
        project.db.insert(
            "gap_evidence",
            {"gap_id": gap_id, "kind": "record", "record_id": record_id,
             "role": "counts_toward"},
        )
        row = project.db.query_one("SELECT * FROM v_gap_register WHERE id = ?", (gap_id,))
        assert row["n_evidence"] == 1 and row["n_records"] == 1

    def test_a_gap_with_no_evidence_is_visibly_unsupported(self, project):
        gap_id = project.db.insert(
            "gap_register",
            {"gap_type": "theoretical", "statement": "A claim with nothing behind it.",
             "created_at": utc_now()},
        )
        row = project.db.query_one("SELECT * FROM v_gap_register WHERE id = ?", (gap_id,))
        assert row["n_evidence"] == 0

    def test_an_invented_gap_type_is_refused(self, project):
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "gap_register",
                {"gap_type": "vibes", "statement": "x", "created_at": utc_now()},
            )

    def test_freshness_starts_unchecked(self, project):
        gap_id = project.db.insert(
            "gap_register",
            {"gap_type": "evidence", "statement": "x", "created_at": utc_now()},
        )
        assert project.db.scalar(
            "SELECT freshness_status FROM gap_register WHERE id = ?", (gap_id,)
        ) == "not_checked"

    def test_the_four_priority_criteria_each_have_a_column(self, project):
        columns = {
            row[1] for row in project.db.query("PRAGMA table_info(gap_register)")
        }
        assert {
            "score_centrality", "score_size", "score_relevance", "score_feasibility"
        } <= columns

    def test_a_reviewer_override_has_somewhere_to_live(self, project):
        gap_id = project.db.insert(
            "gap_register",
            {"gap_type": "evidence", "statement": "x", "priority": 0.4,
             "created_at": utc_now()},
        )
        project.db.execute(
            "UPDATE gap_register SET score_overrides = ?, override_reason = ?, "
            "overridden_by = ?, overridden_at = ?, priority = ? WHERE id = ?",
            ('{"centrality": 0.9}', "central to two propositions", "human:test",
             utc_now(), 0.8, gap_id),
        )
        row = project.db.query_one("SELECT * FROM gap_register WHERE id = ?", (gap_id,))
        assert row["overridden_by"] == "human:test" and row["priority"] == 0.8


class TestFutureResearchStatements:
    def test_a_statement_keeps_its_page(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        record_id = project.db.scalar("SELECT id FROM record LIMIT 1")
        row_id = project.db.insert(
            "future_research",
            {
                "record_id": record_id,
                "kind": "future_research",
                "statement": "Longitudinal designs are needed to establish direction.",
                "quote": "Future work should follow households over time",
                "quote_page": 9,
                "created_at": utc_now(),
            },
        )
        row = project.db.query_one("SELECT * FROM future_research WHERE id = ?", (row_id,))
        assert row["quote_page"] == 9 and row["kind"] == "future_research"

    def test_limitations_are_stored_apart_from_future_work(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        record_id = project.db.scalar("SELECT id FROM record LIMIT 1")
        for kind in ("future_research", "limitation"):
            project.db.insert(
                "future_research",
                {"record_id": record_id, "kind": kind, "statement": kind,
                 "created_at": utc_now()},
            )
        kinds = {
            r["kind"] for r in project.db.query("SELECT kind FROM future_research")
        }
        assert kinds == {"future_research", "limitation"}

    def test_an_invented_kind_is_refused(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        record_id = project.db.scalar("SELECT id FROM record LIMIT 1")
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "future_research",
                {"record_id": record_id, "kind": "speculation", "statement": "x",
                 "created_at": utc_now()},
            )

    def test_nothing_fills_these_in_yet(self, project, fixtures):
        """The Gap Finder populates them; extraction does not, by design."""
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        assert project.db.scalar("SELECT COUNT(*) FROM future_research") == 0


# -- migration -------------------------------------------------------------


class TestMigrationToV5:
    def _v4_project(self, path, *, with_keywords: bool = True) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            BASE_SQL + SCREENING_SQL + FULLTEXT_SQL + EXTRACTION_SQL
        )
        connection.execute(
            "INSERT INTO import_file(filename, original_path, file_sha256, format, "
            "dialect, source_database, imported_at) "
            "VALUES('old.ris','old.ris','abc','ris','scopus','Scopus',"
            "'2026-01-01T00:00:00Z')"
        )
        keywords = '["Trust", "Co-management"]' if with_keywords else "[]"
        connection.execute(
            "INSERT INTO record(id, import_file_id, position_in_file, raw, raw_format, "
            "record_hash, created_at, title, keywords, source_database, year) "
            "VALUES(1, 1, 1, 'raw', 'ris', 'h', '2026-01-01T00:00:00Z', "
            "'A kept record', ?, 'Scopus', 2021)",
            (keywords,),
        )
        connection.execute(
            "INSERT INTO record(id, import_file_id, position_in_file, raw, raw_format, "
            "record_hash, created_at, title, keywords, source_database, year, "
            "active, duplicate_of) "
            "VALUES(2, 1, 2, 'raw', 'ris', 'h2', '2026-01-01T00:00:00Z', "
            "'A merged record', '[\"Participation\"]', 'Web of Science', 2021, 0, 1)"
        )
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','4')")
        connection.commit()
        connection.close()

    def test_a_v4_project_is_brought_forward(self, tmp_path):
        path = tmp_path / "v4.slr"
        self._v4_project(path)
        db = Database(path)
        try:
            assert db.applied_migrations == migrations_from(4)
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            tables = {
                r[0] for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {"gap_register", "gap_evidence", "future_research",
                    "record_keyword"} <= tables
        finally:
            db.close()

    def test_what_the_old_build_wrote_is_still_there(self, tmp_path):
        path = tmp_path / "v4.slr"
        self._v4_project(path)
        db = Database(path)
        try:
            assert db.scalar("SELECT title FROM record WHERE id = 1") == "A kept record"
            assert db.scalar("SELECT filename FROM import_file") == "old.ris"
        finally:
            db.close()

    def test_existing_keywords_are_backfilled_into_the_index(self, tmp_path):
        path = tmp_path / "v4.slr"
        self._v4_project(path)
        db = Database(path)
        try:
            norms = {
                r["keyword_norm"]
                for r in db.query(
                    "SELECT keyword_norm FROM record_keyword WHERE record_id = 1"
                )
            }
            # Its own two, plus the merged record's.
            assert norms == {"trust", "co management", "participation"}
        finally:
            db.close()

    def test_the_backfill_does_not_run_twice(self, tmp_path):
        path = tmp_path / "v4.slr"
        self._v4_project(path)
        db = Database(path)
        first = db.scalar("SELECT COUNT(*) FROM record_keyword")
        db.close()
        db = Database(path)
        try:
            assert db.scalar("SELECT COUNT(*) FROM record_keyword") == first
            assert db.applied_migrations == []
        finally:
            db.close()

    def test_a_project_with_no_keywords_migrates_cleanly(self, tmp_path):
        path = tmp_path / "v4.slr"
        self._v4_project(path, with_keywords=False)
        db = Database(path)
        try:
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            assert db.query_one("SELECT * FROM v_quick_scan_input") is not None
        finally:
            db.close()

    def test_a_v1_project_still_arrives_at_the_current_version(self, tmp_path):
        path = tmp_path / "v1.slr"
        connection = sqlite3.connect(path)
        connection.executescript(BASE_SQL)
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','1')")
        connection.commit()
        connection.close()
        db = Database(path)
        try:
            assert db.applied_migrations == migrations_from(1)
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            db.close()
