"""Small reusable widgets."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

__all__ = ["DropArea", "SimpleTable", "hint_label", "section_label", "text_area"]


def section_label(text: str) -> QLabel:
    """A heading inside a page.

    The font is set here as well as in the stylesheet, so that a page still
    reads as headings and body text when it is built without the
    application stylesheet installed -- in a test, or in a dialog.
    """
    label = QLabel(text)
    label.setObjectName("sectionLabel")
    font = label.font()
    font.setPointSizeF(font.pointSizeF() + 1.5)
    font.setWeight(QFont.Weight.DemiBold)
    label.setFont(font)
    return label


def hint_label(text: str) -> QLabel:
    """Explanatory prose under a heading or beside a control.

    Word-wrapped, because every one of these is a sentence rather than a
    caption, and coloured by the stylesheet rather than here, so that the
    whole application has one grey rather than eleven.
    """
    label = QLabel(text)
    label.setObjectName("hintLabel")
    label.setWordWrap(True)
    return label


def text_area(placeholder: str = "", *, lines: int = 4) -> QPlainTextEdit:
    """A multi-line box that always shows at least *lines* lines.

    The height is measured from the font rather than written as a number of
    pixels. A fixed 80px was three lines on the machine this was written on
    and one clipped line on a Windows laptop at 150% scaling, where the
    reviewer's objectives were cut through the middle of their own letters.

    It also needs a *minimum*, not just a maximum. With only a maximum, a
    vertical layout with more content than room shrinks whichever children
    will let it, and a scrollable box will let it all the way down to the
    height of its own frame.
    """
    box = QPlainTextEdit()
    box.setPlaceholderText(placeholder)
    metrics = box.fontMetrics()
    # Frame, the document's own margins, and a little air so the text is not
    # against the border.
    chrome = 2 * box.frameWidth() + 16
    height = metrics.lineSpacing() * lines + chrome
    # Minimum and maximum together, which is to say a fixed height. Given
    # room to grow, a QPlainTextEdit's size hint takes all of it: three of
    # these on one form turned a page of fields into a page of empty boxes.
    # Longer text scrolls inside the box.
    box.setMinimumHeight(height)
    box.setMaximumHeight(height)
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    box.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    box.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
    return box


class DropArea(QFrame):
    """A dashed panel that accepts dropped files, with a button as a fallback.

    Drag and drop is the fast path; the button matters because not every
    reviewer is comfortable dragging, and because screen readers reach it.
    """

    files_dropped = Signal(list)

    def __init__(
        self,
        prompt: str = "Drop export files here",
        suffixes: Iterable[str] = (),
        file_filter: str = "All files (*)",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._suffixes = {s.casefold() for s in suffixes}
        self._filter = file_filter
        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(110)
        self.setObjectName("dropArea")
        self._set_active(False)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(prompt)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)

        row = QHBoxLayout()
        row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.browse_button = QPushButton("Choose files…")
        self.browse_button.clicked.connect(self._browse)
        row.addWidget(self.browse_button)
        layout.addLayout(row)

    def _set_active(self, active: bool) -> None:
        colour = "#2d6cdf" if active else "#9aa4b2"
        background = "#eef4ff" if active else "transparent"
        self.setStyleSheet(
            f"#dropArea {{ border: 2px dashed {colour}; border-radius: 8px; "
            f"background: {background}; }}"
        )

    def _accepted(self, paths: list[str]) -> list[str]:
        if not self._suffixes:
            return paths
        return [p for p in paths if Path(p).suffix.casefold() in self._suffixes]

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
            if self._accepted(paths):
                self._set_active(True)
                event.acceptProposedAction()
                return
        event.ignore()

    def dragLeaveEvent(self, event: object) -> None:  # noqa: N802
        self._set_active(False)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        self._set_active(False)
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        expanded: list[str] = []
        for path in paths:
            p = Path(path)
            # Dropping a folder of exports should work as well as dropping files.
            if p.is_dir():
                expanded.extend(str(child) for child in sorted(p.iterdir()) if child.is_file())
            else:
                expanded.append(path)
        accepted = self._accepted(expanded)
        if accepted:
            self.files_dropped.emit(accepted)
            event.acceptProposedAction()

    def _browse(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose files", "", self._filter)
        if paths:
            self.files_dropped.emit(list(paths))


class SimpleTable(QTableWidget):
    """A read-only table that sizes its columns sensibly."""

    def __init__(
        self,
        headers: list[str],
        parent: QWidget | None = None,
        *,
        stretch_columns: list[int] | None = None,
    ) -> None:
        super().__init__(0, len(headers), parent)
        self._fit_rows = 0
        self.setHorizontalHeaderLabels(headers)
        self.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        # Columns of prose have to be told to share the leftover width.
        # Left to size themselves to their contents they push the columns
        # after them off the edge of the panel.
        if len(headers) > 1:
            for index in stretch_columns if stretch_columns is not None else [0]:
                if 0 <= index < len(headers):
                    header.setSectionResizeMode(index, QHeaderView.ResizeMode.Stretch)

    def fill(self, rows: list[list[object]]) -> None:
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                item = QTableWidgetItem("" if value is None else str(value))
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.setItem(r, c, item)
        if self._fit_rows:
            self._resize_to_rows()

    def fit_to_rows(self, max_rows: int) -> None:
        """Grow with the data up to *max_rows*, then scroll.

        A table pinned to a fixed height wastes space when it holds two rows
        and hides data when it holds twenty.
        """
        self._fit_rows = max_rows
        self._resize_to_rows()

    def _resize_to_rows(self) -> None:
        shown = min(self.rowCount(), self._fit_rows) or 1
        height = self.horizontalHeader().height() + 2 * self.frameWidth() + 2
        for row in range(shown):
            height += self.rowHeight(row) or 24
        if self.rowCount() > self._fit_rows:
            height += 2
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
