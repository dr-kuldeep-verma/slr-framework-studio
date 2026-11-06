"""Stages that are not built yet.

They appear in the navigator from the first build so the shape of the work is
visible, and each one says plainly what it will do and which build stage
brings it. An empty page that pretends to be finished would be worse than one
that admits it is not.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy

from ..widgets import section_label
from .base import StagePage

__all__ = ["PlaceholderPage"]

#: Comfortable measure for centred prose. Wider than this and the eye loses
#: the start of the next line.
_COLUMN = 620


def _wrapped(text: str, policy: QSizePolicy.Policy) -> QLabel:
    """A centred, word-wrapped label that is given room for every line.

    A wrapped QLabel reports a height that assumes the full width of its
    parent, so inside a centred column the last line or two fall outside the
    widget and are never drawn. Fixing the width and asking the layout to
    respect height-for-width is what makes the label as tall as its text.
    """
    label = QLabel(text)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setFixedWidth(_COLUMN)
    size_policy = QSizePolicy(QSizePolicy.Policy.Fixed, policy)
    size_policy.setHeightForWidth(True)
    label.setSizePolicy(size_policy)
    label.setMinimumHeight(label.heightForWidth(_COLUMN))
    return label


class PlaceholderPage(StagePage):
    def __init__(self, title: str, summary: str, build_stage: str, parent=None) -> None:
        super().__init__(parent)
        self.title = title
        self.subtitle = summary

        self._layout.addStretch(1)
        heading = section_label(title)
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._layout.addWidget(heading)

        body = _wrapped(summary, QSizePolicy.Policy.Preferred)
        self._layout.addWidget(body, 0, Qt.AlignmentFlag.AlignHCenter)

        self._layout.addSpacing(10)
        stage = _wrapped(build_stage, QSizePolicy.Policy.Preferred)
        # Spacing above comes from the layout, not a stylesheet margin: a
        # margin is drawn inside the widget's height and would eat a line.
        stage.setStyleSheet("color: #5b6677;")
        self._layout.addWidget(stage, 0, Qt.AlignmentFlag.AlignHCenter)
        self._layout.addStretch(2)

    def requires_project(self) -> bool:
        return False
