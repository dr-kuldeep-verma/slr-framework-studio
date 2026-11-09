"""One place that decides how the application looks.

Two reasons this is a module rather than stylesheets scattered through the
pages. The first is consistency: before it existed, the same grey was
written out as a hex code in forty places, so changing it meant finding all
forty. The second is Windows dark mode. Qt follows the system theme, and a
light stylesheet applied over a dark palette produces grey text on a grey
background in exactly the places the stylesheet does not mention. Setting
the palette and the stylesheet together, from one set of colours, is what
stops that.

The colours are deliberately quiet. This is a tool somebody reads
screenfuls of titles and abstracts in for days at a time, so the page is
near-white, the text is near-black, and the one saturated colour is spent
on the thing the reviewer is meant to press next. Red and amber are
reserved for a decision that needs attention, which means nothing else may
use them.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QPalette

__all__ = ["COLOURS", "STYLESHEET", "apply_theme", "app_icon"]

COLOURS = {
    # Surfaces, from the page backwards.
    "window": "#f4f6f9",      # behind everything
    "surface": "#ffffff",     # cards, tables, inputs
    "sunken": "#eef1f6",      # alternate table rows, disabled inputs
    "border": "#dde3ec",      # hairlines
    "border_strong": "#c5cedb",
    # Text.
    "text": "#1b2330",
    "muted": "#5b6677",       # explanatory prose, secondary columns
    "faint": "#8b95a5",       # placeholders
    # The accent, spent only on the next action.
    "accent": "#2f5fd0",
    "accent_hover": "#2851bb",
    "accent_pressed": "#23459e",
    "accent_wash": "#e8eefc",  # selected rows, selected nav item
    # Reserved for a decision that needs attention.
    "warning": "#9a4b06",
    "warning_wash": "#fdf3e7",
    "danger": "#a4262c",
}

#: Applied to the whole application. Qt's stylesheet dialect is CSS-like but
#: not CSS: no variables, no shorthand for transitions, and a selector that
#: does not match is silently ignored, so everything here is spelled out.
STYLESHEET = """
/* No blanket `QWidget {{ background }}` rule. The palette already sets the
   window colour, and a stylesheet rule that matches every widget also
   matches every QLabel, which Qt then paints opaque: the result was a grey
   rectangle behind each form label on a white card, and a band across the
   header. Backgrounds are set on the containers that need one. */

QMainWindow, QDialog {{ background: {window}; }}
QLabel {{ background: transparent; }}

QToolTip {{
    background: {text};
    color: {surface};
    border: none;
    padding: 6px 8px;
}}

/* -- the window frame ------------------------------------------------- */

QMenuBar {{
    background: {surface};
    border-bottom: 1px solid {border};
    padding: 2px 6px;
}}
QMenuBar::item {{
    padding: 6px 10px;
    border-radius: 5px;
}}
QMenuBar::item:selected {{ background: {accent_wash}; color: {accent}; }}

QMenu {{
    background: {surface};
    border: 1px solid {border};
    padding: 5px;
}}
QMenu::item {{ padding: 7px 26px 7px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {accent_wash}; color: {accent}; }}
QMenu::separator {{ height: 1px; background: {border}; margin: 5px 8px; }}

QStatusBar {{
    background: {surface};
    border-top: 1px solid {border};
    color: {muted};
}}
QStatusBar::item {{ border: none; }}

/* The stage navigator. Items are tall enough to hit without aiming, and
   the selected one is filled rather than outlined, because an outline on a
   list of eleven reads as a table. */
#nav {{
    background: {window};
    border: none;
    border-right: 1px solid {border};
    outline: none;
    padding: 10px 8px;
}}
#nav::item {{
    padding: 10px 12px;
    margin: 1px 0;
    border-radius: 7px;
    color: {text};
}}
#nav::item:hover {{ background: {sunken}; }}
#nav::item:selected {{ background: {accent_wash}; color: {accent}; }}
#nav::item:disabled {{ color: {faint}; }}

/* The strip above the stage: what this page is, then what it is for. */
#headerBar {{
    background: {surface};
    border-bottom: 1px solid {border};
}}
#headerTitle {{
    font-size: 16pt;
    font-weight: 600;
    color: {text};
}}
#headerSubtitle {{ color: {muted}; }}

#pageScroll, #pageScroll > QWidget > QWidget, #pageContent {{
    background: {window};
}}

/* -- headings and prose ------------------------------------------------ */

#sectionLabel {{ font-weight: 600; color: {text}; }}
#hintLabel {{ color: {muted}; }}
#blockerLabel {{
    background: {warning_wash};
    border: 1px solid #f0dcc0;
    border-left: 3px solid {warning};
    border-radius: 7px;
    padding: 11px 14px;
    color: {warning};
}}

/* -- cards ------------------------------------------------------------- */

/* Every QGroupBox is a card. The ones with no title are pure containers,
   which is why the title styling has to survive an empty string. */
QGroupBox {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 10px;
    margin-top: 8px;
    padding: 16px 16px 14px 16px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 0 5px;
    color: {muted};
    background: transparent;
}}

QFrame[frameShape="4"], QFrame[frameShape="5"] {{   /* HLine, VLine */
    background: {border};
    border: none;
    max-height: 1px;
}}

/* -- controls ---------------------------------------------------------- */

QPushButton {{
    background: {surface};
    border: 1px solid {border_strong};
    border-radius: 7px;
    padding: 8px 15px;
    color: {text};
    min-height: 18px;
}}
QPushButton:hover {{ background: {sunken}; }}
QPushButton:pressed {{ background: {border}; }}
QPushButton:disabled {{
    background: {sunken};
    color: {faint};
    border-color: {border};
}}
QPushButton:focus {{ border-color: {accent}; }}

/* The one action a page wants next. At most one per card: a page where
   everything is emphasised is a page where nothing is. */
QPushButton#primary {{
    background: {accent};
    border: 1px solid {accent};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {accent_hover}; border-color: {accent_hover}; }}
QPushButton#primary:pressed {{ background: {accent_pressed}; }}
QPushButton#primary:disabled {{
    background: #b9c6e4;
    border-color: #b9c6e4;
    color: #f2f5fb;
}}

QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {surface};
    border: 1px solid {border_strong};
    border-radius: 7px;
    padding: 6px 9px;
    selection-background-color: {accent};
    selection-color: #ffffff;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid {accent};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QTextEdit:disabled,
QSpinBox:disabled, QComboBox:disabled {{
    background: {sunken};
    color: {faint};
}}
QPlainTextEdit, QTextEdit {{ padding: 8px 9px; }}

/* Restyling the input box leaves Fusion's spin buttons drawn against the
   old metrics: two slivers overlapping the border. Given a size and a
   background of their own they come back as a pair of arrows. */
QSpinBox, QDoubleSpinBox {{ padding-right: 20px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    width: 18px;
    background: transparent;
    border: none;
}}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; }}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-position: bottom right;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {sunken};
}}

/* The arrows themselves are left to the style to draw. Built out of CSS
   borders, as the usual recipe has it, Qt rendered them as small dark
   squares in the corner of every year field and every dropdown. Fusion's
   own arrows are the right shape, and inherit the palette. */

/* The dropdown button is left alone for the same reason as the spin
   buttons: overriding it stopped the arrow being drawn at all, and a
   combo box with no arrow does not look like a combo box. */
QComboBox QAbstractItemView {{
    background: {surface};
    border: 1px solid {border};
    selection-background-color: {accent_wash};
    selection-color: {accent};
    padding: 4px;
    outline: none;
}}

/* Checkboxes and radio buttons are not styled here AT ALL, deliberately.
   Qt renders a widget through the stylesheet engine as soon as any rule
   matches it, including one that only sets spacing, and that engine has
   no indicator to draw unless one is given: the result was a radio button
   with no circle and a checkbox with no box. Written out as CSS borders
   instead, the checked radio came out square, because a thick border on a
   16px subcontrol loses its corner radius. Fusion draws both correctly and
   takes its colour from the palette, which is set above. */

QProgressBar {{
    background: {sunken};
    border: none;
    border-radius: 5px;
    height: 8px;
    text-align: center;
    color: {muted};
}}
QProgressBar::chunk {{ background: {accent}; border-radius: 5px; }}

/* -- tables ------------------------------------------------------------ */

/* No gridlines. A table of titles and decisions is read along its rows,
   and vertical rules cut every row into pieces. */
QTableWidget, QTableView {{
    background: {surface};
    alternate-background-color: {sunken};
    border: 1px solid {border};
    border-radius: 9px;
    gridline-color: transparent;
    outline: none;
    selection-background-color: {accent_wash};
    selection-color: {text};
}}
QTableWidget::item, QTableView::item {{
    padding: 7px 9px;
    border: none;
}}
QTableWidget::item:selected, QTableView::item:selected {{
    background: {accent_wash};
    color: {text};
}}
QHeaderView {{ background: transparent; }}
QHeaderView::section {{
    background: {surface};
    color: {muted};
    border: none;
    border-bottom: 1px solid {border};
    padding: 9px;
    font-weight: 600;
}}
QHeaderView::section:first {{ border-top-left-radius: 9px; }}
QHeaderView::section:last {{ border-top-right-radius: 9px; }}
QTableCornerButton::section {{ background: {surface}; border: none; }}

QListWidget, QTreeWidget {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 9px;
    outline: none;
    padding: 4px;
}}
QListWidget::item, QTreeWidget::item {{ padding: 6px 8px; border-radius: 5px; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background: {accent_wash};
    color: {accent};
}}

QTabWidget::pane {{
    background: {surface};
    border: 1px solid {border};
    border-radius: 9px;
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    border: none;
    padding: 9px 15px;
    margin-right: 2px;
    color: {muted};
}}
QTabBar::tab:selected {{
    color: {accent};
    border-bottom: 2px solid {accent};
    font-weight: 600;
}}
QTabBar::tab:hover:!selected {{ color: {text}; }}

QSplitter::handle {{ background: {border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

/* -- scrollbars -------------------------------------------------------- */

/* Visible without being loud. These pages are taller than the window, so
   the bar is also the only indication that there is more below. */
QScrollBar:vertical {{
    background: transparent;
    width: 12px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {border_strong};
    border-radius: 4px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {muted}; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 12px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {border_strong};
    border-radius: 4px;
    min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* -- dialogs ----------------------------------------------------------- */

QDialog {{ background: {window}; }}
QDialogButtonBox QPushButton {{ min-width: 84px; }}
""".format(**COLOURS)


def _palette() -> QPalette:
    """A light palette, stated rather than inherited.

    Qt takes the window and text colours from the system theme, so on a
    machine set to dark mode the widgets this stylesheet does not name --
    a message box, a file dialog, the text cursor -- come out dark while
    everything around them is light.
    """
    c = {key: QColor(value) for key, value in COLOURS.items()}
    palette = QPalette()
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Normal,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, c["window"])
        palette.setColor(group, QPalette.ColorRole.WindowText, c["text"])
        palette.setColor(group, QPalette.ColorRole.Base, c["surface"])
        palette.setColor(group, QPalette.ColorRole.AlternateBase, c["sunken"])
        palette.setColor(group, QPalette.ColorRole.Text, c["text"])
        palette.setColor(group, QPalette.ColorRole.PlaceholderText, c["faint"])
        palette.setColor(group, QPalette.ColorRole.Button, c["surface"])
        palette.setColor(group, QPalette.ColorRole.ButtonText, c["text"])
        palette.setColor(group, QPalette.ColorRole.Highlight, c["accent"])
        palette.setColor(group, QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, c["text"])
        palette.setColor(group, QPalette.ColorRole.ToolTipText, c["surface"])
        palette.setColor(group, QPalette.ColorRole.Link, c["accent"])

    disabled = QPalette.ColorGroup.Disabled
    palette.setColor(disabled, QPalette.ColorRole.WindowText, c["faint"])
    palette.setColor(disabled, QPalette.ColorRole.Text, c["faint"])
    palette.setColor(disabled, QPalette.ColorRole.ButtonText, c["faint"])
    palette.setColor(disabled, QPalette.ColorRole.Base, c["sunken"])
    palette.setColor(disabled, QPalette.ColorRole.Button, c["sunken"])
    palette.setColor(disabled, QPalette.ColorRole.Window, c["window"])
    return palette


def _font() -> QFont:
    """The first of these the machine has.

    Named rather than left to Qt's default because the default on Windows
    is still a 9pt face chosen for 96 DPI screens, and these pages are
    mostly prose: titles, abstracts, definitions, quotations.
    """
    for family in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Noto Sans"):
        font = QFont(family, 10)
        if font.exactMatch() or family == "Noto Sans":
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            return font
    font = QFont()
    font.setPointSize(10)
    return font


def apply_theme(app) -> None:
    """Install the palette, the font and the stylesheet on *app*.

    The high-DPI rounding policy is not set here: it has to be set before
    the application object exists, so it lives in the entry point.
    """
    app.setStyle("Fusion")
    app.setPalette(_palette())
    app.setFont(_font())
    app.setStyleSheet(STYLESHEET)


def app_icon():
    """The application's icon, at every size it was drawn for.

    Small sizes are a simplified drawing, not a shrunken large one, so each
    is added separately and Qt picks the closest.
    """
    from pathlib import Path

    from PySide6.QtGui import QIcon

    icon = QIcon()
    folder = Path(__file__).resolve().parent.parent / "resources" / "icons"
    for size in (16, 24, 32, 48, 64, 128, 256):
        path = folder / f"app-icon-{size}.png"
        if path.is_file():
            icon.addFile(str(path))
    return icon
