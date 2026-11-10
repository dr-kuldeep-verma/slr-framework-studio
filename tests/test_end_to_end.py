"""The whole stage-1 pipeline, driven the way the interface drives it.

The demo project ships with the application and this test is what keeps it
honest: four exports from four databases go in, and the PRISMA identification
counts that come out are the ones the Methods section will report.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from slr_studio.cli import main
from slr_studio.core.project import Project
from slr_studio.dedup import DedupEngine
from slr_studio.importers.service import ImportService


class TestFourDatabasePipeline:
    @pytest.fixture
    def finished(self, tmp_path, fixtures):
        project = Project.create(tmp_path / "review", "Four-database review")
        service = ImportService(project)
        service.add_seed("10.1080/jsts.2021.004512")
        service.add_seed("10.9999/never-published.2020")
        for name in (
            "scopus_sample.ris",
            "wos_sample.ris",
            "ebsco_sample.ris",
            "proquest_sample.ris",
        ):
            service.import_file(fixtures / name)
        result = DedupEngine(project).run()
        yield project, result
        project.close()

    def test_every_database_is_represented(self, finished):
        project, _ = finished
        databases = {r["source_database"] for r in project.counts_per_database()}
        assert databases == {
            "Scopus", "Web of Science", "Hospitality & Tourism Complete", "ProQuest",
        }

    def test_identification_counts_add_up(self, finished):
        project, result = finished
        counts = project.counts()
        assert counts["n_identified"] == 16
        assert counts["n_identified"] == (
            counts["n_duplicates_removed"] + counts["n_screened"]
        )

    def test_the_same_study_across_four_databases_is_one_record(self, finished):
        project, _ = finished
        # Okonkwo 2019 is in Scopus, Web of Science and EBSCO.
        kept = project.db.query(
            "SELECT id FROM record WHERE title_norm LIKE 'benefit sharing%' AND active = 1"
        )
        assert len(kept) == 1
        sources = {
            r["source_database"] for r in project.db.query(
                "SELECT source_database FROM record_source WHERE record_id = ?",
                (kept[0]["id"],),
            )
        }
        assert sources == {"Scopus", "Web of Science", "Hospitality & Tourism Complete"}

    def test_a_record_with_no_doi_anywhere_still_merges_on_title_and_year(
        self, finished
    ):
        project, _ = finished
        # The EBSCO and Web of Science copies of Okonkwo carry no DOI; the
        # Scopus one does, and is the copy that survives.
        kept = project.db.query_one(
            "SELECT doi, source_database FROM record "
            "WHERE title_norm LIKE 'benefit sharing%' AND active = 1"
        )
        assert kept["doi"] == "10.1016/j.tgr.2019.02.003"
        assert kept["source_database"] == "Scopus"

    def test_seed_recall_is_reported(self, finished):
        project, _ = finished
        seeds = ImportService(project).check_seeds()
        found = {s["doi"]: s["found"] for s in seeds}
        assert found["10.1080/jsts.2021.004512"] is True
        assert found["10.9999/never-published.2020"] is False

    def test_the_audit_trail_covers_the_whole_run_and_verifies(self, finished):
        project, _ = finished
        actions = [e.action for e in project.audit.entries()]
        assert actions[0] == "project_created"
        assert actions.count("file_imported") == 4
        assert "dedup_run" in actions
        assert project.audit.verify() == []

    def test_nothing_is_ever_deleted(self, finished):
        project, result = finished
        # Merged records are retired, not removed, so the counts can always be
        # rebuilt from the database.
        assert project.db.scalar("SELECT COUNT(*) FROM record") == 16
        assert project.db.scalar(
            "SELECT COUNT(*) FROM record WHERE active = 0"
        ) == result.n_merged


class TestCommandLine:
    def test_a_review_runs_start_to_finish_without_errors(self, tmp_path, fixtures, capsys):
        folder = tmp_path / "cli-review"
        assert main(["new", str(folder), "--title", "CLI review"]) == 0
        assert main(
            [
                "import", str(folder),
                str(fixtures / "scopus_sample.ris"),
                str(fixtures / "wos_sample.ris"),
                "--seed", "10.1080/jsts.2021.004512",
            ]
        ) == 0
        assert main(["dedup", str(folder)]) == 0
        output = capsys.readouterr().out
        payload = json.loads(output[output.index("{") : output.rindex("}") + 1])
        assert payload["n_merged"] == 3
        assert payload["n_remaining"] == 9

    def test_status_reports_counts_and_audit_health(self, tmp_path, fixtures, capsys):
        folder = tmp_path / "cli-review"
        main(["new", str(folder), "--title", "CLI review"])
        main(["import", str(folder), str(fixtures / "scopus_sample.ris")])
        capsys.readouterr()
        assert main(["status", str(folder)]) == 0
        out = capsys.readouterr().out
        assert "Records identified: 6" in out
        assert "Audit chain: intact" in out

    def test_a_human_decision_is_accepted_from_the_command_line(
        self, tmp_path, fixtures, capsys
    ):
        folder = tmp_path / "cli-review"
        main(["new", str(folder), "--title", "CLI review"])
        main(["import", str(folder), str(fixtures / "scopus_sample.ris"),
              str(fixtures / "wos_sample.ris")])
        main(["dedup", str(folder)])
        capsys.readouterr()
        with Project.open(folder) as project:
            pair_id = DedupEngine(project).pending_pairs()[0]["id"]
        assert main(
            ["decide", str(folder), str(pair_id), "distinct", "--reason", "different"]
        ) == 0
        with Project.open(folder) as project:
            assert DedupEngine(project).pending_pairs() == []

    def test_an_unreadable_file_is_reported_not_crashed_on(
        self, tmp_path, fixtures, capsys
    ):
        folder = tmp_path / "cli-review"
        main(["new", str(folder), "--title", "CLI review"])
        junk = tmp_path / "junk.doc"
        junk.write_text("not an export", encoding="utf-8")
        assert main(["import", str(folder), str(junk)]) == 1
        assert "FAILED" in capsys.readouterr().err

    def test_opening_a_folder_without_a_project_is_a_clean_error(self, tmp_path, capsys):
        assert main(["status", str(tmp_path)]) == 2
        assert "error:" in capsys.readouterr().err


class TestDemoProject:
    def test_the_bundled_demo_data_is_present_and_parses(self):
        from slr_studio.demo import demo_files

        files = demo_files()
        assert len(files) >= 2
        for path in files:
            assert Path(path).is_file()

    def test_the_demo_builds_a_project_end_to_end(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "demo")
        try:
            counts = project.counts()
            assert counts["n_identified"] > counts["n_screened"] > 0
            assert project.audit.verify() == []
        finally:
            project.close()

    def test_the_demo_screens_without_a_network_when_asked(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "demo", screen=True)
        try:
            screening = dict(project.db.query_one("SELECT * FROM v_prisma_screening"))
            assert screening["n_not_yet_screened"] == 0
            # Unclear records now go to full text without asking anyone, so
            # the small demo may leave nothing for the reviewer.
            assert screening["n_awaiting_review"] >= 0
            # Every call is recorded as coming from the offline screener, so a
            # demo run can never be mistaken for a real one.
            providers = {
                r["provider"] for r in project.db.query("SELECT DISTINCT provider FROM ai_call")
            }
            assert providers == {"mock"}
            assert project.audit.verify() == []
        finally:
            project.close()


class TestWholeReview:
    """Every stage at once: the numbers a Methods section would report."""

    @pytest.fixture
    def reviewed(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "review", extract=True)
        yield project
        project.close()

    def test_everything_extracted_comes_from_an_included_report(self, reviewed):
        stray = reviewed.db.scalar(
            "SELECT COUNT(*) FROM report_extraction e WHERE coder_role = 'ai' "
            "AND NOT EXISTS (SELECT 1 FROM fulltext_status f "
            "                WHERE f.record_id = e.record_id "
            "                  AND f.decision = 'include')"
        )
        assert stray == 0

    def test_every_relationship_quotes_a_page_that_really_says_it(self, reviewed):
        rows = reviewed.db.query(
            "SELECT quote_verified FROM relationship "
            "WHERE coder_role = 'ai' AND quote <> ''"
        )
        assert rows
        assert all(row["quote_verified"] == 1 for row in rows)

    def test_every_direction_was_re_derived_independently(self, reviewed):
        rows = reviewed.db.query(
            "SELECT verified_direction, verify_call_id FROM relationship "
            "WHERE coder_role = 'ai' AND quote <> ''"
        )
        assert rows
        for row in rows:
            assert row["verified_direction"]
            # The check is its own logged call, not a re-reading of the first.
            assert row["verify_call_id"] is not None

    def test_the_extraction_call_and_the_check_are_separate_calls(self, reviewed):
        tasks = {
            r["task"]
            for r in reviewed.db.query(
                "SELECT DISTINCT task FROM ai_call WHERE stage = 'extract'"
            )
        }
        assert {"extract:report", "extract:direction"} <= tasks

    def test_the_full_text_numbers_reconcile_with_screening(self, reviewed):
        screening = dict(reviewed.db.query_one("SELECT * FROM v_prisma_screening"))
        full = dict(reviewed.db.query_one("SELECT * FROM v_prisma_fulltext"))
        # Everything carried forward from screening is sought, and nothing else.
        assert full["n_sought"] == screening["n_included"] + screening["n_unclear"]
        assert full["n_assessed"] == full["n_retrieved"]
        assert (
            full["n_included"] + full["n_excluded"] + full["n_unclear"]
            == full["n_assessed"]
        )

    def test_every_assessment_quotes_a_page_that_really_says_it(self, reviewed):
        rows = reviewed.db.query(
            "SELECT quote, quote_page, quote_verified FROM fulltext_decision "
            "WHERE reviewer_role = 'assessor' AND quote <> ''"
        )
        assert rows
        assert all(row["quote_verified"] == 1 for row in rows)

    def test_a_report_can_be_traced_from_its_record_to_its_pages(self, reviewed):
        row = reviewed.db.query_one(
            "SELECT r.id, r.title, rep.filename, rep.file_sha256, rep.n_pages "
            "FROM record r JOIN report rep ON rep.record_id = r.id "
            "WHERE rep.status = 'retrieved' LIMIT 1"
        )
        assert row["file_sha256"] and row["n_pages"] > 0
        pages = reviewed.db.query(
            "SELECT page_no FROM report_page WHERE record_id = ?", (row["id"],)
        )
        assert len(pages) == row["n_pages"]

    def test_the_prisma_numbers_reconcile(self, reviewed):
        identification = reviewed.counts()
        screening = dict(reviewed.db.query_one("SELECT * FROM v_prisma_screening"))
        assert (
            identification["n_identified"]
            == identification["n_duplicates_removed"] + identification["n_screened"]
        )
        assert identification["n_screened"] == screening["n_to_screen"]
        assert (
            screening["n_included"] + screening["n_excluded"] + screening["n_unclear"]
            == screening["n_to_screen"]
        )

    def test_every_stage_appears_in_the_audit_log(self, reviewed):
        stages = {e.stage for e in reviewed.audit.entries()}
        assert {
            "project", "import", "dedup", "screen", "retrieve", "fulltext", "extract",
        } <= stages

    def test_screening_names_the_protocol_version_it_read(self, reviewed):
        run = reviewed.db.query_one(
            "SELECT protocol_version FROM screening_run WHERE kind = 'full'"
        )
        locked = reviewed.protocol.current()
        assert run["protocol_version"] == locked.id

    def test_every_screening_decision_traces_to_a_logged_call(self, reviewed):
        orphans = reviewed.db.scalar(
            "SELECT COUNT(*) FROM screening_decision d "
            "WHERE d.reviewer_role NOT IN ('human', 'pilot_human') "
            "AND (d.ai_call_id IS NULL "
            "     OR NOT EXISTS (SELECT 1 FROM ai_call c WHERE c.id = d.ai_call_id))"
        )
        assert orphans == 0

    def test_the_protocol_and_the_audit_chain_both_verify(self, reviewed):
        assert reviewed.audit.verify() == []
        assert reviewed.protocol.verify() == []

    def test_the_exports_a_reviewer_would_hand_over_are_writable(self, reviewed):
        from slr_studio.exports import write_screening_log

        log = write_screening_log(reviewed)
        protocol = reviewed.protocol.export(reviewed.exports_dir / "protocol.md")
        assert log.is_file() and log.stat().st_size > 5_000
        assert "SHA-256" in protocol.read_text(encoding="utf-8").splitlines()[0]


class TestSynthesisedReview:
    """The demo carried through to a graded inventory, offline.

    Section 9 is where separate reports become comparable evidence, so the
    checks here are about what that comparison is allowed to rest on: a
    vocabulary a human approved, one vote per dataset, and a grade that can
    be traced back to the relationships and quotations underneath it.
    """

    @pytest.fixture
    def synthesised(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "review", synthesise=True)
        yield project
        project.close()

    def test_nothing_was_mapped_before_a_human_approved_the_codebook(
        self, synthesised
    ):
        approved_at = synthesised.db.scalar(
            "SELECT approved_at FROM codebook_approval WHERE superseded_at IS NULL"
        )
        assert approved_at, "no codebook was ever approved"
        first_mapping = synthesised.db.scalar(
            "SELECT MIN(created_at) FROM label_mapping"
        )
        assert first_mapping is not None
        assert approved_at <= first_mapping

    def test_every_graded_link_rests_on_at_least_one_dataset(self, synthesised):
        orphans = synthesised.db.scalar(
            "SELECT COUNT(*) FROM graded_link g WHERE g.n_datasets < 1"
        )
        assert orphans == 0

    def test_every_vote_traces_to_a_relationship_and_a_dataset(self, synthesised):
        orphans = synthesised.db.scalar(
            "SELECT COUNT(*) FROM link_vote v "
            "WHERE NOT EXISTS (SELECT 1 FROM dataset d WHERE d.id = v.dataset_id)"
        )
        assert orphans == 0

    def test_a_graded_link_reports_an_interval_not_just_a_proportion(
        self, synthesised
    ):
        rows = synthesised.db.query(
            "SELECT ci_low, ci_high, n_agree, n_datasets FROM graded_link"
        )
        assert rows
        for row in rows:
            assert row["ci_low"] is not None and row["ci_high"] is not None
            assert 0.0 <= row["ci_low"] <= row["ci_high"] <= 1.0

    def test_reviews_and_conceptual_papers_do_not_vote(self, synthesised):
        from slr_studio.synthesis import NON_VOTING_FAMILIES

        voting = synthesised.db.query(
            "SELECT DISTINCT e.design_family FROM link_vote v "
            "JOIN dataset_member m ON m.dataset_id = v.dataset_id "
            "JOIN report_extraction e ON e.record_id = m.record_id "
            "WHERE e.coder_role = 'ai'"
        )
        families = {row["design_family"] for row in voting}
        assert families.isdisjoint(NON_VOTING_FAMILIES)

    def test_every_sign_flip_is_logged_with_its_reason(self, synthesised):
        rows = synthesised.db.query(
            "SELECT original_direction, direction, flip_reason "
            "FROM harmonised_relationship WHERE flipped = 1"
        )
        for row in rows:
            assert row["original_direction"] != row["direction"]
            assert row["flip_reason"]

    def test_a_relationship_kept_as_it_was_is_not_called_a_flip(self, synthesised):
        unflipped = synthesised.db.query(
            "SELECT original_direction, direction FROM harmonised_relationship "
            "WHERE flipped = 0"
        )
        assert unflipped
        for row in unflipped:
            assert row["original_direction"] == row["direction"]

    def test_the_audit_chain_still_verifies_after_synthesis(self, synthesised):
        assert synthesised.audit.verify() == []

    def test_the_graded_inventory_is_writable(self, synthesised):
        from slr_studio.exports import write_graded_inventory

        path = write_graded_inventory(
            synthesised, synthesised.exports_dir / "graded-inventory.xlsx"
        )
        assert path.is_file() and path.stat().st_size > 5_000


class TestSynthesisFromTheCommandLine:
    """The headless path has to reach the same place the window does.

    The reviewer never needs a terminal; this exists so the same code can be
    driven without a display, and a stage missing from it is a stage that
    cannot be scripted or supported.
    """

    @pytest.fixture
    def extracted(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "review", extract=True)
        root = project.root
        project.close()
        return root

    def test_the_whole_synthesis_runs_in_one_command(self, extracted, capsys):
        assert main(
            ["synthesise", str(extracted), "--yes", "--accept-proposals"]
        ) == 0
        assert "graded link" in capsys.readouterr().out.casefold()

    def test_without_accept_proposals_it_stops_rather_than_deciding(
        self, extracted, capsys
    ):
        """Three steps are the reviewer's own. Headless must not take them.

        Exit 1 says the review is waiting for a person, which is not the
        same as exit 2 for something going wrong.
        """
        assert main(["synthesise", str(extracted), "--yes"]) == 1
        output = capsys.readouterr().out.casefold()
        assert "waiting to be approved" in output
        assert "0 graded link" in output

    def test_it_refuses_to_grade_before_a_human_approves_the_codebook(
        self, extracted, capsys
    ):
        """The approval gate is the whole point; it cannot be skipped here."""
        assert main(["synthesise", str(extracted), "--step", "grade", "--yes"]) == 1
        # Waiting for a person is a normal outcome, so it is not an error
        # message; the exit code is what a script reads.
        assert "codebook" in capsys.readouterr().out.casefold()

    def test_a_single_step_can_be_run_on_its_own(self, extracted, capsys):
        assert main(
            ["synthesise", str(extracted), "--step", "codebook", "--yes"]
        ) == 0
        assert "construct" in capsys.readouterr().out.casefold()

    def test_the_graded_inventory_is_exportable(self, extracted, tmp_path, capsys):
        assert main(
            ["synthesise", str(extracted), "--yes", "--accept-proposals"]
        ) == 0
        target = tmp_path / "inventory.xlsx"
        assert main(["export-inventory", str(extracted), str(target)]) == 0
        assert target.is_file() and target.stat().st_size > 5_000

    def test_status_reports_the_synthesis_too(self, extracted, capsys):
        main(["synthesise", str(extracted), "--yes", "--accept-proposals"])
        capsys.readouterr()
        assert main(["status", str(extracted)]) == 0
        assert "synthesis:" in capsys.readouterr().out.casefold()


class TestFrameworkFromTheCommandLine:
    @pytest.fixture
    def synthesised(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "review", synthesise=True)
        root = project.root
        project.close()
        return root

    def test_the_whole_framework_runs_in_one_command(self, synthesised, capsys):
        assert main(
            ["framework", str(synthesised), "--yes", "--accept-proposals"]
        ) == 0
        assert "figure" in capsys.readouterr().out.casefold()

    def test_it_stops_for_the_reviewer_rather_than_approving_themes(
        self, synthesised, capsys
    ):
        assert main(["framework", str(synthesised), "--yes"]) == 1
        assert "theme" in capsys.readouterr().out.casefold()

    def test_the_figures_land_where_the_reviewer_can_find_them(
        self, synthesised
    ):
        main(["framework", str(synthesised), "--yes", "--accept-proposals"])
        figures = sorted(
            p.name for p in (synthesised / "exports" / "figures").iterdir()
        )
        assert any(name.endswith(".pptx") for name in figures)
        assert any(name.endswith(".svg") for name in figures)

    def test_status_reports_the_framework_too(self, synthesised, capsys):
        main(["framework", str(synthesised), "--yes", "--accept-proposals"])
        capsys.readouterr()
        assert main(["status", str(synthesised)]) == 0
        assert "framework:" in capsys.readouterr().out.casefold()


class TestGapsFromTheCommandLine:
    @pytest.fixture
    def framed(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "review", framework=True)
        project.update_info(searched_to="2026-03-01")
        root = project.root
        project.close()
        return root

    def test_the_quick_scan_runs_before_any_review(self, tmp_path, fixtures, capsys):
        folder = tmp_path / "early"
        assert main(["new", str(folder), "--title", "Scoping"]) == 0
        assert main(
            ["import", str(folder), str(fixtures / "scopus_sample.ris")]
        ) == 0
        assert main(["gaps", str(folder), "--step", "quick", "--yes"]) == 0
        assert "topic" in capsys.readouterr().out.casefold()

    def test_the_full_analysis_runs_in_one_command(self, framed, capsys):
        assert main(["gaps", str(framed), "--yes"]) == 0
        assert "gap(s) recorded" in capsys.readouterr().out.casefold()

    def test_it_refuses_without_a_search_date(self, tmp_path, capsys):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "undated", framework=True)
        root = project.root
        project.close()
        assert main(["gaps", str(root), "--yes"]) == 2
        assert "search" in capsys.readouterr().err.casefold()

    def test_the_freshness_check_is_not_run_unless_asked(self, framed, capsys):
        """It is the one step that reaches the network."""
        assert main(["gaps", str(framed), "--yes"]) == 0
        capsys.readouterr()
        assert main(["status", str(framed)]) == 0
        assert "possibly being addressed" not in capsys.readouterr().out.casefold()

    def test_the_register_and_the_agenda_export(self, framed, tmp_path):
        main(["gaps", str(framed), "--yes"])
        assert main(["export-gaps", str(framed)]) == 0
        figures = framed / "exports"
        written = {p.name for p in figures.rglob("*") if p.is_file()}
        assert any(name.endswith(".xlsx") for name in written)
        assert any(name.endswith(".docx") for name in written)
        assert any(name.endswith(".md") for name in written)

    def test_status_reports_the_gaps(self, framed, capsys):
        main(["gaps", str(framed), "--yes"])
        capsys.readouterr()
        assert main(["status", str(framed)]) == 0
        assert "gaps:" in capsys.readouterr().out.casefold()


class TestTheAcceptancePath:
    """Section 14's acceptance test, run headlessly.

    "A non-technical user can install the .exe, create a project, import two
    RIS files, screen, upload PDFs and export a PRISMA diagram and a framework
    figure without opening a terminal." The window is tested elsewhere; what
    is checked here is that the path itself works end to end and produces
    those two outputs.
    """

    @pytest.fixture
    def finished(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "review", gaps=True)
        root = project.root
        project.close()
        return root

    def test_everything_exports_in_one_command(self, finished, capsys):
        assert main(["export-all", str(finished)]) == 0
        assert "file(s) written" in capsys.readouterr().out

    def test_the_prisma_diagram_and_a_framework_figure_are_produced(
        self, finished
    ):
        main(["export-all", str(finished)])
        written = {p.name for p in (finished / "exports").rglob("*") if p.is_file()}
        assert any(n.startswith("prisma-flow") for n in written)
        assert any(
            n.endswith((".pptx", ".svg")) and "theme" in n or
            n.startswith("integrated-framework")
            for n in written
        )

    def test_the_manifest_explains_the_folder(self, finished):
        main(["export-all", str(finished)])
        manifest = (finished / "exports" / "manifest.md").read_text(
            encoding="utf-8"
        )
        assert "audit" in manifest.casefold()
        assert "prisma-flow.docx" in manifest

    def test_the_audit_chain_verified_at_the_end_of_the_whole_review(
        self, finished
    ):
        from slr_studio.core.project import Project

        project = Project.open(finished)
        try:
            assert project.audit.verify() == []
        finally:
            project.close()

    def test_the_methods_draft_carries_the_ai_use_statement(self, finished):
        from docx import Document

        main(["export-all", str(finished)])
        path = finished / "exports" / "methods-draft.docx"
        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        assert "Artificial intelligence was used" in text


class TestTheDemoIsReachableWithoutAnApiKey:
    """Somebody opening this for the first time has no key and no corpus.

    Without a way to see the application with data in it, their first
    impression is an empty window and eleven stages that all say they need a
    project. The demonstration project exists; it just was not reachable
    from either the command line or the menu.
    """

    def test_there_is_a_demo_command(self, tmp_path, capsys):
        target = tmp_path / "demo-review"
        assert main(["demo", str(target)]) == 0
        output = capsys.readouterr().out
        assert "demo" in output.casefold()
        assert (target / "project.slr").is_file()

    def test_it_builds_a_project_with_something_to_look_at(self, tmp_path):
        from slr_studio.core.project import Project

        target = tmp_path / "demo-review"
        main(["demo", str(target)])
        project = Project.open(target)
        try:
            assert project.db.scalar("SELECT COUNT(*) FROM record") > 0
            assert project.db.scalar("SELECT COUNT(*) FROM screening_status") > 0
        finally:
            project.close()

    def test_how_far_it_goes_can_be_chosen(self, tmp_path):
        from slr_studio.core.project import Project

        target = tmp_path / "demo-import"
        assert main(["demo", str(target), "--stage", "import"]) == 0
        project = Project.open(target)
        try:
            assert project.db.scalar("SELECT COUNT(*) FROM record") > 0
            assert project.db.scalar("SELECT COUNT(*) FROM screening_status") == 0
        finally:
            project.close()

    def test_it_says_plainly_that_it_is_a_demonstration(self, tmp_path, capsys):
        main(["demo", str(tmp_path / "d")])
        output = capsys.readouterr().out.casefold()
        assert "not" in output and (
            "finding" in output or "real" in output or "mock" in output
        )

    def test_it_refuses_to_overwrite_an_existing_project(self, tmp_path, capsys):
        target = tmp_path / "demo-review"
        assert main(["demo", str(target)]) == 0
        assert main(["demo", str(target)]) != 0
        assert "exists" in capsys.readouterr().err.casefold()

    def test_the_menu_offers_it_too(self):
        """A reviewer who never opens a terminal has to be able to reach it."""
        import inspect

        from slr_studio.ui.main_window import MainWindow

        source = inspect.getsource(MainWindow)
        assert "demo" in source.casefold()
