"""TabBar com ícones Segoe MDL2, abas arredondadas e indicador animado."""

from __future__ import annotations

from PyQt6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    pyqtProperty,
    pyqtSignal,
)
from PyQt6.QtGui import QColor, QCursor, QFont, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import QTabBar, QToolTip

from remoteops.ui.style import (
    ANIM_TAB,
    COLOR_ACCENT,
    COLOR_BG,
    COLOR_HOVER,
    COLOR_SURFACE,
    COLOR_TEXT,
    COLOR_TEXT_SECONDARY,
    ICON_FONT_PT,
    RADIUS_LARGE,
    anim_ms,
    animations_enabled,
)


class Mdl2TabBar(QTabBar):
    """TabBar que desenha ícone + texto; X de fechar fica colado ao título."""

    tabResetRequested = pyqtSignal(int)

    _PAD_LEFT = 10
    _PAD_GAP = 6
    _CLOSE_GAP = 4
    _PAD_RIGHT = 10
    _CLOSE_CHAR = "\uE711"
    _RESET_CHAR = "\uE777"
    _CLOSE_FONT_PT = 9
    _TAB_H = 32

    def __init__(self, parent=None):
        super().__init__(parent)
        self._icon_font = QFont("Segoe MDL2 Assets", ICON_FONT_PT)
        self._close_font = QFont("Segoe MDL2 Assets", self._CLOSE_FONT_PT)
        self._close_rects: dict[int, QRect] = {}
        self._reset_rects: dict[int, QRect] = {}
        self._pressed_close: int | None = None
        self._pressed_reset: int | None = None
        self._ind_x = 0.0
        self._ind_w = 0.0
        self._ind_anim: QParallelAnimationGroup | None = None
        self._snap_gen = 0
        self.setExpanding(False)
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setUsesScrollButtons(True)
        self.setMovable(False)
        self.setMouseTracking(True)
        self.setDrawBase(False)
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        # O QSS global não deve somar padding: o sizeHint já inclui ícone/X.
        self.setStyleSheet("QTabBar::tab { padding: 0; margin: 0; min-width: 0; border: none; }")
        self.currentChanged.connect(self._on_current_changed)

    def _tab_icon(self, index: int) -> str:
        data = self.tabData(index)
        if isinstance(data, dict):
            return str(data.get("icon") or "")
        return data if isinstance(data, str) else ""

    def _tab_closable(self, index: int) -> bool:
        data = self.tabData(index)
        return isinstance(data, dict) and bool(data.get("closable"))

    def _tab_resettable(self, index: int) -> bool:
        data = self.tabData(index)
        return isinstance(data, dict) and bool(data.get("resettable"))

    def set_tab_meta(
        self,
        index: int,
        icon: str,
        *,
        closable: bool = False,
        resettable: bool = False,
    ) -> None:
        """Define ícone e ações no título (reset e/ou X de fechar)."""
        if closable or resettable:
            self.setTabData(
                index,
                {
                    "icon": icon or "",
                    "closable": bool(closable),
                    "resettable": bool(resettable),
                },
            )
        else:
            self.setTabData(index, icon or "")
        # setTabData não relayouta: sem isto o indicador usa a largura só do texto.
        self.setTabText(index, self.tabText(index))
        if index == self.currentIndex():
            self._schedule_snap()

    def refresh_layout(self) -> None:
        """Força layoutTabs() e realinha o indicador à largura real das abas."""
        for i in range(self.count()):
            self.setTabText(i, self.tabText(i))
        self.updateGeometry()
        self._schedule_snap()

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
        extra_w = 0
        if self._tab_resettable(index):
            extra_w += self._CLOSE_GAP + self._close_glyph_width()
        if self._tab_closable(index):
            extra_w += self._CLOSE_GAP + self._close_glyph_width()

        width = (
            self._PAD_LEFT
            + icon_w
            + (self._PAD_GAP if icon_w else 0)
            + text_w
            + extra_w
            + self._PAD_RIGHT
        )
        # Não somar PM_TabBarTabHSpace: o Fusion infla cada aba e a janela cresce.
        return max(64, width)

    def tabSizeHint(self, index):
        return QSize(self._tab_content_width(index), self._TAB_H)

    def minimumTabSizeHint(self, index):
        return self.tabSizeHint(index)

    def _close_hit_index(self, pos) -> int | None:
        return self._hit_index(self._close_rects, pos)

    def _reset_hit_index(self, pos) -> int | None:
        return self._hit_index(self._reset_rects, pos)

    @staticmethod
    def _hit_index(rects: dict[int, QRect], pos) -> int | None:
        point = pos.toPoint() if hasattr(pos, "toPoint") else pos
        for i, rect in rects.items():
            if rect.contains(point):
                return i
        return None

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            reset_hit = self._reset_hit_index(event.position())
            if reset_hit is not None:
                self._pressed_reset = reset_hit
                self._pressed_close = None
                return
            close_hit = self._close_hit_index(event.position())
            if close_hit is not None:
                self._pressed_close = close_hit
                self._pressed_reset = None
                return
        self._pressed_close = None
        self._pressed_reset = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_reset is not None:
            hit = self._reset_hit_index(event.position())
            idx = self._pressed_reset
            self._pressed_reset = None
            if hit == idx:
                self.tabResetRequested.emit(idx)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_close is not None:
            hit = self._close_hit_index(event.position())
            idx = self._pressed_close
            self._pressed_close = None
            if hit == idx:
                self.tabCloseRequested.emit(idx)
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        reset_hit = self._reset_hit_index(event.position())
        close_hit = self._close_hit_index(event.position())
        self.setCursor(
            QCursor(Qt.CursorShape.PointingHandCursor)
            if reset_hit is not None or close_hit is not None
            else QCursor(Qt.CursorShape.ArrowCursor)
        )
        if reset_hit is not None:
            QToolTip.showText(
                event.globalPosition().toPoint(),
                self.tr("Limpar tudo e voltar ao estado inicial"),
                self,
            )
        else:
            QToolTip.hideText()
        self.update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self._schedule_snap()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ind_anim is not None:
            self._ind_anim.stop()
            self._ind_anim = None
        self._snap_indicator()

    def _indicator_geom(self, index: int) -> tuple[float, float]:
        if index < 0 or index >= self.count():
            return 0.0, 0.0
        rect = self.tabRect(index)
        # Mesma inset do fundo selecionado — cobre o rótulo inteiro (ícone+texto+X).
        inset = 2
        return float(rect.x() + inset), float(max(12, rect.width() - inset * 2))

    def _snap_indicator(self) -> None:
        x, w = self._indicator_geom(self.currentIndex())
        self._ind_x = x
        self._ind_w = w
        self.update()

    def _schedule_snap(self) -> None:
        self._snap_gen += 1
        gen = self._snap_gen
        QTimer.singleShot(0, lambda: self._snap_after_layout(gen))

    def _snap_after_layout(self, gen: int) -> None:
        if gen != self._snap_gen:
            return
        if self._ind_anim is not None:
            self._ind_anim.stop()
            self._ind_anim = None
        self._snap_indicator()

    def tabInserted(self, index):  # noqa: N802
        super().tabInserted(index)
        self._schedule_snap()

    def tabRemoved(self, index):  # noqa: N802
        super().tabRemoved(index)
        self._schedule_snap()

    def _on_current_changed(self, index: int) -> None:
        x, w = self._indicator_geom(index)
        ms = anim_ms(ANIM_TAB)
        hint_w = float(self.tabSizeHint(index).width()) if 0 <= index < self.count() else 0.0
        # tabRect ainda sem ícone/X (setTabData sem relayout) → linha curta.
        rect_short = hint_w > 0 and (w + 4) < hint_w * 0.9
        if ms <= 0 or not self.isVisible() or self._ind_w <= 1 or rect_short:
            self._ind_x = x
            self._ind_w = w
            self.update()
            self._schedule_snap()
            return
        if self._ind_anim is not None:
            self._ind_anim.stop()
            self._ind_anim = None
        ax = QPropertyAnimation(self, b"indicatorX")
        ax.setDuration(ms)
        ax.setStartValue(self._ind_x)
        ax.setEndValue(x)
        ax.setEasingCurve(QEasingCurve.Type.OutCubic)
        aw = QPropertyAnimation(self, b"indicatorW")
        aw.setDuration(ms)
        aw.setStartValue(self._ind_w)
        aw.setEndValue(w)
        aw.setEasingCurve(QEasingCurve.Type.OutCubic)
        group = QParallelAnimationGroup(self)
        group.addAnimation(ax)
        group.addAnimation(aw)
        group.finished.connect(self._on_indicator_anim_finished)
        self._ind_anim = group
        group.start(QAbstractAnimation.DeletionPolicy.KeepWhenStopped)

    def _on_indicator_anim_finished(self) -> None:
        self._ind_anim = None
        self._snap_indicator()

    def _get_ind_x(self) -> float:
        return self._ind_x

    def _set_ind_x(self, value: float) -> None:
        self._ind_x = float(value)
        self.update()

    def _get_ind_w(self) -> float:
        return self._ind_w

    def _set_ind_w(self, value: float) -> None:
        self._ind_w = float(value)
        self.update()

    indicatorX = pyqtProperty(float, _get_ind_x, _set_ind_x)
    indicatorW = pyqtProperty(float, _get_ind_w, _set_ind_w)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setClipRect(event.rect() if event is not None else self.rect())
        painter.fillRect(self.rect(), QColor(COLOR_BG))

        current = self.currentIndex()
        hover = -1
        if self.underMouse():
            hover = self.tabAt(self.mapFromGlobal(QCursor.pos()))
        tab_font = QFont(self.font())
        tab_font_bold = QFont(tab_font)
        tab_font_bold.setBold(True)
        self._close_rects = {}
        self._reset_rects = {}

        for i in range(self.count()):
            rect = self.tabRect(i).adjusted(2, 3, -2, 1)
            is_selected = i == current
            is_hover = i == hover and not is_selected

            if is_selected:
                painter.setPen(QPen(QColor(COLOR_SURFACE), 0))
                painter.setBrush(QColor(COLOR_SURFACE))
                painter.drawRoundedRect(rect, RADIUS_LARGE, RADIUS_LARGE)
            elif is_hover:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(COLOR_HOVER))
                painter.drawRoundedRect(rect, RADIUS_LARGE, RADIUS_LARGE)

            icon_char = self._tab_icon(i)
            text = self.tabText(i) or ""
            resettable = self._tab_resettable(i)
            closable = self._tab_closable(i)
            text_font = tab_font_bold if is_selected else tab_font
            x = rect.left() + self._PAD_LEFT - 2
            mid_y = rect.center().y()

            if icon_char:
                painter.setFont(self._icon_font)
                painter.setPen(QColor(COLOR_ACCENT))
                icon_w = painter.fontMetrics().horizontalAdvance(icon_char)
                icon_h = painter.fontMetrics().height()
                painter.drawText(
                    QRect(x, mid_y - icon_h // 2, icon_w, icon_h),
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    icon_char,
                )
                x += icon_w + self._PAD_GAP

            painter.setFont(text_font)
            painter.setPen(QColor(COLOR_TEXT if is_selected else COLOR_TEXT_SECONDARY))
            text_w = painter.fontMetrics().horizontalAdvance(text)
            text_h = painter.fontMetrics().height()
            painter.drawText(
                QRect(x, mid_y - text_h // 2, text_w, text_h),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                text,
            )
            x += text_w

            if resettable:
                x += self._CLOSE_GAP
                painter.setFont(self._close_font)
                painter.setPen(QColor(COLOR_ACCENT))
                reset_w = painter.fontMetrics().horizontalAdvance(self._RESET_CHAR)
                reset_h = painter.fontMetrics().height()
                reset_rect = QRect(x, mid_y - reset_h // 2, reset_w, reset_h)
                painter.drawText(
                    reset_rect,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    self._RESET_CHAR,
                )
                self._reset_rects[i] = reset_rect.adjusted(-2, -2, 4, 2)
                x += reset_w

            if closable:
                x += self._CLOSE_GAP
                painter.setFont(self._close_font)
                painter.setPen(QColor(COLOR_ACCENT))
                close_w = painter.fontMetrics().horizontalAdvance(self._CLOSE_CHAR)
                close_h = painter.fontMetrics().height()
                close_rect = QRect(x, mid_y - close_h // 2, close_w, close_h)
                painter.drawText(
                    close_rect,
                    Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                    self._CLOSE_CHAR,
                )
                self._close_rects[i] = close_rect.adjusted(-2, -2, 4, 2)

        if self.count() > 0 and self._ind_w > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLOR_ACCENT))
            y = self.height() - 3
            painter.drawRoundedRect(int(self._ind_x), y, int(self._ind_w), 2, 1, 1)
