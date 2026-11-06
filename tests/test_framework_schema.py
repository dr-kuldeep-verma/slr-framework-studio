"""What the framework builder stores, and that an old project still opens.

The one rule the schema itself has to carry is the spec's: a proposition may
cite only links that exist in the graded inventory. A foreign key does half of
that; the engine does the rest, and both are tested.
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


class TestThemes:
    def test_a_theme_groups_constructs(self, project):
        theme_id = project.db.insert(
            "theme",
            {"name": "Trust and fairness", "position": 1, "created_at": utc_now()},
        )
        construct_id = project.db.insert(
            "construct",
            {"name": "trust", "name_norm": "trust", "definition": "d",
             "position": 1, "created_at": utc_now()},
        )
        project.db.insert(
            "theme_member", {"theme_id": theme_id, "construct_id": construct_id}
        )
        assert project.db.scalar(
            "SELECT COUNT(*) FROM theme_member WHERE theme_id = ?", (theme_id,)
        ) == 1

    def test_a_theme_starts_proposed_not_approved(self, project):
        theme_id = project.db.insert(
            "theme", {"name": "A theme", "created_at": utc_now()}
        )
        assert project.db.scalar(
            "SELECT status FROM theme WHERE id = ?", (theme_id,)
        ) == "proposed"

    def test_a_construct_belongs_to_a_theme_only_once(self, project):
        theme_id = project.db.insert(
            "theme", {"name": "A theme", "created_at": utc_now()}
        )
        construct_id = project.db.insert(
            "construct",
            {"name": "trust", "name_norm": "trust", "definition": "d",
             "created_at": utc_now()},
        )
        project.db.insert(
            "theme_member", {"theme_id": theme_id, "construct_id": construct_id}
        )
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "theme_member", {"theme_id": theme_id, "construct_id": construct_id}
            )


class TestPropositions:
    def _graded_link(self, project) -> int:
        ids = [
            project.db.insert(
                "construct",
                {"name": name, "name_norm": name, "definition": "d",
                 "created_at": utc_now()},
            )
            for name in ("trust", "participation")
        ]
        return project.db.insert(
            "graded_link",
            {
                "link_key": "trust|participation",
                "predictor_construct": ids[0],
                "outcome_construct": ids[1],
                "direction": "positive",
                "grade": "strong",
                "n_datasets": 6,
                "created_at": utc_now(),
            },
        )

    def test_a_proposition_carries_everything_the_spec_asks_for(self, project):
        row_id = project.db.insert(
            "proposition",
            {
                "number": 1,
                "statement": "Trust increases participation.",
                "mechanism": "Trust lowers the perceived cost of speaking up.",
                "theories": '["social exchange theory"]',
                "moderators": "community size",
                "boundary_conditions": "Only where a forum exists.",
                "falsifying_test": "A trust intervention that does not move "
                                   "participation.",
                "created_at": utc_now(),
            },
        )
        row = project.db.query_one(
            "SELECT * FROM proposition WHERE id = ?", (row_id,)
        )
        assert row["mechanism"] and row["falsifying_test"]
        assert row["boundary_conditions"] and row["moderators"]

    def test_a_proposition_cites_a_graded_link(self, project):
        link_id = self._graded_link(project)
        prop_id = project.db.insert(
            "proposition",
            {"number": 1, "statement": "s", "created_at": utc_now()},
        )
        project.db.insert(
            "proposition_link",
            {"proposition_id": prop_id, "graded_link_id": link_id},
        )
        assert project.db.scalar(
            "SELECT n_links FROM v_propositions WHERE id = ?", (prop_id,)
        ) == 1

    def test_a_citation_to_a_link_that_does_not_exist_is_refused(self, project):
        """The database will not hold what the spec says to reject."""
        prop_id = project.db.insert(
            "proposition",
            {"number": 1, "statement": "s", "created_at": utc_now()},
        )
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "proposition_link",
                {"proposition_id": prop_id, "graded_link_id": 9999},
            )

    def test_the_same_link_is_not_cited_twice_by_one_proposition(self, project):
        link_id = self._graded_link(project)
        prop_id = project.db.insert(
            "proposition", {"number": 1, "statement": "s", "created_at": utc_now()}
        )
        project.db.insert(
            "proposition_link",
            {"proposition_id": prop_id, "graded_link_id": link_id},
        )
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "proposition_link",
                {"proposition_id": prop_id, "graded_link_id": link_id},
            )


class TestTheThreeKindsOfElement:
    """"Evidenced links, proposed mechanisms and untested pathways" -- kept
    apart, so a reader of the diagram can never take one for another.
    """

    def test_all_three_kinds_are_allowed(self, project):
        sql = project.db.scalar(
            "SELECT sql FROM sqlite_master WHERE name = 'framework_element'"
        )
        for kind in ("evidenced_link", "proposed_mechanism", "untested_pathway"):
            assert f"'{kind}'" in sql

    def test_a_fourth_kind_is_refused(self, project):
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "framework_element",
                {"kind": "strong_hunch", "source_label": "a",
                 "target_label": "b", "created_at": utc_now()},
            )

    def test_an_evidenced_element_must_name_its_link(self, project):
        """Calling something evidenced without evidence is the one lie the
        figure could tell, so the table refuses it.
        """
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "framework_element",
                {"kind": "evidenced_link", "source_label": "a",
                 "target_label": "b", "created_at": utc_now()},
            )

    def test_a_mechanism_does_not_need_a_link(self, project):
        row_id = project.db.insert(
            "framework_element",
            {"kind": "proposed_mechanism", "source_label": "trust",
             "target_label": "participation", "created_at": utc_now()},
        )
        assert row_id


class TestFigures:
    def test_a_figure_records_what_it_was_drawn_from(self, project):
        row_id = project.db.insert(
            "figure",
            {
                "kind": "evidence_map",
                "filename": "theme-1.svg",
                "format": "svg",
                "n_constructs": 4,
                "n_links": 5,
                "created_at": utc_now(),
            },
        )
        row = project.db.query_one("SELECT * FROM figure WHERE id = ?", (row_id,))
        assert row["n_links"] == 5 and row["kind"] == "evidence_map"

    def test_only_the_two_kinds_of_figure_the_spec_names(self, project):
        with pytest.raises(sqlite3.IntegrityError):
            project.db.insert(
                "figure",
                {"kind": "doodle", "filename": "f.svg", "format": "svg",
                 "created_at": utc_now()},
            )


class TestMigrationToV7:
    def _v6_project(self, path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            BASE_SQL + SCREENING_SQL + FULLTEXT_SQL + EXTRACTION_SQL
            + GAP_SQL + SYNTHESIS_SQL
        )
        connection.execute(
            "INSERT INTO construct(name, name_norm, definition, position, "
            "created_at) VALUES('trust', 'trust', "
            "'what studies measured as trust', 1, '2026-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','6')")
        connection.commit()
        connection.close()

    def test_a_v6_project_is_brought_forward(self, tmp_path):
        path = tmp_path / "v6.slr"
        self._v6_project(path)
        db = Database(path)
        try:
            assert db.applied_migrations == list(
                range(7, SCHEMA_VERSION + 1)
            )
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
            tables = {
                r[0] for r in db.query(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            assert {
                "theme", "theme_member", "proposition", "proposition_link",
                "framework_element", "figure",
            } <= tables
        finally:
            db.close()

    def test_what_the_old_build_wrote_is_still_there(self, tmp_path):
        path = tmp_path / "v6.slr"
        self._v6_project(path)
        db = Database(path)
        try:
            assert db.scalar("SELECT name FROM construct") == "trust"
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


class TestMigrationToV9:
    """Figures gained a PDF, which the figure table's CHECK did not allow.

    SQLite cannot widen a CHECK constraint in place, so the table is rebuilt.
    The rows already in it have to survive that, or a reviewer loses the
    record of the figures they have already drawn.
    """

    def _v8_project(self, path) -> None:
        connection = sqlite3.connect(path)
        from slr_studio.core.schema import GAP_RUN_SQL

        connection.executescript(
            BASE_SQL + SCREENING_SQL + FULLTEXT_SQL + EXTRACTION_SQL
            + GAP_SQL + SYNTHESIS_SQL + FRAMEWORK_SQL + GAP_RUN_SQL
        )
        connection.execute(
            "INSERT INTO figure(kind, filename, format, n_constructs, n_links, "
            "created_at) VALUES('evidence_map', 'theme-1.svg', 'svg', 4, 5, "
            "'2026-01-01T00:00:00Z')"
        )
        connection.execute("INSERT INTO meta(key, value) VALUES('schema_version','8')")
        connection.commit()
        connection.close()

    def test_a_v8_project_is_brought_forward(self, tmp_path):
        path = tmp_path / "v8.slr"
        self._v8_project(path)
        db = Database(path)
        try:
            assert db.get_meta("schema_version") == str(SCHEMA_VERSION)
        finally:
            db.close()

    def test_the_figures_already_recorded_survive_the_rebuild(self, tmp_path):
        path = tmp_path / "v8.slr"
        self._v8_project(path)
        db = Database(path)
        try:
            assert db.scalar("SELECT filename FROM figure") == "theme-1.svg"
            assert db.scalar("SELECT COUNT(*) FROM figure") == 1
        finally:
            db.close()

    def test_a_pdf_figure_is_now_allowed(self, tmp_path):
        path = tmp_path / "v8.slr"
        self._v8_project(path)
        db = Database(path)
        try:
            db.insert(
                "figure",
                {"kind": "evidence_map", "filename": "theme-1.pdf",
                 "format": "pdf", "created_at": utc_now()},
            )
            assert db.scalar(
                "SELECT COUNT(*) FROM figure WHERE format = 'pdf'"
            ) == 1
        finally:
            db.close()

    def test_an_invented_format_is_still_refused(self, tmp_path):
        path = tmp_path / "v8.slr"
        self._v8_project(path)
        db = Database(path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                db.insert(
                    "figure",
                    {"kind": "evidence_map", "filename": "x.bmp",
                     "format": "bmp", "created_at": utc_now()},
                )
        finally:
            db.close()
