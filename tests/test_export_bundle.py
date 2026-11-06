"""Everything Section 12 lists, written in one go.

The bundle is what a reviewer actually uses: one action, and the folder holds
the diagram, the checklist, the logs, the RIS files, the REFI-QDA package, the
tables, the figures, the Methods draft and the audit log. The test that
matters is that nothing on the spec's list is quietly missing.

What the project has not reached yet is skipped rather than written empty, and
the result says which — an empty graded inventory in the folder would be read
as a finding.
"""

from __future__ import annotations

import pytest

from slr_studio.exports.bundle import SECTION_12, export_everything


@pytest.fixture
def reviewed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", gaps=True)
    yield project
    project.close()


class TestTheBundle:
    def test_every_item_the_spec_lists_is_covered(self):
        """Section 12's list, as a checklist against the code."""
        kinds = {item.kind for item in SECTION_12}
        assert {
            "prisma_diagram", "prisma_checklist", "screening_log",
            "extraction_matrix", "ris", "refi", "graded_inventory",
            "synthesis_tables", "figures", "methods", "audit_log",
        } <= kinds

    def test_it_writes_the_lot(self, reviewed):
        outcome = export_everything(reviewed)
        assert outcome.written
        for path in outcome.written:
            assert path.is_file(), path
            assert path.stat().st_size > 0, path

    def test_the_prisma_diagram_is_there_in_both_formats(self, reviewed):
        outcome = export_everything(reviewed)
        names = {path.name for path in outcome.written}
        assert any(n.startswith("prisma-flow") and n.endswith(".docx")
                   for n in names)
        assert any(n.startswith("prisma-flow") and n.endswith(".pdf")
                   for n in names)

    def test_the_refi_package_validates(self, reviewed):
        from slr_studio.exports.refi import validate_qdpx

        outcome = export_everything(reviewed)
        package = next(
            path for path in outcome.written if path.suffix == ".qdpx"
        )
        assert validate_qdpx(package) == []

    def test_the_audit_log_is_included(self, reviewed):
        outcome = export_everything(reviewed)
        assert any("audit" in path.name for path in outcome.written)

    def test_the_methods_draft_is_included(self, reviewed):
        outcome = export_everything(reviewed)
        assert any("methods" in path.name for path in outcome.written)

    def test_a_stage_not_reached_is_skipped_and_said_so(self, tmp_path):
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "early")
        try:
            outcome = export_everything(project)
            assert outcome.skipped
            for kind, reason in outcome.skipped.items():
                assert reason, kind
            names = " ".join(path.name for path in outcome.written)
            assert "graded-inventory" not in names
        finally:
            project.close()

    def test_a_skipped_item_leaves_no_empty_file_behind(self, tmp_path):
        """An empty inventory in the folder would be read as a finding."""
        from slr_studio.demo import build_demo_project

        project = build_demo_project(tmp_path / "early")
        try:
            export_everything(project)
            names = {
                path.name for path in project.exports_dir.rglob("*")
                if path.is_file()
            }
            assert not any("graded-inventory" in name for name in names)
        finally:
            project.close()

    def test_a_manifest_lists_what_was_written(self, reviewed):
        outcome = export_everything(reviewed)
        manifest = next(
            path for path in outcome.written if path.name == "manifest.md"
        )
        text = manifest.read_text(encoding="utf-8")
        for path in outcome.written:
            if path.name != "manifest.md":
                assert path.name in text

    def test_the_manifest_records_whether_the_audit_chain_verified(
        self, reviewed
    ):
        outcome = export_everything(reviewed)
        manifest = next(
            path for path in outcome.written if path.name == "manifest.md"
        )
        text = manifest.read_text(encoding="utf-8").casefold()
        assert "audit" in text and ("intact" in text or "broken" in text)

    def test_one_failure_does_not_lose_the_rest(self, reviewed, monkeypatch):
        """A reviewer should get eleven files and a note, not nothing."""
        import slr_studio.exports.bundle as bundle

        def explode(*args, **kwargs):
            raise RuntimeError("the inventory writer fell over")

        monkeypatch.setattr(bundle, "write_graded_inventory", explode)
        outcome = export_everything(reviewed)
        assert outcome.written
        assert any(
            "fell over" in reason for reason in outcome.failed.values()
        )

    def test_the_outcome_describes_itself(self, reviewed):
        outcome = export_everything(reviewed)
        text = outcome.describe()
        assert str(len(outcome.written)) in text
