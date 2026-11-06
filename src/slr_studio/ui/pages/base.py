"""Shared behaviour for stage pages."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.project import Project

__all__ = ["StagePage"]


class StagePage(QWidget):
    """A page in the stage navigator.

    Pages never hold their own copy of the data. They read from the project on
    :meth:`refresh`, which the window calls whenever the project changes or the
    page is shown, so two pages can never disagree about the counts.

    Everything a page builds goes into ``self._layout``, which is inside a
    scroll area. That is not a detail: these pages are tall -- a form, a
    couple of tables and a run log -- and on a laptop screen the bottom of
    most of them is below the fold. Built without the scroll area they did
    not merely hide that content, they made it unreachable, and squeezed the
    input boxes down to a clipped single line trying to fit. Subclasses get
    the scrolling for free and cannot forget it.
    """

    #: Emitted when this page changed something other pages display.
    data_changed = Signal()
    #: Emitted with a message for the status bar.
    status_message = Signal(str)

    title: str = ""
    subtitle: str = ""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project: Project | None = None

        frame = QVBoxLayout(self)
        frame.setContentsMargins(0, 0, 0, 0)
        frame.setSpacing(0)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("pageScroll")
        # The content widget is resized to the viewport width, so prose wraps
        # to the window instead of scrolling sideways.
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self._scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )

        self._content = QWidget()
        self._content.setObjectName("pageContent")
        self._scroll.setWidget(self._content)
        frame.addWidget(self._scroll)

        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(28, 24, 28, 32)
        self._layout.setSpacing(14)

    def scroll_to_top(self) -> None:
        """Put the page back at the top. Called when the page is shown.

        Without this, coming back to a page leaves it wherever it was left,
        which reads as a page that has lost its heading.
        """
        self._scroll.verticalScrollBar().setValue(0)

    def set_project(self, project: Project | None) -> None:
        self.project = project
        self.setEnabled(project is not None or not self.requires_project())
        self.refresh()

    def refresh(self) -> None:
        """Re-read everything this page shows. Safe to call repeatedly."""

    def requires_project(self) -> bool:
        """False for pages that are readable without a project open."""
        return True
