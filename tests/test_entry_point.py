"""The desktop entry point, as the packaged build runs it.

PyInstaller does not import this module from its package; it runs the script
directly, so `__package__` is empty and a relative import raises
ImportError. That is what the first build of this application did, and the
reviewer would have seen a crash dialog instead of a window.

The whole file was at nought per cent coverage, because nothing ever ran it
the way the shipped build does.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
APP = SRC / "slr_studio" / "app.py"


class TestItRunsAsAScript:
    def test_the_module_has_no_relative_imports(self):
        """A relative import is what breaks under PyInstaller."""
        text = APP.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            assert not stripped.startswith("from ."), (
                f"{stripped!r} is a relative import; PyInstaller runs this "
                "file as __main__, where that raises ImportError."
            )

    def test_running_the_file_directly_does_not_fail_on_imports(self):
        """Run it the way the packaged build does, with no package context."""
        result = subprocess.run(
            [sys.executable, str(APP), "--version"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/bin:/bin", "QT_QPA_PLATFORM": "offscreen",
                 "PYTHONPATH": str(SRC), "HOME": "/tmp"},
        )
        assert "ImportError" not in result.stderr, result.stderr
        assert "attempted relative import" not in result.stderr

    def test_version_is_reported_without_opening_a_window(self):
        result = subprocess.run(
            [sys.executable, str(APP), "--version"],
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/bin:/bin", "QT_QPA_PLATFORM": "offscreen",
                 "PYTHONPATH": str(SRC), "HOME": "/tmp"},
        )
        assert result.returncode == 0, result.stderr
        from slr_studio import __version__

        assert __version__ in result.stdout


class TestOneExecutableDoesBothJobs:
    """The packaged build is the only thing a Windows user has.

    With no arguments it should open the window. With a pipeline command it
    should run that command and exit, because otherwise the shipped
    executable cannot be scripted, cannot be used on a server, and cannot be
    smoke-tested in CI -- the build would just open a window and hang.
    """

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(APP), *args],
            capture_output=True, text=True, timeout=180,
            env={"PATH": "/usr/bin:/bin", "QT_QPA_PLATFORM": "offscreen",
                 "PYTHONPATH": str(SRC), "HOME": "/tmp"},
        )

    def test_a_pipeline_command_runs_without_opening_a_window(self, tmp_path):
        result = self._run("demo", str(tmp_path / "d"), "--stage", "import")
        assert result.returncode == 0, result.stderr
        assert (tmp_path / "d" / "project.slr").is_file()

    def test_help_lists_the_pipeline_commands(self):
        result = self._run("--help")
        assert result.returncode == 0, result.stderr
        for command in ("demo", "import", "screen", "export-all"):
            assert command in result.stdout

    def test_an_unknown_command_is_an_error_not_a_window(self):
        result = self._run("not-a-command", "x")
        assert result.returncode != 0

    def test_the_help_says_that_no_arguments_opens_the_window(self):
        result = self._run("--help")
        assert "window" in result.stdout.casefold()
