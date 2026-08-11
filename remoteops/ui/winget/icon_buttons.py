"""Fábricas de botões/ícones reutilizáveis no layout do app."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QPushButton, QSizePolicy, QToolButton

from remoteops.ui.style import ICON_FONT_PT

ICON_SIZE_ROW = 24
ICON_SIZE_TOP = 32

_MDL2_FONT = QFont("Segoe MDL2 Assets", ICON_FONT_PT)


def _field_button_style(sel: str, size: int, radius: int) -> str:
    return f"""
    {sel} {{
        border: 1px solid palette(mid);
        border-radius: {radius}px;
        background: palette(button);
        color: palette(highlight);
        padding: 0px;
        margin: 0px;
        min-width: {size}px;
        max-width: {size}px;
        min-height: {size}px;
        max-height: {size}px;
    }}
    {sel}:hover {{ background: palette(light); border-color: palette(highlight); }}
    {sel}:pressed {{ background: palette(dark); }}
    {sel}:disabled {{ color: palette(mid); background: palette(button); }}
    """


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
    radius = max(2, min(4, size // 6))
    sel = "QPushButton#fieldIconButton" if field_compact else "QPushButton"
    btn.setStyleSheet(_field_button_style(sel, size, radius))
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
    btn.setStyleSheet(
        """
        QPushButton {
            border: 1px solid palette(mid);
            border-radius: 6px;
            background: palette(button);
            color: palette(highlight);
            padding: 0;
            min-width: 40px;
            min-height: 40px;
        }
        QPushButton:hover {
            background: palette(light);
            border-color: palette(highlight);
        }
        QPushButton:pressed { background: palette(dark); }
        QPushButton:disabled { color: palette(mid); background: palette(button); }
        """
    )
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
        """
        QToolButton {
            border: none;
            background: transparent;
            color: palette(highlight);
        }
        QToolButton:hover {
            background: palette(light);
            border-radius: 4px;
        }
        QToolButton:pressed {
            background: palette(dark);
            border-radius: 4px;
        }
        """
    )
    return btn
