from __future__ import annotations

from PyQt6.QtWidgets import QApplication, QTableWidget

from remoteops.ui.style import apply_ui_defaults as apply_app_ui_defaults
from remoteops.ui.widgets.table import configure_standard_table

CARD_GRID_VERTICAL_SPACING = 2


def apply_flat_list_table_style(tbl: QTableWidget, *, object_name: str) -> None:
    """Lista flat WinGet: sem stripe/seleção; checkbox fixo + demais em stretch."""
    cols = tbl.columnCount()
    stretch = tuple(range(1, cols)) if cols > 1 else ()
    configure_standard_table(
        tbl,
        stretch_columns=stretch,
        fixed_columns={0: 34},
        skip_sort_columns=(0,),
        flat=True,
        object_name=object_name,
    )


def apply_interactive_list_headers(
    tbl: QTableWidget,
    *,
    checkbox_col: int = 0,
    checkbox_width: int = 34,
) -> None:
    """Compat: headers já são aplicados por apply_flat_list_table_style."""
    cols = tbl.columnCount()
    stretch = tuple(c for c in range(cols) if c != checkbox_col)
    configure_standard_table(
        tbl,
        stretch_columns=stretch,
        fixed_columns={checkbox_col: checkbox_width},
        skip_sort_columns=(checkbox_col,),
        flat=True,
        object_name=tbl.objectName() or None,
    )


def apply_ui_defaults(app: QApplication) -> None:
    apply_app_ui_defaults(app)
