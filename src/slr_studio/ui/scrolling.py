"""Pop-up windows that never hide their text.

Two problems on a laptop screen. A dialog with a lot in it (Model settings,
the protocol window) can be taller than the screen, which cuts off the
bottom, Save button included. And Qt's message boxes do not scroll at all, so
a long explanation runs off the screen with no way to read the end.

* :func:`make_scrollable` puts a dialog's contents in a scroll area, keeps
  its buttons fixed at the bottom, and never lets it open taller than the
  screen.
* :func:`install_scrolling_messages` makes QMessageBox's information,
  warning, critical and question pop-ups scroll when their text is long.
  Short messages still use the ordinary message box.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QScrollArea,
    QStyle,
    QVBoxLayout,
    QWidget,
)

__all__ = ["FittingScrollArea", "make_scrollable", "install_scrolling_messages", "scrolling_message"]

#: Never open a pop-up taller than this share of the screen.
SCREEN_SHARE = 0.85
#: Messages longer than this (characters or lines) get the scrolling window.
LONG_TEXT_CHARS = 600
LONG_TEXT_LINES = 14


def _screen_height(widget: QWidget | None) -> int:
    screen = None
    if widget is not None:
        screen = widget.screen()
    if screen is None:
        screen = QGuiApplication.primaryScreen()
    if screen is None:  # pragma: no cover - no display at all
        return 800
    return screen.availableGeometry().height()


class FittingScrollArea(QScrollArea):
    """A scroll area as tall as its contents, up to most of the screen.

    A plain QScrollArea asks for a fixed small size whatever it holds, so a
    dialog built around one opened squashed. This one asks for its
    contents' height at the width it has, wrapped text included.
    """

    def __init__(self, parent: QWidget | None = None, reserve: int = 0) -> None:
        super().__init__(parent)
        #: Height the rest of the window needs (buttons, margins).
        self.reserve = reserve
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt's name
        inner = self.widget()
        if inner is None:
            return super().sizeHint()
        width = max(self.viewport().width(), inner.minimumSizeHint().width(), 200)
        layout = inner.layout()
        if layout is not None and layout.hasHeightForWidth():
            height = layout.totalHeightForWidth(width)
        else:
            height = inner.sizeHint().height()
        cap = int(_screen_height(self) * SCREEN_SHARE) - self.reserve
        return QSize(inner.sizeHint().width(), max(120, min(height + 2, cap)))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt's name
        return QSize(super().minimumSizeHint().width(), 120)


def make_scrollable(dialog: QDialog) -> FittingScrollArea:
    """Move a dialog's contents into a scroll area, leaving its buttons.

    Call it at the end of the dialog's constructor, once its top-level
    layout is complete. A QDialogButtonBox that is the last item stays
    outside the scroll area so Save and Cancel are always visible.
    """
    outer = dialog.layout()
    if outer is None:
        raise ValueError("make_scrollable needs a dialog with a layout.")

    stretches = [outer.stretch(i) for i in range(outer.count())] if hasattr(outer, "stretch") else []
    items = []
    while outer.count():
        items.append(outer.takeAt(0))
    footer = None
    if items and isinstance(items[-1].widget(), QDialogButtonBox):
        footer = items.pop()

    body = QWidget()
    inner = QVBoxLayout(body)
    inner.setContentsMargins(0, 0, 8, 0)  # room beside the scroll bar
    inner.setSpacing(outer.spacing())
    for index, item in enumerate(items):
        stretch = stretches[index] if index < len(stretches) else 0
        if item.widget() is not None:
            inner.addWidget(item.widget(), stretch)
        elif item.layout() is not None:
            child: QLayout = item.layout()
            child.setParent(None)
            inner.addLayout(child)
        else:
            inner.addItem(item)

    footer_height = footer.widget().sizeHint().height() if footer is not None else 0
    margins = outer.contentsMargins()
    reserve = footer_height + outer.spacing() + margins.top() + margins.bottom() + 40
    area = FittingScrollArea(dialog, reserve=reserve)
    area.setWidget(body)
    outer.addWidget(area, 1)
    if footer is not None:
        outer.addWidget(footer.widget())
    dialog.setMaximumHeight(int(_screen_height(dialog) * SCREEN_SHARE) + 40)
    dialog._scroll_area = area  # type: ignore[attr-defined]
    return area


# -- message boxes ---------------------------------------------------------------

_ORIGINAL: dict[str, Any] = {}
_ICONS = {
    "information": QStyle.StandardPixmap.SP_MessageBoxInformation,
    "warning": QStyle.StandardPixmap.SP_MessageBoxWarning,
    "critical": QStyle.StandardPixmap.SP_MessageBoxCritical,
    "question": QStyle.StandardPixmap.SP_MessageBoxQuestion,
}
_DEFAULT_BUTTONS = {
    "information": QMessageBox.StandardButton.Ok,
    "warning": QMessageBox.StandardButton.Ok,
    "critical": QMessageBox.StandardButton.Ok,
    "question": QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
}


def _is_long(text: str) -> bool:
    return len(text) > LONG_TEXT_CHARS or text.count("\n") + 1 > LONG_TEXT_LINES


def _value(flag: Any) -> int:
    return int(getattr(flag, "value", flag))


def scrolling_message(
    kind: str,
    parent: QWidget | None,
    title: str,
    text: str,
    buttons: Any = None,
    default_button: Any = None,
) -> QMessageBox.StandardButton:
    """A message window whose text scrolls. Returns the button pressed."""
    buttons = _DEFAULT_BUTTONS[kind] if buttons is None else buttons
    wanted = _value(buttons)

    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.setMinimumWidth(520)
    layout = QVBoxLayout(dialog)

    row = QHBoxLayout()
    icon = QLabel()
    size = dialog.style().pixelMetric(QStyle.PixelMetric.PM_MessageBoxIconSize)
    icon.setPixmap(dialog.style().standardIcon(_ICONS[kind]).pixmap(size, size))
    icon.setAlignment(Qt.AlignmentFlag.AlignTop)
    row.addWidget(icon)
    label = QLabel(text)
    label.setObjectName("messageText")
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    row.addWidget(label, 1)
    layout.addLayout(row)

    box = QDialogButtonBox()
    order = [
        QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Ok, QMessageBox.StandardButton.Save,
        QMessageBox.StandardButton.Discard, QMessageBox.StandardButton.Retry,
        QMessageBox.StandardButton.Ignore, QMessageBox.StandardButton.Abort,
        QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Close,
    ]
    present = [b for b in order if wanted & _value(b)]
    for standard in present:
        box.addButton(QDialogButtonBox.StandardButton(_value(standard)))
    layout.addWidget(box)

    chosen: dict[str, Any] = {}

    def clicked(button) -> None:
        chosen["button"] = QMessageBox.StandardButton(_value(box.standardButton(button)))
        dialog.accept()

    box.clicked.connect(clicked)
    if default_button is not None and _value(default_button):
        push = box.button(QDialogButtonBox.StandardButton(_value(default_button)))
        if push is not None:
            push.setDefault(True)
            push.setFocus()

    make_scrollable(dialog)
    dialog.resize(max(dialog.width(), 560), dialog.sizeHint().height())
    dialog.exec()
    if "button" in chosen:
        return chosen["button"]
    # Closed with Escape or the window's X: the safe answer.
    for safe in (QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.No):
        if safe in present:
            return safe
    return present[0] if present else QMessageBox.StandardButton.NoButton


def _wrapper(kind: str):
    def show(parent, title, text, buttons=None, defaultButton=None, **kwargs):  # noqa: N803
        original = _ORIGINAL[kind]
        text = str(text)
        if not _is_long(text) or QApplication.instance() is None:
            args = [parent, title, text]
            if buttons is not None:
                args.append(buttons)
                if defaultButton is not None:
                    args.append(defaultButton)
            return original(*args, **kwargs)
        return scrolling_message(kind, parent, title, text, buttons, defaultButton)

    show.__name__ = kind
    return staticmethod(show)


def install_scrolling_messages() -> None:
    """Make every long QMessageBox pop-up in the application scroll."""
    for kind in _ICONS:
        if kind not in _ORIGINAL:
            _ORIGINAL[kind] = getattr(QMessageBox, kind)
        setattr(QMessageBox, kind, _wrapper(kind))
