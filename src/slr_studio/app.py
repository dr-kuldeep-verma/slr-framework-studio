"""The entry point: one executable that does both jobs.

Run with no arguments it opens the window, which is how a reviewer uses it.
Run with a command it runs the pipeline and exits, which is how it is
scripted, run on a machine with no display, and smoke-tested after a build.

Both have to be the same executable because the packaged build is the only
thing a Windows user has: there is no `python -m` on a machine that never
installed Python.

Qt is imported only on the path that needs it, so the pipeline still runs
where there is no display and no Qt.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys.argv)

    # Absolute imports, not relative ones. PyInstaller does not import this
    # module from its package -- it runs the script, so `__package__` is
    # empty and `from . import ...` raises ImportError. The packaged build
    # then shows a crash dialog instead of a window, which is what the first
    # one did.
    from slr_studio import __app_name__, __version__

    if "--version" in arguments:
        print(f"{__app_name__} {__version__}")
        return 0

    # Anything after the program name is a pipeline command. Qt is not
    # imported on this path, so it works with no display.
    if len(arguments) > 1:
        from slr_studio.cli import main as cli_main

        return int(cli_main(arguments[1:]))

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - only hit in a broken install
        print(
            "The desktop interface needs PySide6. Install it with:\n"
            "    pip install PySide6\n"
            "The packaged Windows build bundles it already.",
            file=sys.stderr,
        )
        return 1

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    from slr_studio.ui.main_window import MainWindow
    from slr_studio.ui.theme import app_icon, apply_theme

    if sys.platform == "win32":  # pragma: no cover - Windows only
        # Without this, a copy run from Python shows Python's icon in the
        # taskbar instead of the application's.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "SLRFrameworkStudio.Desktop"
            )
        except Exception:
            pass

    # Before the application object, or it has no effect. Windows laptops
    # are commonly set to 125% or 150%, and with the default rounding Qt
    # scales to 100% or 200% instead: at 125% that is a window of 10pt text
    # on a 1920x1080 panel the user has told Windows is too small to read.
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    from slr_studio.errorlog import install_excepthook

    install_excepthook()
    app = QApplication(arguments)
    app.setApplicationName(__app_name__)
    app.setOrganizationName("SLR Framework Studio")
    apply_theme(app)
    app.setWindowIcon(app_icon())
    # Long explanations in pop-ups scroll instead of running off the screen.
    from slr_studio.ui.scrolling import install_scrolling_messages

    install_scrolling_messages()

    window = MainWindow()
    window.show()
    return int(app.exec())


if __name__ == "__main__":
    raise SystemExit(main())
