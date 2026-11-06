"""Importing into a project: storage, counts, provenance and seed checking."""

from __future__ import annotations

import json

import pytest

from slr_studio.importers.service import DuplicateFileError, ImportService


class TestImporting:
    def test_records_are_stored_with_their_file(self, project, fixtures):
        summary = ImportService(project).import_file(fixtures / "scopus_sample.ris")
        assert summary.n_records == 6
        assert summary.source_database == "Scopus"
        stored = project.db.scalar(
            "SELECT COUNT(*) FROM record WHERE import_file_id = ?",
            (summary.import_file_id,),
        )
        assert stored == 6

    def test_missing_abstracts_are_counted(self, project, fixtures):
        summary = ImportService(project).import_file(fixtures / "scopus_sample.ris")
        assert summary.n_missing_abstract == 1

    def test_normalised_and_raw_forms_are_both_kept(self, project, fixtures):
        ImportService(project).import_file(fixtures / "wos_sample.ris")
        row = project.db.query_one(
            "SELECT * FROM record WHERE title LIKE '%Coastal Kerala%'"
        )
        assert row["title_norm"] == (
            "resident participation in sustainable tourism planning evidence from "
            "coastal kerala"
        )
        assert row["raw"].startswith("TY  - JOUR")
        assert row["record_hash"]

    def test_every_record_starts_active_and_unmerged(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        assert project.db.scalar("SELECT COUNT(*) FROM record WHERE active = 1") == 6
        assert project.db.scalar(
            "SELECT COUNT(*) FROM record WHERE duplicate_of IS NOT NULL"
        ) == 0

    def test_each_record_records_its_own_provenance(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        assert project.db.scalar("SELECT COUNT(*) FROM record_source") == 6

    def test_the_file_is_archived_inside_the_project(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        assert (project.imports_dir / "scopus_sample.ris").is_file()

    def test_the_same_file_twice_is_refused(self, project, fixtures):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        with pytest.raises(DuplicateFileError, match="already imported"):
            service.import_file(fixtures / "scopus_sample.ris")

    def test_a_deliberate_reimport_is_allowed(self, project, fixtures):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        service.import_file(fixtures / "scopus_sample.ris", allow_reimport=True)
        assert project.counts()["n_identified"] == 12

    def test_the_source_database_can_be_corrected_at_import(self, project, fixtures):
        service = ImportService(project)
        service.import_file(
            fixtures / "scopus_sample.ris", source_database="Scopus (2024 re-run)"
        )
        databases = {
            row["source_database"] for row in project.db.query(
                "SELECT DISTINCT source_database FROM record"
            )
        }
        assert databases == {"Scopus (2024 re-run)"}

    def test_importing_many_collects_failures_rather_than_stopping(
        self, project, fixtures, tmp_path
    ):
        broken = tmp_path / "broken.doc"
        broken.write_text("this is not an export", encoding="utf-8")
        summaries, failures = ImportService(project).import_many(
            [fixtures / "scopus_sample.ris", broken, fixtures / "wos_sample.ris"]
        )
        assert len(summaries) == 2
        assert len(failures) == 1
        assert failures[0][0] == "broken.doc"

    def test_import_is_written_to_the_audit_log(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        entry = next(
            e for e in project.audit.entries(stage="import")
            if e.action == "file_imported"
        )
        assert entry.detail["n_records"] == 6
        assert entry.detail["file_sha256"]
        assert project.audit.verify() == []

    def test_counts_per_database_after_two_files(self, project, fixtures):
        service = ImportService(project)
        service.import_file(fixtures / "scopus_sample.ris")
        service.import_file(fixtures / "wos_sample.ris")
        counts = {r["source_database"]: r["n_imported"] for r in service.counts_per_database()}
        assert counts == {"Scopus": 6, "Web of Science": 6}

    def test_authors_and_keywords_round_trip_as_json(self, project, fixtures):
        ImportService(project).import_file(fixtures / "scopus_sample.ris")
        row = project.db.query_one(
            "SELECT authors, keywords FROM record WHERE doi = ?",
            ("10.1080/jsts.2021.004512",),
        )
        assert json.loads(row["authors"]) == ["Menon, R.", "Fernandes, L.P."]
        assert "coastal tourism" in json.loads(row["keywords"])


class TestSeedStudies:
    def test_a_seed_found_in_the_import_is_reported(self, project, fixtures):
        service = ImportService(project)
        service.add_seed("10.1080/jsts.2021.004512")
        service.import_file(fixtures / "scopus_sample.ris")
        seeds = service.check_seeds()
        assert seeds[0]["found"] is True
        assert "Kerala" in seeds[0]["title"]

    def test_a_missing_seed_is_reported(self, project, fixtures):
        service = ImportService(project)
        service.add_seed("10.9999/missing.2020")
        service.import_file(fixtures / "scopus_sample.ris")
        assert service.check_seeds()[0]["found"] is False

    def test_seed_dois_are_normalised_before_storing(self, project):
        service = ImportService(project)
        service.add_seed("https://doi.org/10.1080/JSTS.2021.004512")
        assert project.db.scalar("SELECT doi FROM seed_study") == (
            "10.1080/jsts.2021.004512"
        )

    def test_adding_the_same_seed_twice_stores_it_once(self, project):
        service = ImportService(project)
        first = service.add_seed("10.1080/jsts.2021.004512")
        second = service.add_seed("doi:10.1080/jsts.2021.004512")
        assert first == second
        assert project.db.scalar("SELECT COUNT(*) FROM seed_study") == 1

    def test_a_string_without_a_doi_is_rejected(self, project):
        with pytest.raises(ValueError, match="does not contain a DOI"):
            ImportService(project).add_seed("the Menon paper")

    def test_the_seed_check_is_logged(self, project, fixtures):
        service = ImportService(project)
        service.add_seed("10.9999/missing.2020")
        service.import_file(fixtures / "scopus_sample.ris")
        entry = [e for e in project.audit.entries() if e.action == "seed_check"][-1]
        assert entry.detail["n_found"] == 0
        assert entry.detail["missing"] == ["10.9999/missing.2020"]
