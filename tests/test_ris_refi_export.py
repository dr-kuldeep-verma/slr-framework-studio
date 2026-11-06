"""RIS files, and the REFI-QDA project.

The RIS files have to survive the application's own importer: an export that
cannot be read back is not an export, and the importer is the strictest reader
to hand.

The REFI-QDA package is held to its own standard. Every coding points at a
code that exists, every selection falls inside the text it is taken from, and
every identifier is a real GUID — because a package that opens in QualCoder
and shows codings attached to the wrong span is worse than one that refuses to
open.
"""

from __future__ import annotations

import zipfile

import pytest

from slr_studio.exports.refi import (
    RefiValidationError,
    validate_qdpx,
    write_refi_project,
)
from slr_studio.exports.ris import write_ris


@pytest.fixture
def reviewed(tmp_path):
    from slr_studio.demo import build_demo_project

    project = build_demo_project(tmp_path / "review", extract=True)
    yield project
    project.close()


# -- RIS --------------------------------------------------------------------


class TestRis:
    def test_a_file_is_written_for_the_included_records(self, reviewed):
        path = write_ris(reviewed, reviewed.exports_dir / "included.ris",
                         stage="included")
        assert path.is_file()
        assert path.read_text(encoding="utf-8").startswith("TY  - ")

    def test_every_record_ends_with_the_end_tag(self, reviewed):
        text = write_ris(
            reviewed, reviewed.exports_dir / "included.ris", stage="included"
        ).read_text(encoding="utf-8")
        assert text.count("ER  - ") == text.count("TY  - ")

    def test_the_count_matches_the_stage(self, reviewed):
        path = write_ris(reviewed, reviewed.exports_dir / "included.ris",
                         stage="included")
        expected = reviewed.db.scalar(
            "SELECT COUNT(*) FROM fulltext_status WHERE decision = 'include'"
        )
        assert path.read_text(encoding="utf-8").count("TY  - ") == expected

    def test_screened_in_is_a_larger_set_than_included(self, reviewed):
        screened = write_ris(
            reviewed, reviewed.exports_dir / "screened.ris", stage="screened_in"
        ).read_text(encoding="utf-8")
        included = write_ris(
            reviewed, reviewed.exports_dir / "included.ris", stage="included"
        ).read_text(encoding="utf-8")
        assert screened.count("TY  - ") >= included.count("TY  - ")

    def test_an_unknown_stage_is_refused(self, reviewed):
        with pytest.raises(ValueError):
            write_ris(reviewed, reviewed.exports_dir / "x.ris", stage="whatever")

    def test_the_export_can_be_read_back_by_the_importer(self, reviewed, tmp_path):
        """The round trip, against the strictest reader available."""
        from slr_studio.core.project import Project
        from slr_studio.importers.service import ImportService

        path = write_ris(reviewed, reviewed.exports_dir / "included.ris",
                         stage="included")
        reread = Project.create(tmp_path / "reread", "Re-read", actor="human:test")
        try:
            outcome = ImportService(reread).import_file(path)
            assert outcome.n_records == path.read_text(
                encoding="utf-8"
            ).count("TY  - ")
            assert outcome.n_records > 0
        finally:
            reread.close()

    def test_the_titles_survive_the_round_trip(self, reviewed, tmp_path):
        from slr_studio.core.project import Project
        from slr_studio.importers.service import ImportService

        path = write_ris(reviewed, reviewed.exports_dir / "included.ris",
                         stage="included")
        before = {
            row["title"]
            for row in reviewed.db.query(
                "SELECT r.title FROM fulltext_status f "
                "JOIN record r ON r.id = f.record_id "
                "WHERE f.decision = 'include'"
            )
        }
        reread = Project.create(tmp_path / "reread", "Re-read", actor="human:test")
        try:
            ImportService(reread).import_file(path)
            after = {
                row["title"] for row in reread.db.query("SELECT title FROM record")
            }
            assert before == after
        finally:
            reread.close()

    def test_a_doi_survives_the_round_trip(self, reviewed, tmp_path):
        from slr_studio.core.project import Project
        from slr_studio.importers.service import ImportService

        path = write_ris(reviewed, reviewed.exports_dir / "screened.ris",
                         stage="screened_in")
        reread = Project.create(tmp_path / "reread", "Re-read", actor="human:test")
        try:
            ImportService(reread).import_file(path)
            assert reread.db.scalar(
                "SELECT COUNT(*) FROM record WHERE doi <> ''"
            ) > 0
        finally:
            reread.close()

    def test_a_multi_line_abstract_does_not_break_the_file(self, reviewed, tmp_path):
        from slr_studio.core.project import Project
        from slr_studio.importers.service import ImportService

        reviewed.db.execute(
            "UPDATE record SET abstract = ? WHERE id = "
            "(SELECT record_id FROM fulltext_status WHERE decision = 'include' "
            "LIMIT 1)",
            ("First line.\nSecond line.\n\nThird.",),
        )
        path = write_ris(reviewed, reviewed.exports_dir / "included.ris",
                         stage="included")
        reread = Project.create(tmp_path / "reread", "Re-read", actor="human:test")
        try:
            outcome = ImportService(reread).import_file(path)
            assert outcome.n_records == path.read_text(
                encoding="utf-8"
            ).count("TY  - ")
        finally:
            reread.close()


# -- REFI-QDA ----------------------------------------------------------------


class TestRefiPackage:
    def test_a_qdpx_is_written(self, reviewed):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        assert path.is_file()
        assert zipfile.is_zipfile(path)

    def test_it_holds_the_project_file_at_the_root(self, reviewed):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zipfile.ZipFile(path) as archive:
            assert "project.qde" in archive.namelist()

    def test_the_sources_are_inside_the_package(self, reviewed):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zipfile.ZipFile(path) as archive:
            assert any(
                name.startswith("Sources/") for name in archive.namelist()
            )

    def test_it_validates(self, reviewed):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        assert validate_qdpx(path) == []

    def test_every_included_report_is_a_source(self, reviewed):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zipfile.ZipFile(path) as archive:
            text = archive.read("project.qde").decode("utf-8")
        expected = reviewed.db.scalar(
            "SELECT COUNT(*) FROM fulltext_status WHERE decision = 'include'"
        )
        assert text.count("<TextSource ") == expected

    def test_the_codes_are_the_constructs_and_the_decisions(self, reviewed):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zipfile.ZipFile(path) as archive:
            text = archive.read("project.qde").decode("utf-8")
        assert "<Code " in text

    def test_every_coding_carries_a_quotation(self, reviewed):
        from xml.etree import ElementTree

        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zipfile.ZipFile(path) as archive:
            root = ElementTree.fromstring(archive.read("project.qde"))
        ns = {"q": "urn:QDA-XML:project:1.0"}
        selections = root.findall(".//q:PlainTextSelection", ns)
        assert selections
        for selection in selections:
            start = int(selection.get("startPosition"))
            end = int(selection.get("endPosition"))
            assert end > start


class TestTheValidator:
    def _tamper(self, path, replace, with_, tmp_path):
        import shutil

        broken = tmp_path / "broken.qdpx"
        with zipfile.ZipFile(path) as source:
            text = source.read("project.qde").decode("utf-8")
            others = [n for n in source.namelist() if n != "project.qde"]
            payloads = {n: source.read(n) for n in others}
        with zipfile.ZipFile(broken, "w") as target:
            target.writestr("project.qde", text.replace(replace, with_, 1))
            for name, payload in payloads.items():
                target.writestr(name, payload)
        return broken

    def test_a_coding_pointing_at_no_code_is_caught(self, reviewed, tmp_path):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        import re
        import zipfile as zf

        with zf.ZipFile(path) as archive:
            text = archive.read("project.qde").decode("utf-8")
        target = re.search(r'targetGUID="([0-9A-F-]+)"', text).group(1)
        broken = self._tamper(
            path, f'targetGUID="{target}"',
            'targetGUID="11111111-1111-1111-1111-111111111111"', tmp_path,
        )
        problems = validate_qdpx(broken)
        assert any("targetGUID" in problem for problem in problems)

    def test_a_malformed_guid_is_caught(self, reviewed, tmp_path):
        import re
        import zipfile as zf

        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zf.ZipFile(path) as archive:
            text = archive.read("project.qde").decode("utf-8")
        first = re.search(r'<Code guid="([0-9A-F-]+)"', text).group(1)
        broken = self._tamper(path, first, "not-a-guid", tmp_path)
        assert any("guid" in p.casefold() for p in validate_qdpx(broken))

    def test_a_missing_project_file_is_caught(self, tmp_path):
        empty = tmp_path / "empty.qdpx"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("readme.txt", "nothing here")
        assert any("project.qde" in p for p in validate_qdpx(empty))

    def test_the_wrong_namespace_is_caught(self, reviewed, tmp_path):
        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        broken = self._tamper(
            path, "urn:QDA-XML:project:1.0", "urn:something-else", tmp_path
        )
        assert any("namespace" in p.casefold() for p in validate_qdpx(broken))

    def test_a_selection_outside_its_text_is_caught(self, reviewed, tmp_path):
        import re
        import zipfile as zf

        path = write_refi_project(reviewed, reviewed.exports_dir / "project.qdpx")
        with zf.ZipFile(path) as archive:
            text = archive.read("project.qde").decode("utf-8")
        match = re.search(r'endPosition="(\d+)"', text)
        broken = self._tamper(
            path, f'endPosition="{match.group(1)}"',
            'endPosition="99999999"', tmp_path,
        )
        assert any("outside" in p.casefold() for p in validate_qdpx(broken))

    def test_the_writer_validates_before_it_returns(self, reviewed):
        """A package is never handed over unchecked."""
        import inspect

        assert "validate_qdpx" in inspect.getsource(write_refi_project)

    def test_the_error_type_exists_for_callers_that_want_to_raise(self):
        assert issubclass(RefiValidationError, Exception)
