"""What the shipped application must contain, declared rather than hoped for.

Both of these were found by building on Windows for the first time, and
neither could have been found on the machine the code was written on, where
everything happens to be installed already.

The first: a package needed by a Section 12 export was declared optional, so
the build installed without it and the export would have failed on the
reviewer's machine while passing every test here.

The second: the executable was built windowed, which on Windows means it has
no console, so everything the command-line mode printed went nowhere.
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "slr_studio"
SPEC = ROOT / "packaging" / "slr_studio.spec"


def _pyproject() -> dict:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)


def _requirement_name(spec: str) -> str:
    """"PyMuPDF>=1.24" -> "PyMuPDF". The distribution name, as written."""
    return re.split(r"[<>=!~\[ ;]", spec, maxsplit=1)[0].strip()


def _core_requirements() -> set[str]:
    out = set()
    for entry in _pyproject()["project"]["dependencies"]:
        name = re.split(r"[<>=!\[ ]", entry, maxsplit=1)[0]
        out.add(name.strip().casefold().replace("-", "_"))
    return out


#: The import name a reviewer's machine needs, against the distribution that
#: provides it.
_PROVIDES = {
    "docx": "python_docx",
    "pptx": "python_pptx",
    "PIL": "pillow",
    "fitz": "pymupdf",
    "pymupdf": "pymupdf",
    "yaml": "pyyaml",
}


def _third_party_imports(paths) -> set[str]:
    """Top-level packages a file needs, excluding our own and the optional.

    An import inside `try: ... except ImportError` is a package the code
    already copes without -- `lxml` in the REFI validator is there only to
    run an XSD the application does not ship. Those are genuinely optional,
    and demanding they be core dependencies would be wrong. An unguarded
    import is not optional, whatever the packaging says.
    """
    import sys

    found = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))

        guarded: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            def catches(handler) -> bool:
                if handler.type is None:          # bare except
                    return True
                dumped = ast.dump(handler.type)
                return any(
                    name in dumped
                    for name in ("ImportError", "ModuleNotFoundError",
                                 "Exception")
                )

            if not any(catches(handler) for handler in node.handlers):
                continue
            for inner in node.body:
                for descendant in ast.walk(inner):
                    guarded.add(id(descendant))

        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                found.add(node.module.split(".")[0])

    return {
        name for name in found
        if name not in sys.stdlib_module_names
        and name not in ("slr_studio", "__future__")
    }


class TestTheExportsCanActuallyRun:
    """Section 12's outputs may not depend on an optional extra.

    `reportlab` and `python-docx` were optional, under an extra the Windows
    build did not install. The build succeeded; the exports would have
    failed one by one on the reviewer's machine.
    """

    def test_every_package_the_exports_import_is_a_core_dependency(self):
        core = _core_requirements()
        missing = {}
        for path in sorted((SRC / "exports").glob("*.py")):
            for name in _third_party_imports([path]):
                distribution = _PROVIDES.get(name, name).casefold()
                if distribution not in core:
                    missing.setdefault(distribution, []).append(path.name)
        assert not missing, (
            "these are imported by an export but are not core dependencies, "
            f"so a packaged build will not have them: {missing}"
        )

    def test_the_figure_writers_too(self):
        core = _core_requirements()
        for path in (
            SRC / "framework" / "figures.py",
            SRC / "gaps" / "gapmap.py",
        ):
            for name in _third_party_imports([path]):
                distribution = _PROVIDES.get(name, name).casefold()
                assert distribution in core, (
                    f"{path.name} imports {name!r} ({distribution}), which is "
                    "not a core dependency"
                )

    def test_a_guarded_import_is_not_demanded(self):
        """The distinction the rule rests on, checked rather than assumed."""
        refi = SRC / "exports" / "refi.py"
        assert "lxml" in refi.read_text(encoding="utf-8")
        assert "lxml" not in _third_party_imports([refi])

    def test_reportlab_specifically(self):
        """Named, because this is the one that was wrong."""
        assert "reportlab" in _core_requirements()

    def test_python_docx_specifically(self):
        assert "python_docx" in _core_requirements()


class TestTheCommandLineWorksInTheShippedBuild:
    """A windowed executable on Windows has no console.

    The application's own build script ran `SLRFrameworkStudio.exe --version`
    and printed nothing at all, because the output went to a console that did
    not exist. Everything the pipeline prints -- progress, costs, the reason
    a run stopped -- was going nowhere.
    """

    def test_the_spec_builds_a_console_executable_as_well(self):
        text = SPEC.read_text(encoding="utf-8")
        assert text.count("EXE(") >= 2, (
            "the spec builds one executable; a windowed build has no console "
            "on Windows, so the command-line mode needs its own"
        )
        assert "console=True" in text.replace(" ", "")

    def test_the_windowed_one_is_still_windowed(self):
        """A console window flashing up behind the application is a fault too."""
        text = SPEC.read_text(encoding="utf-8").replace(" ", "")
        assert "console=False" in text

    def test_both_are_collected(self):
        text = SPEC.read_text(encoding="utf-8")
        assert "COLLECT(" in text
        # Both executables have to end up in the same folder, or the console
        # one is built and then thrown away.
        collect = text[text.index("COLLECT("):]
        assert collect.count("exe") >= 2 or "console_exe" in collect


class TestTheLicenceIsConsistent:
    """Two files that disagree about the licence is worse than neither.

    `pyproject.toml` said "Proprietary" while LICENSE said MIT. Published
    like that, nobody downloading it could tell what they were allowed to
    do, and the honest answer would have been "neither can the author".
    """

    def test_the_project_declares_a_licence(self):
        licence = _pyproject()["project"].get("license")
        assert licence, "no licence declared at all"

    def test_it_is_the_same_licence_as_the_LICENSE_file(self):
        declared = _pyproject()["project"]["license"]
        # A plain SPDX string, not the deprecated table form.
        assert isinstance(declared, str), (
            "declare the licence as an SPDX string; the table form is "
            "deprecated and warns on every build"
        )
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        first_line = text.strip().splitlines()[0]
        # The SPDX identifier is not a substring of the licence's own title:
        # AGPL-3.0 opens "GNU AFFERO GENERAL PUBLIC LICENSE". Matching on
        # the id alone passed for MIT by luck and would have passed a file
        # containing the GPL while pyproject said AGPL, which are different
        # licences differing in one clause.
        opening = {
            "AGPL-3.0": "GNU AFFERO GENERAL PUBLIC LICENSE",
            "GPL-3.0": "GNU GENERAL PUBLIC LICENSE",
            "LGPL-3.0": "GNU LESSER GENERAL PUBLIC LICENSE",
            "MIT": "MIT LICENSE",
            "Apache-2.0": "APACHE LICENSE",
            "BSD-3-Clause": "BSD",
        }
        stem = declared.removesuffix("-or-later").removesuffix("-only")
        expected = opening.get(stem)
        assert expected, (
            f"pyproject declares {declared!r}, which this test does not know "
            "how to check. Add its licence's opening line to the table above."
        )
        assert expected.casefold() in first_line.casefold(), (
            f"pyproject says {declared!r} but LICENSE begins {first_line!r}"
        )

    def test_the_agpl_text_is_the_whole_licence(self):
        """A truncated licence file grants nothing it means to grant.

        The AGPL differs from the GPL in one section, and that section is
        the reason this project is under it rather than the GPL. A copy
        missing it would be the wrong licence under the right name.
        """
        declared = _pyproject()["project"]["license"]
        if not declared.startswith("AGPL"):
            pytest.skip("not an AGPL project")
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        for required in (
            "Version 3, 19 November 2007",
            "TERMS AND CONDITIONS",
            "13. Remote Network Interaction",
            "END OF TERMS AND CONDITIONS",
            "How to Apply These Terms to Your New Programs",
        ):
            assert required in text, f"LICENSE is missing {required!r}"

    def test_the_package_declares_the_same_licence_as_the_build(self):
        """The About dialog reads its text from the package, not the file.

        Two places stating a licence is two places to get it wrong, so a
        test holds them together.
        """
        from slr_studio import __license__

        assert __license__ == _pyproject()["project"]["license"]

    def test_the_third_party_notices_name_every_dependency(self):
        """A notices file that omits something reads as a complete list.

        Shipping other people's code means saying whose it is. This fails
        when a dependency is added to pyproject and not to the notices.
        """
        notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        declared = _pyproject()["project"]["dependencies"]
        optional = _pyproject()["project"].get("optional-dependencies", {})
        names = [
            _requirement_name(spec)
            for spec in [*declared, *optional.get("gui", []), *optional.get("ocr", [])]
        ]
        missing = [
            name for name in names
            if name.casefold() not in notices.casefold()
        ]
        assert not missing, f"not mentioned in THIRD-PARTY-NOTICES.md: {missing}"

    def test_no_source_file_is_hidden_from_git(self):
        """The whole `exports` package was invisible to git for a week.

        `.gitignore` carried `exports/` to keep a reviewer's output folder
        out of the repository. A gitignore pattern with no leading slash
        matches at *any* depth, so it also matched
        `src/slr_studio/exports/` -- ten modules that write every Word,
        PDF, Excel and REFI-QDA output the application produces. They were
        never committed. The repository on GitHub was missing a package,
        and the Windows build made from it died on startup with
        "No module named 'slr_studio.exports'".

        Nothing in the application could have caught that: the code was
        right, the tests passed, and the fault was in what reached the
        repository. This is the check that would have.
        """
        import subprocess

        source_files = sorted(
            str(p.relative_to(ROOT))
            for p in (ROOT / "src").rglob("*.py")
            if "__pycache__" not in p.parts
        )
        assert source_files, "no source files found; this test is not running"

        result = subprocess.run(
            ["git", "check-ignore", "--stdin"],
            cwd=ROOT,
            input="\n".join(source_files),
            capture_output=True,
            text=True,
        )
        if result.returncode not in (0, 1):
            pytest.skip(f"git unavailable here: {result.stderr.strip()}")
        ignored = [line for line in result.stdout.splitlines() if line.strip()]
        assert not ignored, (
            "these source files are ignored by .gitignore and would never "
            "be committed:\n  " + "\n  ".join(ignored)
            + "\n\nA directory pattern needs a leading slash to mean 'at the "
            "top level only'."
        )

    def test_every_package_under_src_is_importable_from_the_tree(self):
        """A package with no `__init__.py` is collected by nothing.

        Separate from the gitignore fault above and with the same symptom
        at the far end: a missing module at runtime, on a user's machine,
        with the traceback pointing at the import rather than the cause.
        """
        missing = [
            str(folder.relative_to(ROOT))
            for folder in (ROOT / "src" / "slr_studio").iterdir()
            if folder.is_dir()
            and folder.name != "__pycache__"
            and any(folder.glob("*.py"))
            and not (folder / "__init__.py").is_file()
        ]
        assert not missing, f"packages with no __init__.py: {missing}"

    def test_the_build_ships_the_licence_beside_the_executable(self):
        """A downloaded folder that states no terms conveys no permission.

        The reviewer who unzips the build never sees this repository, so
        the licence and the third-party notices have to be in the folder
        with the .exe, not only here.
        """
        spec = (ROOT / "packaging" / "slr_studio.spec").read_text(encoding="utf-8")
        assert '"LICENSE"' in spec or '/ "LICENSE"' in spec, (
            "the PyInstaller spec does not bundle LICENSE"
        )
        assert "THIRD-PARTY-NOTICES.md" in spec, (
            "the PyInstaller spec does not bundle THIRD-PARTY-NOTICES.md"
        )

    def test_the_installer_shows_the_licence(self):
        installer = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
        assert "LicenseFile" in installer, (
            "the installer never shows the user what terms they are accepting"
        )

    def test_the_agpl_reason_is_recorded(self):
        """Why this is AGPL, where the next maintainer will look.

        It is not a preference: PyMuPDF is AGPL-or-commercial, and that
        decides it. Somebody who does not know that will eventually try to
        relicense this permissively and ship a licence violation.
        """
        notices = (ROOT / "THIRD-PARTY-NOTICES.md").read_text(encoding="utf-8")
        assert "PyMuPDF" in notices
        assert "AGPL" in notices
        # And the LGPL obligations Qt carries, which are separate.
        assert "LGPL" in notices and "dynamically linked" in notices

    def test_the_citation_file_agrees_too(self):
        declared = _pyproject()["project"]["license"]
        citation = (ROOT / "CITATION.cff").read_text(encoding="utf-8")
        assert f"license: {declared}" in citation, (
            "CITATION.cff must name the same licence; it is what a citing "
            "author reads"
        )


class TestTheDistributionIsClean:
    def test_compiled_caches_are_not_packaged(self):
        """`__pycache__` was being picked up as an importable package."""
        config = _pyproject().get("tool", {}).get("setuptools", {})
        exclude = config.get("packages", {}).get("find", {}).get("exclude", [])
        assert any("__pycache__" in entry for entry in exclude), (
            "setuptools found slr_studio.demo.__pycache__ and warned that it "
            "would be ignored; exclude it rather than shipping the warning"
        )


class TestTheBundledDataIsComplete:
    """Everything the demonstration reads has to be inside the build.

    The spec bundled `demo/data` and not `demo/pdfs`, so in the packaged
    application the demonstration imported its records, screened them, and
    then stopped with "there are no extracted labels to build a codebook
    from" -- because there were no full texts to extract any from. That is
    the first button a new user presses.
    """

    def _spec_datas(self) -> list[str]:
        text = SPEC.read_text(encoding="utf-8")
        block = text[text.index("datas = ["):text.index("icon_path =")]
        return re.findall(r'"slr_studio/([^"]+)"', block) + re.findall(
            r'/ "([a-z_]+)"\)', block
        )

    def test_every_data_directory_the_package_has_is_bundled(self):
        """Compare what is on disk against what the spec copies in."""
        bundled = " ".join(self._spec_datas())
        missing = []
        for directory in sorted(
            path for path in (SRC / "demo").iterdir()
            if path.is_dir() and path.name != "__pycache__"
        ):
            if directory.name not in bundled:
                missing.append(f"demo/{directory.name}")
        assert not missing, (
            f"on disk but not bundled: {missing}. The packaged application "
            "will not have them."
        )

    def test_the_demonstration_pdfs_specifically(self):
        """Named, because this is the one that was missing."""
        assert (SRC / "demo" / "pdfs").is_dir()
        assert "pdfs" in " ".join(self._spec_datas())

    def test_the_package_data_declaration_covers_them_too(self):
        """pip install has to carry them as well, not only PyInstaller."""
        declared = " ".join(
            _pyproject()["tool"]["setuptools"]["package-data"]["slr_studio"]
        )
        assert "pdfs" in declared and "demo" in declared
