from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout, QSizePolicy, QWidgetItem


class FlowLayout(QLayout):
    """
    Layout que organiza widgets em linhas e quebra automaticamente conforme a largura.
    Ideal para grupos grandes de checkboxes (ex: Flags) sem forçar largura mínima gigante.
    """

    def __init__(self, parent=None, margin: int = 0, h_spacing: int = 8, v_spacing: int = 6):
        super().__init__(parent)
        self._items: list[QWidgetItem] = []
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self.setContentsMargins(margin, margin, margin, margin)

    def addItem(self, item):  # type: ignore[override]
        self._items.append(item)

    def count(self) -> int:  # type: ignore[override]
        return len(self._items)

    def itemAt(self, index: int):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int):  # type: ignore[override]
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:  # type: ignore[override]
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return self._do_layout(QRect(0, 0, max(0, width), 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # type: ignore[override]
        """Altura real com quebra de linha na largura atual (não só 1 checkbox)."""
        width = self._hint_width()
        return QSize(width, self.heightForWidth(width))

    def minimumSize(self) -> QSize:  # type: ignore[override]
        """Largura ≥ item mais largo; altura = empilhamento nessa largura."""
        max_item_w = 0
        for item in self._items:
            max_item_w = max(max_item_w, item.sizeHint().width())
        left, top, right, bottom = self.getContentsMargins()
        width = max_item_w + left + right
        return QSize(width, self.heightForWidth(width))

    def _hint_width(self) -> int:
        parent = self.parentWidget()
        if parent is not None and parent.width() > 0:
            return parent.width()
        # Largura de uma única linha (preferida quando ainda não há geometria)
        total = 0
        for i, item in enumerate(self._items):
            total += item.sizeHint().width()
            if i:
                total += self._h_spacing
        left, _t, right, _b = self.getContentsMargins()
        return max(1, total + left + right)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective_rect = rect.adjusted(left, top, -right, -bottom)
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0

        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()

            next_x = x + w + self._h_spacing
            if next_x - self._h_spacing > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + self._v_spacing
                next_x = x + w + self._h_spacing
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), QSize(w, h)))

            x = next_x
            line_height = max(line_height, h)

        if line_height == 0 and not self._items:
            return top + bottom
        return (y + line_height - rect.y()) + bottom
