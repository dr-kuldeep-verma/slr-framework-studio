"""The application window: a stage navigator on the left, the stage on the right."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __author__, __source_url__, __version__
from ..core.project import Project, ProjectError
from .scrolling import make_scrollable
from .pages import (
    DedupPage,
    ExportPage,
    ExtractPage,
    FrameworkPage,
    GapFinderPage,
    FullTextPage,
    ImportPage,
    PlaceholderPage,
    ProjectPage,
    RetrievePage,
    ScreenPage,
    SynthesisePage,
)

__all__ = ["MainWindow"]

#: Stages still to be built, with what each will do. Keeping them in the
#: navigator from day one means the reviewer can see the whole path.
_FUTURE_STAGES = [
]


class NewProjectDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New review")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Resident participation in sustainable tourism")
        form.addRow("Review title", self.title_edit)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Choose an empty folder for this review")
        folder_row.addWidget(self.folder_edit, 1)
        button = QPushButton("Browse…")
        button.clicked.connect(self._browse)
        folder_row.addWidget(button)
        form.addRow("Project folder", folder_row)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@university.edu")
        form.addRow("Contact email", self.email_edit)
        layout.addLayout(form)

        note = QLabel(
            "The folder holds project.slr, the imported files, PDFs, figures and "
            "exports. Nothing leaves your computer except the calls you make to "
            "a language model."
        )
        note.setWordWrap(True)
        note.setObjectName("hintLabel")
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        make_scrollable(self)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a project folder")
        if folder:
            self.folder_edit.setText(folder)

    def values(self) -> tuple[str, str, str]:
        return (
            self.folder_edit.text().strip(),
            self.title_edit.text().strip(),
            self.email_edit.text().strip(),
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.project: Project | None = None
        self.setWindowTitle(__app_name__)
        self.resize(1180, 760)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.nav = QListWidget()
        self.nav.setFixedWidth(208)
        self.nav.setObjectName("nav")
        # Styled in theme.py. The eleven stages are the spine of the
        # application, so how they look is not a per-window decision.
        layout.addWidget(self.nav)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        # The strip above the stage says what the page is, then what it is
        # for. One label had to do both jobs and did neither: the stage name
        # was nowhere on screen except in the navigator.
        header_bar = QWidget()
        header_bar.setObjectName("headerBar")
        header_layout = QVBoxLayout(header_bar)
        header_layout.setContentsMargins(28, 16, 28, 16)
        header_layout.setSpacing(3)
        self.header_title = QLabel()
        self.header_title.setObjectName("headerTitle")
        header_layout.addWidget(self.header_title)
        self.header = QLabel()
        self.header.setObjectName("headerSubtitle")
        # A long summary wraps onto a second line rather than running off the
        # right edge: a strip that grows is better than a sentence cut in half.
        self.header.setWordWrap(True)
        header_layout.addWidget(self.header)
        right_layout.addWidget(header_bar)

        # "What do I do next?" answered on every page: the next step for this
        # review, a button that goes to its page, and how to do it.
        self.next_bar = QFrame()
        self.next_bar.setObjectName("nextStepBar")
        next_layout = QHBoxLayout(self.next_bar)
        next_layout.setContentsMargins(28, 8, 28, 8)
        self.next_label = QLabel()
        self.next_label.setObjectName("nextStepText")
        self.next_label.setWordWrap(True)
        self.next_label.setTextFormat(Qt.TextFormat.RichText)
        next_layout.addWidget(self.next_label, 1)
        self.next_how = QPushButton("Show me how")
        self.next_how.clicked.connect(self.show_next_step)
        next_layout.addWidget(self.next_how)
        self.next_go = QPushButton()
        self.next_go.clicked.connect(self._go_to_next_step)
        next_layout.addWidget(self.next_go)
        self._next = None
        right_layout.addWidget(self.next_bar)

        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack, 1)
        layout.addWidget(right, 1)
        self.setCentralWidget(central)

        self._build_pages()
        self.nav.currentRowChanged.connect(self._change_page)
        self._build_menu()

        self.counts_label = QLabel("No project open")
        self.statusBar().addPermanentWidget(self.counts_label)
        self.statusBar().showMessage(f"{__app_name__} {__version__}")
        self.nav.setCurrentRow(0)
        self._set_project(None)

    # -- construction ------------------------------------------------------

    def _build_pages(self) -> None:
        self.pages: list = [
            ProjectPage(), ImportPage(), DedupPage(), ScreenPage(),
            RetrievePage(), FullTextPage(), ExtractPage(), SynthesisePage(),
            FrameworkPage(), GapFinderPage(), ExportPage(),
        ]
        for title, summary, stage in _FUTURE_STAGES:
            self.pages.append(PlaceholderPage(title, summary, stage))
        for page in self.pages:
            page.data_changed.connect(self._refresh_all)
            page.status_message.connect(self.statusBar().showMessage)
            self.stack.addWidget(page)
            self.nav.addItem(QListWidgetItem(page.title))

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        new_action = QAction("&New review…", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.triggered.connect(self.new_project)
        file_menu.addAction(new_action)

        open_action = QAction("&Open review…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_project)
        file_menu.addAction(open_action)

        file_menu.addSeparator()
        close_action = QAction("&Close review", self)
        close_action.triggered.connect(lambda: self._set_project(None))
        file_menu.addAction(close_action)

        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        tools_menu = self.menuBar().addMenu("&Tools")
        model_action = QAction("&Model settings…", self)
        model_action.triggered.connect(self.model_settings)
        tools_menu.addAction(model_action)
        tools_menu.addSeparator()

        verify_action = QAction("&Verify audit trail", self)
        verify_action.triggered.connect(self.verify_audit)
        tools_menu.addAction(verify_action)

        folder_action = QAction("Open project &folder", self)
        folder_action.triggered.connect(self.open_folder)
        tools_menu.addAction(folder_action)

        tools_menu.addSeparator()
        demo_action = QAction("Build &demonstration project…", self)
        demo_action.setStatusTip(
            "A small review carried end to end with a mock model, so the "
            "stages can be looked at before any API key is entered."
        )
        demo_action.triggered.connect(self.build_demo)
        tools_menu.addAction(demo_action)

        help_menu = self.menuBar().addMenu("&Help")
        next_action = QAction("&What do I do next?", self)
        next_action.setShortcut(QKeySequence("F1"))
        next_action.triggered.connect(self.show_next_step)
        help_menu.addAction(next_action)
        about_action = QAction("&About", self)
        about_action.triggered.connect(self.about)
        help_menu.addAction(about_action)

    # -- project -----------------------------------------------------------

    def new_project(self) -> None:
        dialog = NewProjectDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        folder, title, email = dialog.values()
        if not folder or not title:
            QMessageBox.warning(
                self, "Missing details", "A review needs a title and a folder."
            )
            return
        try:
            project = Project.create(
                folder, title, actor="human:desktop", contact_email=email
            )
        except ProjectError as exc:
            QMessageBox.critical(self, "Could not create the review", str(exc))
            return
        self._set_project(project)
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage(f"Created {title}")

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open review", "", "SLR project (project.slr);;All files (*)"
        )
        if not path:
            return
        try:
            project = Project.open(path, actor="human:desktop")
        except Exception as exc:
            QMessageBox.critical(self, "Could not open the review", str(exc))
            return
        self._set_project(project)

    def _set_project(self, project: Project | None) -> None:
        if self.project is not None and self.project is not project:
            self.project.close()
        self.project = project
        # Every page gets the new review even if one of them fails to
        # refresh. Stopping at the first failure left the pages after it
        # holding the review just closed, and the next action on one of them
        # failed with "Cannot operate on a closed database".
        problems: list[str] = []
        for page in self.pages:
            try:
                page.set_project(project)
            except Exception as exc:
                page.project = project
                problems.append(f"{page.title}: {exc}")
                from ..errorlog import log_exception

                log_exception(exc, where=f"opening review, page {page.title}")
        if problems:
            QMessageBox.warning(
                self, "Part of the review could not be shown",
                "The review is open, but these pages could not be refreshed:\n\n"
                + "\n".join(problems),
            )
        if project is None:
            self.setWindowTitle(__app_name__)
            self.counts_label.setText("No project open")
        else:
            info = project.info()
            self.setWindowTitle(f"{info['title']} — {__app_name__}")
        self._refresh_counts()
        self._refresh_next_step()
        self._change_page(self.nav.currentRow())

    def _refresh_all(self) -> None:
        for page in self.pages:
            page.refresh()
        self._refresh_counts()
        self._refresh_next_step()

    def _refresh_next_step(self) -> None:
        from ..core.next_step import next_step

        if self.project is None:
            self._next = None
            self.next_label.setText(
                "<b>Next step:</b> open a review (File → Open review…) or "
                "start one (File → New review…)."
            )
            self.next_go.setVisible(False)
            self.next_how.setVisible(False)
            return
        try:
            step = next_step(self.project)
        except Exception as exc:  # the guide must never break the window
            from ..errorlog import log_exception

            log_exception(exc, where="working out the next step")
            self.next_bar.setVisible(False)
            return
        self._next = step
        self.next_bar.setVisible(True)
        progress = f" <span style='color:#5b6677'>({step.progress})</span>" if step.progress else ""
        prefix = "Done:" if step.done else "Next step:"
        self.next_label.setText(f"<b>{prefix}</b> {step.headline}{progress}")
        self.next_how.setVisible(bool(step.steps or step.why))
        current = self.pages[self.nav.currentRow()].title if self.nav.currentRow() >= 0 else ""
        self.next_go.setText(f"Go to {step.page}")
        self.next_go.setVisible(step.page != current)

    def _go_to_next_step(self) -> None:
        if self._next is None:
            return
        for index, page in enumerate(self.pages):
            if page.title == self._next.page:
                self.nav.setCurrentRow(index)
                if self._next.focus and hasattr(page, "focus_on"):
                    page.focus_on(self._next.focus)
                return

    def show_next_step(self) -> None:
        """The next step's instructions, in a window of their own."""
        self._refresh_next_step()
        if self._next is None:
            QMessageBox.information(
                self, "What to do next",
                "Open a review with File → Open review…, or start one with "
                "File → New review….",
            )
            return
        step = self._next
        box = QMessageBox(self)
        box.setWindowTitle("What to do next")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"<b>{step.headline}</b>" + (f" ({step.progress})" if step.progress else ""))
        body = ([step.why, ""] if step.why else []) + [
            f"{i}. {line}" for i, line in enumerate(step.steps, 1)
        ]
        body.append(f"\nThis happens on the {step.page} page.")
        box.setInformativeText("\n".join(body))
        go = box.addButton(f"Go to {step.page}", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is go:
            self._go_to_next_step()

    def _refresh_counts(self) -> None:
        if self.project is None:
            return
        counts = self.project.counts()
        self.counts_label.setText(
            f"identified {counts.get('n_identified', 0)}  ·  "
            f"duplicates removed {counts.get('n_duplicates_removed', 0)}  ·  "
            f"to screen {counts.get('n_screened', 0)}"
        )

    def _change_page(self, row: int) -> None:
        if row < 0 or row >= len(self.pages):
            return
        page = self.pages[row]
        self.stack.setCurrentIndex(row)
        self.header_title.setText(page.title)
        self.header.setText(page.subtitle)
        self.header.setVisible(bool(page.subtitle))
        page.refresh()
        page.scroll_to_top()
        if hasattr(self, "next_bar"):
            self._refresh_next_step()

    # -- tools -------------------------------------------------------------

    def model_settings(self) -> None:
        if self.project is None:
            QMessageBox.information(
                self, "No review open",
                "The model is chosen per review, so that reopening a finished "
                "review shows what it was actually screened with. Open or "
                "create one first.",
            )
            return
        from ..ai.settings import load_settings, save_settings
        from .dialogs import ModelSettingsDialog

        dialog = ModelSettingsDialog(load_settings(self.project), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        save_settings(self.project, dialog.values(), actor="human:desktop")
        self.statusBar().showMessage("Model settings saved.")
        self._refresh_all()

    def verify_audit(self) -> None:
        if self.project is None:
            return
        breaks = self.project.audit.verify()
        protocol_problems = self.project.protocol.verify()
        if not breaks and not protocol_problems:
            n = len(self.project.audit.entries())
            QMessageBox.information(
                self,
                "Audit trail intact",
                f"All {n} entries verify against the hash chain, and every "
                "locked protocol version matches its stored hash. Nothing has "
                "been altered or removed since it was written.",
            )
        elif not breaks:
            QMessageBox.critical(
                self, "Protocol altered", "\n".join(protocol_problems)
            )
        else:
            detail = "\n".join(f"entry {b.entry_id}: {b.reason}" for b in breaks[:10])
            QMessageBox.critical(
                self,
                "Audit trail broken",
                f"{len(breaks)} problem(s) found.\n\n{detail}",
            )

    def open_folder(self) -> None:
        if self.project is None:
            return
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.project.root))))

    def build_demo(self) -> None:
        """Build the bundled demonstration project and open it.

        Somebody opening this application for the first time has no corpus
        and no API key, and an empty window with eleven stages that all say
        they need a project tells them nothing. This builds a small review
        end to end against a mock model so the stages can be clicked
        through, and says plainly that none of it is a finding.
        """
        from PySide6.QtWidgets import QApplication, QFileDialog

        folder = QFileDialog.getExistingDirectory(
            self, "Choose an empty folder for the demonstration project"
        )
        if not folder:
            return
        target = Path(folder)
        if any(target.iterdir()):
            QMessageBox.warning(
                self,
                "Choose an empty folder",
                f"{target} is not empty. Choose an empty folder: the "
                "demonstration will not be written over anything you have.",
            )
            return

        if (
            QMessageBox.question(
                self,
                "Build the demonstration project",
                "This builds a small review from two sample RIS files and "
                "three generated PDFs, carried all the way to a gap "
                "register. It uses a mock model, so it needs no API key and "
                "costs nothing, and it takes a minute or so.\n\n"
                "Nothing in it is a finding about anything.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        from ..demo import build_demo_project

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            project = build_demo_project(target, gaps=True)
        except Exception as exc:  # pragma: no cover - surfaced to the user
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "The demonstration could not be built", str(exc)
            )
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._set_project(project)
        self.statusBar().showMessage(
            "Demonstration project built. Nothing in it is a finding."
        )

    def about(self) -> None:
        # This dialog is the application's "Appropriate Legal Notices" in the
        # sense of section 5(d) of the AGPL: an interactive program has to
        # offer a prominent, convenient place showing the copyright, the
        # absence of warranty, that the work may be conveyed under this
        # licence, and how to read it. A Help menu item is what the licence
        # names as meeting that.
        QMessageBox.about(
            self,
            f"About {__app_name__}",
            f"<b>{__app_name__}</b> {__version__}<br><br>"
            "A PRISMA 2020 review workbench. AI makes the first pass at every "
            "stage; a human confirms or overrides each decision before it "
            "counts, and every decision of either kind is written to an "
            "append-only audit trail.<br><br>"
            "Your data stays on this computer apart from the calls you choose "
            "to make to a language model.<br><br>"
            "<hr>"
            f"Copyright © 2026 {__author__}<br><br>"
            "This program is free software: you may redistribute it and/or "
            "modify it under the terms of the GNU Affero General Public "
            "License as published by the Free Software Foundation, either "
            "version 3 of the licence, or (at your option) any later "
            "version.<br><br>"
            "It is distributed in the hope that it will be useful, but "
            "<b>with no warranty</b>; without even the implied warranty of "
            "merchantability or fitness for a particular purpose. See the "
            "GNU Affero General Public License for more detail.<br><br>"
            "The full licence is in the <code>LICENSE</code> file beside "
            "this program, and at "
            "<a href='https://www.gnu.org/licenses/'>gnu.org/licenses</a>. "
            "The source code is at "
            f"<a href='{__source_url__}'>{__source_url__}</a>.",
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.project is not None:
            self.project.close()
        super().closeEvent(event)
