"""Regenerate the screenshots in docs/screenshots from a demonstration review.

    QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py

Run this after any change to the interface. Screenshots that show an older
build are worse than none: the first set in this repository was taken
before the pages were restyled, so every one of them showed a cramped
window with fields a reviewer could not read, which is not the application
anybody would download.

Everything shown is the demonstration project, built against the mock
model. No API key, no network, and nothing in it is a finding.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from slr_studio.core.project import Project
from slr_studio.demo import build_demo_project
from slr_studio.ui.main_window import MainWindow
from slr_studio.ui.theme import apply_theme

#: Stage name -> the file it is written to. Only the stages worth showing a
#: newcomer: eleven near-identical screenshots teach less than seven chosen
#: ones.
WANTED = {
    "Project": "01-project.png",
    "Import": "02-import.png",
    "Screen": "03-screen.png",
    "Full-text": "04-fulltext.png",
    "Extract": "05-extract.png",
    "Synthesise": "06-synthesise.png",
    "Framework": "07-framework.png",
    "Gap Finder": "08-gap-finder.png",
    "Export": "09-export.png",
}

#: Which tab to open before grabbing, where the first one is not the one
#: worth showing. The Gap Finder opens on the quick scan, which is a
#: scoping view deliberately described as not a finding; the register of
#: 84 gaps on the next tab is what the stage is for.
TAB = {
    "Gap Finder": 1,      # 2. Gap register
    "Synthesise": 0,      # 1. Codebook
}

WIDTH, HEIGHT = 1280, 820


def _populate_detail_panes(page, app) -> None:
    """Select the first row of each table, so detail panes are not blank.

    A screenshot of an empty right-hand panel teaches a reader that half
    the window does nothing. Selecting a row is what any user does within
    a second of arriving, and it shows the thing the panel is for.
    """
    from PySide6.QtWidgets import QTableWidget

    for table in page.findChildren(QTableWidget):
        if table.rowCount() and table.isVisible():
            table.selectRow(0)
            app.processEvents()


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    out = root / "docs" / "screenshots"
    workspace = Path(argv[0]) if argv else root / ".screenshot-project"

    app = QApplication.instance() or QApplication([])
    apply_theme(app)

    if (workspace / "project.slr").exists():
        project = Project.open(workspace / "project.slr", actor="human:screenshot")
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        project = build_demo_project(workspace, gaps=True)

    # Out with the old: a stale screenshot is a wrong screenshot, and
    # leaving one behind beside a new one is how a README ends up showing
    # two different applications.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    window = MainWindow()
    window._set_project(project)
    window.resize(WIDTH, HEIGHT)
    window.show()
    app.processEvents()

    written: list[str] = []
    for row in range(window.nav.count()):
        window.nav.setCurrentRow(row)
        for _ in range(3):
            app.processEvents()
        page = window.stack.currentWidget()
        name = WANTED.get(page.title)
        if not name:
            continue
        if page.title in TAB:
            from PySide6.QtWidgets import QTabWidget

            for tabs in page.findChildren(QTabWidget):
                tabs.setCurrentIndex(TAB[page.title])
                app.processEvents()
                break
        _populate_detail_panes(page, app)
        window.grab().save(str(out / name))
        written.append(name)

    # One extra: the choice that decides how the framework gets built, which
    # is the question a reviewer has to answer before the synthesis stage
    # means anything.
    project.update_info(
        framework_approach="top_down",
        a_priori_theory="Social Exchange Theory",
        a_priori_citation=(
            "Ap, J. (1992). Residents' perceptions on tourism impacts. "
            "Annals of Tourism Research, 19(4), 665-690."
        ),
        a_priori_constructs=(
            "Perceived benefit — what residents judge they gain from tourism\n"
            "Perceived cost\n"
            "Trust in local government"
        ),
    )
    window.nav.setCurrentRow(0)
    for _ in range(3):
        app.processEvents()
    page = window.stack.currentWidget()
    page._content.adjustSize()
    app.processEvents()
    page._content.grab().save(str(out / "10-framework-approach.png"))
    written.append("10-framework-approach.png")

    window.close()
    project.close()
    if not argv:
        shutil.rmtree(workspace, ignore_errors=True)

    missing = set(WANTED.values()) - set(written)
    for name in sorted(written):
        print(f"  {name}")
    if missing:
        print(f"MISSING: {sorted(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
