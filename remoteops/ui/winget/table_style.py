from __future__ import annotations

from PyQt6.QtWidgets import QApplication, QHeaderView, QTableWidget

from remoteops.ui.style import (
    COLOR_SURFACE,
    COLOR_TEXT,
    apply_ui_defaults as apply_app_ui_defaults,
    table_frame_qss,
)
from remoteops.ui.widgets.table import enable_header_sorting

CARD_GRID_VERTICAL_SPACING = 2


def _list_table_stylesheet(object_name: str) -> str:
    n = object_name
    return table_frame_qss(f"QTableWidget#{n}") + f"""
QTableWidget#{n} {{
    alternate-background-color: {COLOR_SURFACE};
    selection-background-color: {COLOR_SURFACE};
    selection-color: {COLOR_TEXT};
    outline: none;
}}
QTableWidget#{n}::item {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
}}
QTableWidget#{n}::item:alternate {{
    background: {COLOR_SURFACE};
}}
QTableWidget#{n}::item:hover,
QTableWidget#{n}::item:selected,
QTableWidget#{n}::item:selected:hover,
QTableWidget#{n}::item:focus,
QTableWidget#{n}::item:selected:focus {{
    background: {COLOR_SURFACE};
    color: {COLOR_TEXT};
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
    enable_header_sorting(tbl, skip_columns=(checkbox_col,))


def apply_ui_defaults(app: QApplication) -> None:
    apply_app_ui_defaults(app)
