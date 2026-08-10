"""Identidade visual e resolução de assets (dev e PyInstaller)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon, QPixmap

from remoteops.core.version import __version__ as APP_VERSION
from remoteops.paths import assets_dir

APP_NAME = "RemoteOps"
APP_DISPLAY_NAME = "RemoteOps — Operações remotas"
ORG_NAME = "RemoteOps"

BRAND_NAVY = "#0F2744"
BRAND_AZURE = "#0063C4"
BRAND_CYAN = "#38BDF8"


def asset_path(name: str):
    return assets_dir() / name


def app_icon() -> QIcon:
    ico = asset_path("icon.ico")
    if ico.is_file():
        return QIcon(str(ico))
    png = asset_path("app_icon.png")
    if png.is_file():
        return QIcon(str(png))
    return QIcon()


def app_mark_pixmap(size: int = 28) -> QPixmap:
    path = asset_path("app_mark.png")
    if not path.is_file():
        return QPixmap()
    pm = QPixmap(str(path))
    if pm.isNull():
        return pm
    return pm.scaled(
        size,
        size,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
