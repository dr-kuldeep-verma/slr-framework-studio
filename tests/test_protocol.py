"""Drafting, locking and amending the protocol."""

from __future__ import annotations

import pytest

from slr_studio.core.hashing import sha256_text
from slr_studio.core.protocol import ProtocolError, draft_protocol
from slr_studio.core.project import Project


@pytest.fixture
def filled(tmp_path):
    project = Project.create(
        tmp_path / "review",
        "Resident participation in sustainable tourism",
        actor="human:test",
        objectives="Map what is known about resident participation.",
        research_questions=["How do residents participate?", "What does it change?"],
        inclusion_criteria="Empirical studies\nAny country\n2000 onwards",
    )
    yield project
    project.close()


class TestDrafting:
    def test_the_document_covers_the_form(self, filled):
        text = filled.protocol.draft()
        assert "Resident participation in sustainable tourism" in text
        assert "How do residents participate?" in text
        assert "## Inclusion criteria" in text
        assert "E1" in text  # the exclusion ladder is tabulated

    def test_the_draft_is_a_pure_function_of_the_form(self, filled):
        # No timestamp in the body: a changed hash must mean a changed
        # protocol, not a different minute.
        assert filled.protocol.draft() == filled.protocol.draft()

    def test_changing_the_form_changes_the_document(self, filled):
        before = filled.protocol.draft()
        filled.update_info(inclusion_criteria="Empirical studies\nEnglish only")
        assert filled.protocol.draft() != before

    def test_unanswered_fields_say_so_rather_than_being_invented(self, tmp_path):
        project = Project.create(tmp_path / "bare", "Bare review")
        text = project.protocol.draft()
        assert "_Not yet stated._" in text
        project.close()

    def test_the_framework_choice_shapes_the_headings(self, filled):
        filled.update_info(framework_type="SPIDER")
        assert "Phenomenon of interest" in filled.protocol.draft()

    def test_a_built_in_framework_outside_the_four_names_its_own_stages(self, filled):
        filled.update_info(framework_type="CIMO")
        text = filled.protocol.draft()
        assert "organised against Context, Intervention, Mechanism and Outcome." in text
        assert "Population" not in text.split("## Framework")[1].split("##")[0]

    def test_a_reviewers_own_framework_names_its_own_stages(self, filled):
        filled.define_framework("ADO", ["antecedents", "decisions", "outcomes"])
        filled.update_info(framework_type="ADO")
        text = filled.protocol.draft()
        assert "## Framework: ADO" in text
        assert "organised against Antecedents, Decisions and Outcomes." in text

    def test_seed_studies_are_listed(self, filled):
        from slr_studio.importers.service import ImportService

        ImportService(filled).add_seed("10.1080/jsts.2021.004512")
        assert "10.1080/jsts.2021.004512" in filled.protocol.draft()

    def test_a_pipe_in_a_definition_does_not_break_the_table(self, tmp_path):
        text = draft_protocol(
            {"title": "T", "framework_type": "PCC"},
            [{"position": 1, "code": "E1", "label": "A", "definition": "x | y"}],
        )
        assert "x \\| y" in text


class TestLocking:
    def test_locking_stores_a_hash_of_the_content(self, filled):
        version = filled.protocol.lock(actor="human:test")
        assert version.version == 1
        assert version.content_hash == sha256_text(version.content)
        assert version.locked_by == "human:test"

    def test_a_locked_protocol_reports_no_pending_changes(self, filled):
        filled.protocol.lock(actor="human:test")
        assert filled.protocol.has_unlocked_changes() is False
        assert filled.protocol.pending_diff() == ""

    def test_locking_an_unchanged_protocol_is_refused(self, filled):
        filled.protocol.lock(actor="human:test")
        with pytest.raises(ProtocolError, match="unchanged"):
            filled.protocol.lock(actor="human:test")

    def test_locking_is_logged(self, filled):
        filled.protocol.lock(actor="human:test")
        entry = [e for e in filled.audit.entries() if e.action == "protocol_locked"][-1]
        assert entry.detail["version"] == 1
        assert filled.audit.verify() == []

    def test_the_locked_version_can_be_exported(self, filled):
        filled.protocol.lock(actor="human:test")
        path = filled.protocol.export(filled.exports_dir / "protocol.md")
        text = path.read_text(encoding="utf-8")
        assert "SHA-256" in text.splitlines()[0]
        assert "# Review protocol" in text

    def test_exporting_before_locking_is_refused(self, filled):
        with pytest.raises(ProtocolError, match="not been locked"):
            filled.protocol.export(filled.exports_dir / "protocol.md")


class TestAmending:
    def test_an_amendment_needs_a_reason(self, filled):
        filled.protocol.lock(actor="human:test")
        filled.update_info(inclusion_criteria="Empirical studies\nEnglish only")
        with pytest.raises(ProtocolError, match="needs a reason"):
            filled.protocol.lock(actor="human:test")

    def test_an_amendment_records_version_reason_and_diff(self, filled):
        filled.protocol.lock(actor="human:test")
        filled.update_info(inclusion_criteria="Empirical studies\nEnglish only")
        second = filled.protocol.lock(actor="human:test", reason="Reviewer asked")
        assert second.version == 2
        amendment = filled.protocol.amendments()[0]
        assert amendment["reason"] == "Reviewer asked"
        assert amendment["from_number"] == 1 and amendment["to_number"] == 2
        assert "English only" in amendment["diff"]

    def test_an_amendment_before_any_import_is_not_after_data(self, filled):
        filled.protocol.lock(actor="human:test")
        filled.update_info(objectives="Narrowed objectives")
        filled.protocol.lock(actor="human:test", reason="Narrowed")
        assert filled.protocol.amendments()[0]["after_data_seen"] == 0

    def test_an_amendment_after_an_import_is_recorded_as_such(self, filled, fixtures):
        from slr_studio.importers.service import ImportService

        filled.protocol.lock(actor="human:test")
        ImportService(filled).import_file(fixtures / "scopus_sample.ris")
        filled.update_info(objectives="Narrowed after seeing the records")
        filled.protocol.lock(actor="human:test", reason="Search was too broad")
        amendment = filled.protocol.amendments()[0]
        assert amendment["after_data_seen"] == 1

    def test_after_data_seen_is_observed_not_self_reported(self, filled, fixtures):
        from slr_studio.importers.service import ImportService

        assert filled.protocol.data_seen() is False
        ImportService(filled).import_file(fixtures / "scopus_sample.ris")
        assert filled.protocol.data_seen() is True

    def test_every_version_is_kept(self, filled):
        filled.protocol.lock(actor="human:test")
        filled.update_info(objectives="Second")
        filled.protocol.lock(actor="human:test", reason="r1")
        filled.update_info(objectives="Third")
        filled.protocol.lock(actor="human:test", reason="r2")
        assert [v.version for v in filled.protocol.versions()] == [1, 2, 3]
        assert len(filled.protocol.amendments()) == 2


class TestTamperDetection:
    def test_an_intact_protocol_verifies(self, filled):
        filled.protocol.lock(actor="human:test")
        assert filled.protocol.verify() == []

    def test_editing_a_locked_protocol_in_the_database_is_detected(self, filled):
        filled.protocol.lock(actor="human:test")
        filled.db.execute(
            "UPDATE protocol_version SET content = content || ' (quietly added)' "
            "WHERE version = 1"
        )
        problems = filled.protocol.verify()
        assert problems and "does not match its stored hash" in problems[0]
