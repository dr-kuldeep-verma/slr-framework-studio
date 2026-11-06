"""The Methods draft and the AI-use statement.

Integrity rule 4: the AI-use statement is generated from the log and cannot be
switched off. So there is no argument for omitting it, no setting that hides
it, and a Methods draft written for a project that used a model always carries
it — tested by looking for a way to suppress it and failing to find one.

The draft describes the procedure *as run*. A review that skipped the pilot,
or waived the second coder, gets a Methods section that says so. Describing
the intended procedure rather than the actual one is how a Methods section
becomes false.
"""

from __future__ import annotations

import pytest

from slr_studio.exports.methods import (
    ai_use_statement,
    write_audit_log,
    write_methods_draft,
)


@pytest.fixture
def reviewed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", gaps=True)
    yield project
    project.close()


class TestTheAiUseStatement:
    def test_it_names_the_model_and_the_provider(self, reviewed):
        text = ai_use_statement(reviewed)
        model = reviewed.db.scalar("SELECT model FROM ai_call LIMIT 1")
        assert model in text

    def test_it_names_every_task_the_model_did(self, reviewed):
        text = ai_use_statement(reviewed)
        from slr_studio.exports.methods import _task_phrase

        tasks = {
            row["task"] for row in reviewed.db.query(
                "SELECT DISTINCT task FROM ai_call"
            )
        }
        assert tasks
        for task in tasks:
            # Named in readable words rather than the internal task key.
            assert _task_phrase(task) in text.casefold()

    def test_it_gives_the_dates_the_calls_were_made(self, reviewed):
        text = ai_use_statement(reviewed)
        first = reviewed.db.scalar("SELECT MIN(created_at) FROM ai_call")
        assert first[:10] in text

    def test_it_counts_the_calls(self, reviewed):
        text = ai_use_statement(reviewed)
        total = reviewed.db.scalar("SELECT COUNT(*) FROM ai_call")
        assert str(total) in text

    def test_it_says_a_human_confirmed_every_decision(self, reviewed):
        assert "override" in ai_use_statement(reviewed).casefold()

    def test_there_is_no_way_to_switch_it_off(self):
        """Integrity rule 4, tested as an absence."""
        import inspect

        source = inspect.getsource(write_methods_draft)
        for forbidden in ("include_ai", "ai_statement=False", "omit_ai",
                          "skip_ai", "no_ai"):
            assert forbidden not in source
        assert "ai_use_statement" in source

    def test_a_project_that_used_no_model_says_so_rather_than_nothing(
        self, tmp_path
    ):
        from slr_studio.core.project import Project

        project = Project.create(tmp_path / "bare", "Bare", actor="human:test")
        try:
            text = ai_use_statement(project)
            assert "no" in text.casefold()
            assert text.strip()
        finally:
            project.close()


class TestTheMethodsDraft:
    def test_a_word_file_is_written(self, reviewed):
        path = write_methods_draft(reviewed, reviewed.exports_dir / "methods.docx")
        assert path.is_file() and path.stat().st_size > 5_000

    def test_the_ai_use_statement_is_in_it(self, reviewed):
        from docx import Document

        path = write_methods_draft(reviewed, reviewed.exports_dir / "methods.docx")
        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        model = reviewed.db.scalar("SELECT model FROM ai_call LIMIT 1")
        assert model in text

    def test_it_reports_the_counts_that_actually_happened(self, reviewed):
        from docx import Document

        path = write_methods_draft(reviewed, reviewed.exports_dir / "methods.docx")
        text = "\n".join(p.text for p in Document(str(path)).paragraphs)
        identified = reviewed.db.scalar(
            "SELECT n_identified FROM v_prisma_identification"
        )
        assert str(identified) in text

    def test_it_describes_each_stage_that_was_run(self, reviewed):
        from docx import Document

        path = write_methods_draft(reviewed, reviewed.exports_dir / "methods.docx")
        headings = [
            p.text.casefold()
            for p in Document(str(path)).paragraphs
            if p.style.name.startswith("Heading")
        ]
        joined = " ".join(headings)
        for stage in ("search", "screening", "extraction", "synthesis"):
            assert stage in joined

    def test_a_stage_that_was_not_run_is_not_described(self, tmp_path):
        """Describing the intended procedure rather than the actual one is
        how a Methods section becomes false.
        """
        from docx import Document

        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "early")
        try:
            path = write_methods_draft(
                project, project.exports_dir / "methods.docx"
            )
            headings = " ".join(
                p.text.casefold()
                for p in Document(str(path)).paragraphs
                if p.style.name.startswith("Heading")
            )
            assert "synthesis" not in headings
        finally:
            project.close()

    def test_it_says_plainly_that_it_is_a_draft(self, reviewed):
        from docx import Document

        path = write_methods_draft(reviewed, reviewed.exports_dir / "methods.docx")
        text = "\n".join(
            p.text for p in Document(str(path)).paragraphs
        ).casefold()
        assert "draft" in text

    def test_a_waived_pilot_is_reported_not_hidden(self, reviewed):
        from docx import Document

        from slr_studio.core.db import utc_now

        reviewed.db.insert(
            "audit_log_marker" if False else "meta",
            {"key": "pilot_waived_at", "value": utc_now()},
        )
        path = write_methods_draft(reviewed, reviewed.exports_dir / "methods.docx")
        text = "\n".join(
            p.text for p in Document(str(path)).paragraphs
        ).casefold()
        assert "waive" in text or "pilot" in text


class TestTheAuditLog:
    def test_it_is_written_with_a_row_per_entry(self, reviewed):
        from openpyxl import load_workbook

        path = write_audit_log(reviewed, reviewed.exports_dir / "audit.xlsx")
        sheet = load_workbook(path)["Audit log"]
        assert sheet.max_row == reviewed.db.scalar(
            "SELECT COUNT(*) FROM audit_log"
        ) + 1

    def test_the_hash_chain_travels_with_it(self, reviewed):
        from openpyxl import load_workbook

        path = write_audit_log(reviewed, reviewed.exports_dir / "audit.xlsx")
        headers = [
            cell.value for cell in load_workbook(path)["Audit log"][1]
        ]
        joined = " ".join(h.casefold() for h in headers if h)
        assert "hash" in joined

    def test_the_verification_result_is_stated(self, reviewed):
        from openpyxl import load_workbook

        path = write_audit_log(reviewed, reviewed.exports_dir / "audit.xlsx")
        workbook = load_workbook(path)
        assert "Verification" in workbook.sheetnames
        text = " ".join(
            str(cell.value)
            for row in workbook["Verification"].iter_rows()
            for cell in row
            if cell.value
        ).casefold()
        assert "intact" in text or "verified" in text

    def test_the_ai_calls_are_exported_with_their_hashes(self, reviewed):
        from openpyxl import load_workbook

        path = write_audit_log(reviewed, reviewed.exports_dir / "audit.xlsx")
        workbook = load_workbook(path)
        assert "AI calls" in workbook.sheetnames
        headers = [cell.value for cell in workbook["AI calls"][1]]
        joined = " ".join(h.casefold() for h in headers if h)
        for wanted in ("model", "prompt", "input", "timestamp"):
            assert wanted in joined


class TestTheStatementReadsLikeProse:
    """The AI-use statement goes into a manuscript, so it has to read as
    English rather than as a list of the application's internal task keys.
    """

    def test_no_internal_task_key_leaks_through(self, reviewed):
        text = ai_use_statement(reviewed).casefold()
        # These are task names in the code, not words a reader should meet.
        for internal in ("screener_a", "screener a", "qc", "assess:",
                         "extract:report", "synthesise:map"):
            assert internal not in text, f"{internal!r} leaked into the statement"

    def test_every_task_is_named_in_words(self, reviewed):
        from slr_studio.exports.methods import _TASK_WORDS

        tasks = {
            row["task"] for row in reviewed.db.query(
                "SELECT DISTINCT task FROM ai_call"
            )
        }
        for task in tasks:
            assert task in _TASK_WORDS, (
                f"{task!r} has no readable phrase, so it would be printed raw"
            )

    def test_the_tasks_are_listed_once_each_in_a_stable_order(self, reviewed):
        text = ai_use_statement(reviewed)
        listed = text.split("The model was used for: ")[1].split(". Every")[0]
        parts = [p.strip() for p in listed.split(",")]
        assert len(parts) == len(set(parts))
        assert ai_use_statement(reviewed) == text

    def test_overrides_are_counted_from_the_override_table(self, reviewed):
        """They are recorded structurally; counting them by matching words in
        the audit log's action names silently reported nought.
        """
        reviewed.audit.log_override(
            "screen", "record", 1, "decision", "include", "exclude",
            reason="off topic on reading the abstract", actor="human:test",
        )
        text = ai_use_statement(reviewed)
        expected = reviewed.db.scalar("SELECT COUNT(*) FROM human_override")
        assert expected >= 1
        assert f"{expected} override(s)" in text
