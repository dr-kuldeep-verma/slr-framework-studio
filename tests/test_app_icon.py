"""The icon is drawn, bundled and used."""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_the_windows_icon_has_every_size_windows_asks_for():
    from PIL import Image

    ico = Image.open(ROOT / "packaging" / "app.ico")
    assert {(16, 16), (24, 24), (32, 32), (48, 48), (256, 256)} <= set(ico.info["sizes"])


def test_the_build_and_the_installer_point_at_it():
    assert "packaging\" / \"app.ico" in (ROOT / "packaging" / "slr_studio.spec").read_text()
    assert "SetupIconFile=app.ico" in (ROOT / "packaging" / "installer.iss").read_text()


def test_the_window_icon_loads_from_the_package(monkeypatch):
    pytest.importorskip("PySide6")
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from slr_studio.ui.theme import app_icon

    icon = app_icon()
    assert not icon.isNull()
    assert len(icon.availableSizes()) == 7
