"""Fábricas de botões/ícones reutilizáveis no layout do app."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QToolButton

from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_HOVER,
    COLOR_PRESSED,
    ICON_FONT_PT,
    RADIUS_MEDIUM,
    RADIUS_SMALL,
    accent_button_qss,
)

ICON_SIZE_ROW = 24
ICON_SIZE_TOP = 32

_MDL2_FONT = QFont("Segoe MDL2 Assets", ICON_FONT_PT)


def _glyph_point_size(size: int) -> int:
    if size <= 28:
        return 11
    if size <= 32:
        return 12
    return ICON_FONT_PT


def icon_button(
    icon_char: str,
    tooltip: str,
    size: int = 32,
    parent=None,
    *,
    field_compact: bool = False,
) -> QPushButton:
    """Botão compacto (ex.: 32x32) para ações de campo (browse/ping)."""
    btn = QPushButton(icon_char, parent)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    if field_compact:
        btn.setObjectName("fieldIconButton")
    btn.setFont(QFont("Segoe MDL2 Assets", _glyph_point_size(size)))
    btn.setFixedSize(size, size)
    btn.setMinimumSize(size, size)
    btn.setMaximumSize(size, size)
    btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    sel = "QPushButton#fieldIconButton" if field_compact else "QPushButton"
    btn.setStyleSheet(accent_button_qss(sel, radius=RADIUS_MEDIUM, size=size))
    return btn


def action_button(icon_char: str, tooltip: str, parent=None) -> QPushButton:
    """Botão quadrado 40x40 para ações em lote no topo das tabelas."""
    btn = QPushButton(icon_char, parent)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    f = QFont(_MDL2_FONT)
    f.setPointSize(12)
    btn.setFont(f)
    btn.setFixedSize(40, 40)
    btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    btn.setStyleSheet(accent_button_qss(radius=RADIUS_MEDIUM, size=40))
    return btn


def cell_icon(icon_char: str, tooltip: str, *, size: int = ICON_SIZE_ROW) -> QToolButton:
    """Ícone puro para célula de tabela (sem cara de botão/card)."""
    btn = QToolButton()
    btn.setText(icon_char)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setAutoRaise(True)
    pt = 11 if size <= 24 else 12
    btn.setFont(QFont("Segoe MDL2 Assets", pt))
    btn.setFixedSize(size, size)
    btn.setStyleSheet(
        f"""
        QToolButton {{
            border: none;
            background: transparent;
            color: {COLOR_ACCENT};
        }}
        QToolButton:hover {{
            background: {COLOR_HOVER};
            border-radius: {RADIUS_SMALL}px;
        }}
        QToolButton:pressed {{
            background: {COLOR_PRESSED};
            border-radius: {RADIUS_SMALL}px;
        }}
        """
    )
    return btn
