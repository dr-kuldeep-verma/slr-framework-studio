"""What the Gap Finder needs recorded, and that older projects still open.

The field that matters most is the search date. Every claim of absence the
Gap Finder makes is qualified "within the included corpus, searched to
[date]", and a gap statement without that date is an unqualified claim about
the whole literature, which is exactly what a reviewer must never publish.
"""

from __future__ import annotations

import sqlite3

import pytest

from slr_studio import SCHEMA_VERSION
from slr_studio.core.db import Database, utc_now
from slr_studio.core.schema import (
    BASE_SQL,
    EXTRACTION_SQL,
    FRAMEWORK_SQL,
    FULLTEXT_SQL,
    GAP_SQL,
    SCREENING_SQL,
    SYNTHESIS_SQL,
)


class TestTheSearchDate:
    def test_a_project_records_when_the_search_was_run(self, project):
        project.update_info(searched_to="2026-03-01")
        assert project.info()["searched_to"] == "2026-03-01"

    def test_it_starts_empty_rather_than_guessing_today(self, project):
        """Today is when the software ran, not when the reviewer searched."""
        assert not project.info()["searched_to"]

    def test_the_theories_the_reviewer_names_are_recorded(self, project):
        project.update_info(theories="social exchange theory; SOBC")
        assert "social exchange" in project.info()["theories"]

    def test_the_stimulus_attributes_the_reviewer_names_are_recorded(self, project):
        """Section 11 asks which attributes were never varied; it needs the list."""
        project.update_info(stimulus_attributes="source; certainty; framing")
        assert "framing" in project.info()["stimulus_attributes"]

    def test_an_unknown_field_is_still_refused(self, project):
        from slr_studio.core.project import ProjectError

        with pytest.raises(ProjectError):
            project.update_info(favourite_colour="blue")


class TestGapRuns:
    def test_a_run_records_what_it_was_and_what_it_found(self, project):
        run_id = project.db.insert(
            "gap_run",
            {
                "mode": "full",
                "started_at": utc_now(),
                "corpus_searched_to": "2026-03-01",
                "n_gaps": 4,
            },
        )
        row = project.db.query_one("SELECT * FROM gap_run WHERE id = ?", (run_id,))
        assert row["mode"] == "full" and row["n_gaps"] == 4

    def test_only_the_two_modes_the_spec_names(self, project):
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "gap_run", {"mode": "vibes", "started_at": utc_now()}
            )


class TestQuickScanTopics:
    def test_a_topic_holds_the_records_it_clustered(self, project, fixtures):
        from slr_studio.importers.service import ImportService

        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        record_id = project.db.scalar("SELECT id FROM record LIMIT 1")
        topic_id = project.db.insert(
            "topic",
            {
                "label": "community participation",
                "terms": '["participation", "community"]',
                "n_records": 1,
                "created_at": utc_now(),
            },
        )
        project.db.insert(
            "topic_member",
            {"topic_id": topic_id, "record_id": record_id, "similarity": 0.8},
        )
        assert project.db.scalar(
            "SELECT COUNT(*) FROM topic_member WHERE topic_id = ?", (topic_id,)
        ) == 1

    def test_a_record_is_in_one_topic_only(self, project, fixtures):
        from slr_studio.importers.service import ImportService

        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        record_id = project.db.scalar("SELECT id FROM record LIMIT 1")
        topic_id = project.db.insert(
            "topic", {"label": "a", "created_at": utc_now()}
        )
        project.db.insert(
            "topic_member", {"topic_id": topic_id, "record_id": record_id}
        )
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "topic_member", {"topic_id": topic_id, "record_id": record_id}
            )


class TestTheRegisterKeepsItsQualifier:
    def test_a_gap_carries_the_date_the_corpus_was_searched_to(self, project):
        gap_id = project.db.insert(
            "gap_register",
            {
                "gap_type": "evidence",
                "statement": "No dataset measures X with Y.",
                "corpus_searched_to": "2026-03-01",
                "supporting_counts": '{"n_datasets": 0}',
                "created_at": utc_now(),
            },
        )
        assert project.db.scalar(
            "SELECT corpus_searched_to FROM gap_register WHERE id = ?", (gap_id,)
        ) == "2026-03-01"


class TestMigrationToV8:
    def _v7_project(self, path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            BASE_SQL + SCREENING_SQL + FULLTEXT_SQL + EXTRACTION_SQL
            + GAP_SQL + SYNTHESIS_SQL + FRAMEWORK_SQL
        )
        connection.execute(
            "INSERT INTO project(id, title, objectives, created_at, updated_at) "
            "VALUES(1, 'An older review', 'map participation', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','7')")
        connection.commit()
        connection.close()

    def test_a_v7_project_is_brought_forward(self, tmp_path):
        path = tmp_path / "v7.slr"
        self._v7_project(path)
        db = Database(path)
        try:
            assert db.applied_migrations == list(
                range(8, SCHEMA_VERSION + 1)
            )
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            tables = {
                r[0] for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {"gap_run", "topic", "topic_member"} <= tables
        finally:
            db.close()

    def test_the_older_project_keeps_its_title_and_gains_the_new_fields(
        self, tmp_path
    ):
        path = tmp_path / "v7.slr"
        self._v7_project(path)
        db = Database(path)
        try:
            assert db.scalar("SELECT title FROM project") == "An older review"
            assert db.scalar("SELECT searched_to FROM project") == ""
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
            assert db.applied_migrations == list(
                range(2, SCHEMA_VERSION + 1)
            )
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            db.close()
