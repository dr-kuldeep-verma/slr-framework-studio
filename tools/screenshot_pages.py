"""Render the window to PNGs so a layout can be looked at, not guessed at.

    QT_QPA_PLATFORM=offscreen python tools/screenshot_pages.py out/ [stage ...]

Every UI defect in this application so far -- clipped prose, squashed
fields, content below the fold with no scrollbar -- passed every test that
read a widget's value and was obvious in the first screenshot. So this
exists, and it renders the real window with the real theme rather than a
widget on its own, because most of the faults were in the interaction
between the two.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

from slr_studio.core.project import Project
from slr_studio.demo import build_demo_project
from slr_studio.ui.main_window import MainWindow
from slr_studio.ui.theme import apply_theme


def main(argv: list[str]) -> int:
    out = Path(argv[0] if argv else "screenshots")
    wanted = {name.casefold() for name in argv[1:]}
    out.mkdir(parents=True, exist_ok=True)

    app = QApplication.instance() or QApplication([])
    apply_theme(app)

    # A demonstration project, so the tables have rows in them. An empty
    # project hides exactly the layout faults that matter.
    workspace = out / "_project"
    if (workspace / "project.slr").exists():
        project = Project.open(workspace / "project.slr", actor="human:screenshot")
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        project = build_demo_project(workspace, gaps=True)

    window = MainWindow()
    window._set_project(project)
    window.resize(1280, 820)
    window.show()
    app.processEvents()

    written = []
    for row in range(window.nav.count()):
        window.nav.setCurrentRow(row)
        for _ in range(3):
            app.processEvents()
        page = window.stack.currentWidget()
        if wanted and page.title.casefold() not in wanted:
            continue
        name = page.title.lower().replace(" ", "-")
        path = out / f"{row:02d}-{name}.png"
        window.grab().save(str(path))
        written.append(path)

        # Also the whole page at its natural height, which is what the
        # scroll area exists to let the reviewer reach.
        content = page._content
        content.adjustSize()
        app.processEvents()
        if content.height() > page.height():
            content.grab().save(str(out / f"{row:02d}-{name}-full.png"))

    print("\n".join(str(p) for p in written))
    window.close()
    project.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
