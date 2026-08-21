"""Comportamento compartilhado de QTableWidget / QTableView."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtGui import QHelpEvent
from PyQt6.QtWidgets import (
    QApplication,
    QHeaderView,
    QTableView,
    QTableWidget,
    QToolTip,
    QWidget,
)


class _CopyCellOnDoubleClickFilter(QObject):
    """Duplo clique em uma célula copia o texto visível para a área de transferência."""

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() != QEvent.Type.MouseButtonDblClick:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False

        table = obj.parent() if not isinstance(obj, QTableView) else obj
        if not isinstance(table, QTableView) or obj is not table.viewport():
            return False

        pos = event.position().toPoint()
        index = table.indexAt(pos)
        if not index.isValid():
            return False
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if text is None:
            return False
        text = str(text).strip()
        if not text:
            return False
        QApplication.clipboard().setText(text)
        return False


def install_copy_cell_on_double_click(app: QApplication) -> None:
    """Ativa a cópia por duplo clique em todas as tabelas do aplicativo."""
    existing = getattr(app, "_remoteops_copy_cell_filter", None)
    if existing is not None:
        return
    filt = _CopyCellOnDoubleClickFilter(app)
    app.installEventFilter(filt)
    app._remoteops_copy_cell_filter = filt


def _cell_widget(table: QTableView, index) -> QWidget | None:  # noqa: ANN001
    if isinstance(table, QTableWidget):
        widget = table.cellWidget(index.row(), index.column())
        if widget is not None:
            return widget
    return table.indexWidget(index)


class _CellToolTipFilter(QObject):
    """Tooltip de uma linha com o DisplayRole; não cobre widgets da célula."""

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() != QEvent.Type.ToolTip:
            return False
        if not isinstance(event, QHelpEvent):
            return False

        table = obj.parent()
        if not isinstance(table, QTableView) or obj is not table.viewport():
            return False

        pos = event.pos()
        if obj.childAt(pos) is not None:
            return False

        index = table.indexAt(pos)
        if not index.isValid():
            QToolTip.hideText()
            return True
        if _cell_widget(table, index) is not None:
            return False

        raw = index.data(Qt.ItemDataRole.DisplayRole)
        if raw is None:
            QToolTip.hideText()
            return True
        text = " ".join(str(raw).split())
        if not text:
            QToolTip.hideText()
            return True

        QToolTip.showText(event.globalPos(), text, obj, table.visualRect(index))
        return True


def apply_table_cell_behavior(table: QTableView) -> None:
    """Uma linha, ElideRight e tooltip do DisplayRole no viewport()."""
    if table.property("_remoteops_cell_behavior"):
        return
    table.setWordWrap(False)
    table.setTextElideMode(Qt.TextElideMode.ElideRight)
    filt = _CellToolTipFilter(table)
    table.viewport().installEventFilter(filt)
    table.setProperty("_remoteops_cell_behavior", True)
    table._remoteops_cell_tooltip_filter = filt


class _InstallTableCellBehaviorFilter(QObject):
    """Garante o comportamento de célula em tabelas atuais e futuras."""

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() == QEvent.Type.ChildAdded:
            child = event.child() if hasattr(event, "child") else None
            if isinstance(child, QTableView):
                apply_table_cell_behavior(child)
            return False
        if isinstance(obj, QTableView) and event.type() in (
            QEvent.Type.Polish,
            QEvent.Type.Show,
        ):
            apply_table_cell_behavior(obj)
            return False
        parent = obj.parent() if isinstance(obj, QWidget) else None
        if (
            isinstance(parent, QTableView)
            and obj is parent.viewport()
            and not parent.property("_remoteops_cell_behavior")
        ):
            apply_table_cell_behavior(parent)
        return False


def install_table_cell_behavior(app: QApplication) -> None:
    """Ativa linha única + tooltip global em todas as QTableView/QTableWidget."""
    existing = getattr(app, "_remoteops_table_cell_behavior", None)
    if existing is not None:
        return
    filt = _InstallTableCellBehaviorFilter(app)
    app.installEventFilter(filt)
    app._remoteops_table_cell_behavior = filt


class _SkipHeaderSortFilter(QObject):
    """Ignora clique no título de colunas que não devem ordenar (ações, checkbox)."""

    def __init__(self, skip_columns: Sequence[int], parent: QHeaderView):
        super().__init__(parent)
        self._skip = set(skip_columns)

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if not isinstance(obj, QHeaderView):
            return False
        col = obj.logicalIndexAt(event.position().toPoint())
        return col in self._skip


def enable_header_sorting(
    table: QTableWidget,
    *,
    skip_columns: Sequence[int] = (),
) -> None:
    """Clique no título da coluna ordena A↔Z / Z↔A."""
    apply_table_cell_behavior(table)
    header = table.horizontalHeader()
    header.setSectionsClickable(True)
    header.setSortIndicatorShown(True)
    table.setSortingEnabled(True)
    skip = tuple(int(c) for c in skip_columns)
    if not skip or header.property("_remoteops_skip_sort"):
        return
    filt = _SkipHeaderSortFilter(skip, header)
    header.installEventFilter(filt)
    header.setProperty("_remoteops_skip_sort", True)
    header._remoteops_skip_sort_filter = filt


@contextmanager
def pause_table_sorting(table: QTableWidget) -> Iterator[None]:
    """Desliga a ordenação durante inserção/atualização e reaplica o critério atual."""
    enabled = table.isSortingEnabled()
    table.setSortingEnabled(False)
    try:
        yield
    finally:
        table.setSortingEnabled(enabled)
