from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QHeaderView, QTableWidget

# Mantém a mesma base do psexecgui
FONT_UI = "Segoe UI"
FONT_UI_FALLBACK = "Segoe UI, sans-serif"
FONT_MONO = "Consolas"
SIZE_UI = 10
SIZE_UI_SMALL = 9
SIZE_MONO = 9
ICON_FONT_PT = 13

CARD_GRID_VERTICAL_SPACING = 2

# Tabelas das abas: sem zebra, sem realce de hover/seleção na linha (ação = checkbox + botão).


def _list_table_stylesheet(object_name: str) -> str:
    n = object_name
    return f"""
QTableWidget#{n} {{
    background: palette(base);
    alternate-background-color: palette(base);
    selection-background-color: palette(base);
    selection-color: palette(windowText);
    outline: none;
}}
QTableWidget#{n}::item {{
    background: palette(base);
    color: palette(windowText);
}}
QTableWidget#{n}::item:alternate {{
    background: palette(base);
}}
QTableWidget#{n}::item:hover,
QTableWidget#{n}::item:selected,
QTableWidget#{n}::item:selected:hover,
QTableWidget#{n}::item:focus,
QTableWidget#{n}::item:selected:focus {{
    background: palette(base);
    color: palette(windowText);
}}
"""


def apply_flat_list_table_style(tbl: QTableWidget, *, object_name: str) -> None:
    tbl.setObjectName(object_name)
    tbl.setAlternatingRowColors(False)
    base = tbl.styleSheet() or ""
    tbl.setStyleSheet(base + _list_table_stylesheet(object_name))


def apply_interactive_list_headers(
    tbl: QTableWidget,
    *,
    checkbox_col: int = 0,
    checkbox_width: int = 34,
) -> None:
    """Colunas de conteúdo ocupam toda a largura; clique no título ordena A↔Z."""
    header = tbl.horizontalHeader()
    header.setStretchLastSection(False)
    header.setMinimumSectionSize(40)
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(True)
    cols = tbl.columnCount()
    for col in range(cols):
        if col == checkbox_col:
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
        else:
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
    tbl.setColumnWidth(checkbox_col, checkbox_width)
    tbl.setSortingEnabled(True)


def apply_ui_defaults(app: QApplication) -> None:
    font = QFont(FONT_UI, SIZE_UI)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    base = app.styleSheet() or ""
    app.setStyleSheet(
        base
        + """
        QLineEdit {
            font-family: "Segoe UI";
            min-height: 22px;
            border: 1px solid palette(mid);
            border-radius: 4px;
            padding: 4px 8px;
        }
        QLineEdit:focus {
            border-color: palette(highlight);
        }
        QComboBox, QSpinBox, QCheckBox {
            font-family: "Segoe UI";
            min-height: 22px;
        }
        QPlainTextEdit, QTextEdit {
            padding: 2px;
        }
        QPushButton, QToolButton {
            min-height: 22px;
        }
        /* Ícones ao lado de QLineEdit: não herdar altura mínima genérica (evita retângulo largo). */
        QPushButton#fieldIconButton {
            min-width: 0px;
            min-height: 0px;
            padding: 0px;
        }
        """
    )

