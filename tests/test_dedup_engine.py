"""Deduplication against a real project, including re-runs and human overrides.

The fixture pair of files is built so that every rule fires once:

* Menon 2021 is in both files with the same DOI          -> doi_exact
* Okonkwo 2019 is in both, DOI only in Scopus            -> title_year
* Larsen 2022 differs by one hyphen, same first author   -> fuzzy_title, merged
* Nakamura 2020 has the same title but conflicting DOIs  -> uncertain
* Gurung appears as 2018 and as 2024                     -> auto_distinct
"""

from __future__ import annotations

import pytest

from slr_studio.dedup import DedupEngine, DedupSettings
from slr_studio.importers.service import ImportService


@pytest.fixture
def engine(imported_project):
    return DedupEngine(imported_project)


def _insert_records(project, titles_and_years, doi="10.1234/shared.2020"):
    """Put minimal records straight into the database for edge-case scenarios."""
    from slr_studio.core.db import utc_now
    from slr_studio.importers.normalise import normalise_title, title_key

    now = utc_now()
    file_id = project.db.insert(
        "import_file",
        {
            "filename": "synthetic.ris", "original_path": "synthetic.ris",
            "file_sha256": "0" * 64, "format": "ris", "dialect": "generic",
            "source_database": "Synthetic", "n_records": len(titles_and_years),
            "imported_at": now,
        },
    )
    for position, (title, year) in enumerate(titles_and_years, start=1):
        project.db.insert(
            "record",
            {
                "import_file_id": file_id, "position_in_file": position,
                "title": title, "year": year, "doi": doi,
                "title_norm": normalise_title(title), "title_key": title_key(title),
                "source_database": "Synthetic", "raw": f"TY  - JOUR\nTI  - {title}",
                "raw_format": "ris", "record_hash": f"hash{position}",
                "created_at": now,
            },
        )
    return file_id


class TestRunning:
    def test_the_expected_number_of_duplicates_is_found(self, engine):
        result = engine.run()
        assert result.n_records == 12
        assert result.n_merged == 3
        assert result.n_remaining == 9

    def test_each_rule_contributes(self, engine):
        result = engine.run()
        assert result.by_rule["doi_exact"] >= 1
        assert result.by_rule["title_year"] >= 1
        assert result.by_rule["fuzzy_title"] >= 1

    def test_conflicting_dois_are_left_for_a_human(self, engine):
        result = engine.run()
        assert result.n_uncertain == 1
        pending = engine.pending_pairs()
        assert "conflicting DOIs" in pending[0]["evidence"]["reason"]

    def test_the_same_title_six_years_apart_is_not_merged(self, engine, imported_project):
        engine.run()
        rows = imported_project.db.query(
            "SELECT year, active FROM record WHERE title LIKE '%homestay%' ORDER BY year"
        )
        assert [(r["year"], r["active"]) for r in rows] == [(2018, 1), (2024, 1)]

    def test_prisma_counts_follow_from_the_merges(self, engine, imported_project):
        engine.run()
        counts = imported_project.counts()
        assert counts["n_identified"] == 12
        assert counts["n_duplicates_removed"] == 3
        assert counts["n_screened"] == 9

    def test_the_run_is_written_to_the_audit_log(self, engine, imported_project):
        engine.run()
        entry = [e for e in imported_project.audit.entries(stage="dedup")][-1]
        assert entry.action == "dedup_run"
        assert entry.detail["n_merged"] == 3
        assert imported_project.audit.verify() == []


class TestWhatSurvivesAMerge:
    def test_the_richer_record_is_kept(self, engine, imported_project):
        engine.run()
        row = imported_project.db.query_one(
            "SELECT source_database, active FROM record "
            "WHERE doi = ? AND active = 1", ("10.1080/jsts.2021.004512",)
        )
        # Scopus and Web of Science both carry this one; Scopus wins on the
        # fuller abstract and keyword list.
        assert row["source_database"] == "Scopus"

    def test_the_merged_record_points_at_the_one_that_was_kept(
        self, engine, imported_project
    ):
        engine.run()
        merged = imported_project.db.query_one(
            "SELECT duplicate_of, cluster_id FROM record "
            "WHERE active = 0 AND source_database = 'Web of Science' LIMIT 1"
        )
        assert merged["duplicate_of"] is not None
        assert merged["cluster_id"] is not None

    def test_both_databases_are_remembered_on_the_surviving_record(
        self, engine, imported_project
    ):
        engine.run()
        kept = imported_project.db.scalar(
            "SELECT id FROM record WHERE doi = ? AND active = 1",
            ("10.1080/jsts.2021.004512",),
        )
        databases = {
            r["source_database"] for r in imported_project.db.query(
                "SELECT source_database FROM record_source WHERE record_id = ?", (kept,)
            )
        }
        assert databases == {"Scopus", "Web of Science"}

    def test_every_merge_is_logged_with_its_rule(self, engine):
        engine.run()
        merges = engine.merged_groups()
        assert len(merges) == 3
        assert {m["rule"] for m in merges} <= {
            "doi_exact", "title_year", "fuzzy_title", "transitive",
        }
        assert all(m["merged_at"] for m in merges)

    def test_per_database_counts_show_the_loss(self, imported_project, engine):
        engine.run()
        counts = {
            r["source_database"]: (r["n_imported"], r["n_after_dedup"])
            for r in imported_project.counts_per_database()
        }
        assert counts["Scopus"] == (6, 6)
        assert counts["Web of Science"] == (6, 3)


class TestReRunning:
    def test_running_twice_gives_the_same_answer(self, engine):
        first = engine.run()
        second = engine.run()
        assert (first.n_merged, first.n_remaining) == (second.n_merged, second.n_remaining)

    def test_raising_the_threshold_finds_fewer_duplicates(self, imported_project):
        DedupEngine(imported_project).run()
        strict = DedupEngine(
            imported_project, DedupSettings(fuzzy_threshold=0.999, auto_threshold=0.999)
        ).run()
        # The hyphenation pair is no longer close enough to propose.
        assert strict.n_merged == 2

    def test_a_stale_automatic_pair_is_dropped_when_settings_tighten(
        self, imported_project
    ):
        DedupEngine(imported_project).run()
        before = imported_project.db.scalar("SELECT COUNT(*) FROM dedup_pair")
        DedupEngine(
            imported_project, DedupSettings(fuzzy_threshold=0.999, auto_threshold=0.999)
        ).run()
        after = imported_project.db.scalar("SELECT COUNT(*) FROM dedup_pair")
        assert after < before

    def test_importing_more_files_then_rerunning_widens_the_net(
        self, imported_project, fixtures
    ):
        DedupEngine(imported_project).run()
        ImportService(imported_project).import_file(fixtures / "proquest_sample.ris")
        result = DedupEngine(imported_project).run()
        # The ProQuest copy of the Haddad paper shares its DOI with the
        # Web of Science copy.
        assert result.n_records == 14
        assert result.n_merged == 4


class TestHumanDecisions:
    def test_a_human_merge_is_applied(self, engine, imported_project):
        engine.run()
        pair = engine.pending_pairs()[0]
        engine.decide_pair(pair["id"], True, actor="human:tester", reason="same study")
        assert imported_project.counts()["n_duplicates_removed"] == 4

    def test_a_human_rejection_keeps_both_records(self, engine, imported_project):
        engine.run()
        pair = engine.pending_pairs()[0]
        engine.decide_pair(
            pair["id"], False, actor="human:tester",
            reason="conference paper and journal article",
        )
        assert imported_project.counts()["n_duplicates_removed"] == 3
        assert engine.pending_pairs() == []

    def test_the_decision_survives_a_rerun(self, engine, imported_project):
        engine.run()
        pair_id = engine.pending_pairs()[0]["id"]
        engine.decide_pair(pair_id, False, actor="human:tester")
        engine.run()
        status = imported_project.db.scalar(
            "SELECT status FROM dedup_pair WHERE id = ?", (pair_id,)
        )
        assert status == "human_distinct"
        assert engine.pending_pairs() == []

    def test_the_override_is_logged_against_the_pair(self, engine, imported_project):
        engine.run()
        pair_id = engine.pending_pairs()[0]["id"]
        engine.decide_pair(pair_id, False, actor="human:tester", reason="different samples")
        row = imported_project.db.query_one(
            "SELECT * FROM human_override WHERE entity_id = ?", (str(pair_id),)
        )
        assert row["stage"] == "dedup"
        assert row["reason"] == "different samples"
        assert imported_project.audit.verify() == []

    def test_deciding_a_pair_that_does_not_exist_is_an_error(self, engine):
        engine.run()
        with pytest.raises(ValueError, match="No duplicate pair"):
            engine.decide_pair(9999, True)

    def test_a_reviewer_sees_both_records_side_by_side(self, engine):
        engine.run()
        pair = engine.pending_pairs()[0]
        for side in ("a", "b"):
            assert pair[side]["title"]
            assert pair[side]["source_database"]
            assert isinstance(pair[side]["authors"], list)


class TestEmptyAndEdgeCases:
    def test_running_on_an_empty_project_is_harmless(self, project):
        result = DedupEngine(project).run()
        assert (result.n_records, result.n_pairs, result.n_merged) == (0, 0, 0)

    def test_a_project_with_no_duplicates_merges_nothing(self, project, fixtures):
        ImportService(project).import_file(fixtures / "pubmed_sample.nbib")
        result = DedupEngine(project).run()
        assert result.n_merged == 0
        assert result.n_remaining == 2

    def test_a_contradiction_between_a_human_and_transitivity_is_surfaced(
        self, project
    ):
        # Three records share one DOI, so every pair matches. A reviewer then
        # says two of them are different studies. Transitivity still pulls all
        # three together through the third record; the run must say so rather
        # than quietly picking a side.
        _insert_records(project, [("A study of trust", 2020), ("A study of trust", 2020),
                                  ("A study of trust", 2020)])
        engine = DedupEngine(project)
        engine.run()
        pair_id = project.db.scalar(
            "SELECT id FROM dedup_pair WHERE record_a_id = 1 AND record_b_id = 2"
        )
        project.db.execute(
            "UPDATE dedup_pair SET status = 'human_distinct', decided_by = 'human:tester' "
            "WHERE id = ?",
            (pair_id,),
        )
        result = engine.run()
        assert result.warnings
        assert "marked distinct" in result.warnings[0]
