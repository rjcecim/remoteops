"""TabBar com ícones Segoe MDL2 e botão fechar colado ao título."""

from __future__ import annotations

from PyQt6.QtCore import QRect, QSize, Qt
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPalette
from PyQt6.QtWidgets import QStyle, QStyleOptionTab, QTabBar

from remoteops.ui.style import ICON_FONT_PT


class Mdl2TabBar(QTabBar):
    """TabBar que desenha ícone + texto; X de fechar fica colado ao título."""

    _PAD_LEFT = 8
    _PAD_GAP = 4
    _CLOSE_GAP = 2
    _PAD_RIGHT = 8
    _CLOSE_CHAR = "\uE711"
    _CLOSE_FONT_PT = 9

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon_font = QFont("Segoe MDL2 Assets", ICON_FONT_PT)
        self._close_font = QFont("Segoe MDL2 Assets", self._CLOSE_FONT_PT)
        self._close_rects: dict[int, QRect] = {}
        self._pressed_close: int | None = None
        self.setExpanding(False)
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setUsesScrollButtons(True)
        self.setMovable(False)
        self.setMouseTracking(True)

    def _tab_icon(self, index: int) -> str:
        data = self.tabData(index)
        if isinstance(data, dict):
            return str(data.get("icon") or "")
        return data if isinstance(data, str) else ""

    def _tab_closable(self, index: int) -> bool:
        data = self.tabData(index)
        return isinstance(data, dict) and bool(data.get("closable"))

    def set_tab_meta(self, index: int, icon: str, *, closable: bool = False) -> None:
        """Define ícone e se a aba tem X ao lado do título."""
        if closable:
            self.setTabData(index, {"icon": icon or "", "closable": True})
        else:
            self.setTabData(index, icon or "")

    def _close_glyph_width(self) -> int:
        return QFontMetrics(self._close_font).horizontalAdvance(self._CLOSE_CHAR)

    def _tab_content_width(self, index: int) -> int:
        icon = self._tab_icon(index)
        text = self.tabText(index) or ""

        text_font = QFont(self.font())
        text_font_bold = QFont(text_font)
        text_font_bold.setBold(True)
        text_w = max(
            QFontMetrics(text_font).horizontalAdvance(text),
            QFontMetrics(text_font_bold).horizontalAdvance(text),
        )

        icon_w = QFontMetrics(self._icon_font).horizontalAdvance(icon) if icon else 0
        close_w = 0
        if self._tab_closable(index):
            close_w = self._CLOSE_GAP + self._close_glyph_width()

        width = (
            self._PAD_LEFT
            + icon_w
            + (self._PAD_GAP if icon_w else 0)
            + text_w
            + close_w
            + self._PAD_RIGHT
        )
        width += self.style().pixelMetric(
            QStyle.PixelMetric.PM_TabBarTabHSpace, None, self
        )
        return max(60, width)

    def tabSizeHint(self, index):
        return QSize(self._tab_content_width(index), super().tabSizeHint(index).height())

    def minimumTabSizeHint(self, index):
        return self.tabSizeHint(index)

    def _close_hit_index(self, pos) -> int | None:
        point = pos.toPoint() if hasattr(pos, "toPoint") else pos
        for i, rect in self._close_rects.items():
            if rect.contains(point):
                return i
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            hit = self._close_hit_index(event.position())
            if hit is not None:
                self._pressed_close = hit
                return
        self._pressed_close = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_close is not None:
            hit = self._close_hit_index(event.position())
            idx = self._pressed_close
            self._pressed_close = None
            if hit == idx:
                self.tabCloseRequested.emit(idx)
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        hit = self._close_hit_index(event.position())
        self.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor)
            if hit is not None
            else QCursor(Qt.CursorShape.ArrowCursor)
        )
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        current = self.currentIndex()
        tab_font = self.font()
        tab_font_bold = QFont(tab_font)
        tab_font_bold.setBold(True)
        highlight = self.palette().color(QPalette.ColorRole.Highlight)
        text_color = self.palette().color(QPalette.ColorRole.WindowText)
        self._close_rects = {}

        for i in range(self.count()):
            opt = QStyleOptionTab()
            self.initStyleOption(opt, i)
            rect = self.tabRect(i)
            icon_char = self._tab_icon(i)
            text = self.tabText(i) or ""
            closable = self._tab_closable(i)
            opt.text = ""
            self.style().drawControl(QStyle.ControlElement.CE_TabBarTab, opt, painter, self)

            is_selected = i == current
            text_font = tab_font_bold if is_selected else tab_font
            x = rect.left() + self._PAD_LEFT
            mid_y = rect.center().y()

            if icon_char:
                painter.setFont(self._icon_font)
                painter.setPen(highlight)
                icon_w = painter.fontMetrics().horizontalAdvance(icon_char)
                icon_h = painter.fontMetrics().height()
                painter.drawText(
                    QRect(x, mid_y - icon_h // 2, icon_w, icon_h),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    icon_char,
                )
                x += icon_w + self._PAD_GAP

            painter.setFont(text_font)
            painter.setPen(text_color)
            text_w = painter.fontMetrics().horizontalAdvance(text)
            text_h = painter.fontMetrics().height()
            painter.drawText(
                QRect(x, mid_y - text_h // 2, text_w, text_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            x += text_w

            if closable:
                x += self._CLOSE_GAP
                painter.setFont(self._close_font)
                painter.setPen(highlight)
                close_w = painter.fontMetrics().horizontalAdvance(self._CLOSE_CHAR)
                close_h = painter.fontMetrics().height()
                close_rect = QRect(x, mid_y - close_h // 2, close_w, close_h)
                painter.drawText(
                    close_rect,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    self._CLOSE_CHAR,
                )
                self._close_rects[i] = close_rect.adjusted(-2, -2, 4, 2)
