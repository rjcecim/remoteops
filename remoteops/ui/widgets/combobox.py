"""ComboBox Fluent: chevron MDL2, popup em flyout e item com visto."""

from __future__ import annotations

import weakref

from PyQt6 import sip
from PyQt6.QtCore import QEvent, QObject, QRect, Qt
from PyQt6.QtGui import QColor, QFont, QPainter
from PyQt6.QtWidgets import QComboBox, QFrame, QLabel, QStyle, QStyledItemDelegate

from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    COMBO_CHEVRON_WIDTH,
    FONT_UI,
    ICON_FONT_PT,
    RADIUS_SMALL,
    SIZE_UI,
)

_CHECK_COL = 28
_ITEM_H = 32
_ITEM_INSET = 4
_CHEVRON_DOWN = "\uE70D"
_CHEVRON_UP = "\uE70E"
_CHECK_GLYPH = "\uE73E"


class _FluentComboItemDelegate(QStyledItemDelegate):
    """Itens do flyout: pill de hover, visto na opção atual."""

    def __init__(self, combo: QComboBox):
        super().__init__(combo.view())
        self._combo_ref = weakref.ref(combo)

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(hint.height(), _ITEM_H))
        return hint

    def paint(self, painter, option, index) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        enabled = bool(index.flags() & Qt.ItemFlag.ItemIsEnabled)
        hovered = enabled and bool(
            option.state
            & (
                QStyle.StateFlag.State_MouseOver
                | QStyle.StateFlag.State_Selected
            )
        )
        combo = self._combo_ref()
        is_current = combo is not None and index.row() == combo.currentIndex()

        rect = option.rect.adjusted(_ITEM_INSET, 1, -_ITEM_INSET, -1)
        if is_current:
            fill = QColor(COLOR_ACCENT_SOFT)
        elif hovered:
            fill = QColor(COLOR_HOVER)
        else:
            fill = None
        if fill is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, RADIUS_SMALL, RADIUS_SMALL)
        if is_current and hovered:
            accent = QRect(rect.left() + 4, rect.top() + 8, 3, max(rect.height() - 16, 8))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(COLOR_ACCENT))
            painter.drawRoundedRect(accent, 1.5, 1.5)

        check_rect = QRect(rect.left(), rect.top(), _CHECK_COL, rect.height())
        if is_current:
            painter.setPen(QColor(COLOR_ACCENT if enabled else COLOR_TEXT_MUTED))
            painter.setFont(QFont("Segoe MDL2 Assets", 9))
            painter.drawText(
                check_rect,
                int(Qt.AlignmentFlag.AlignCenter),
                _CHECK_GLYPH,
            )

        text = index.data(Qt.ItemDataRole.DisplayRole)
        text = "" if text is None else str(text)
        text_rect = rect.adjusted(_CHECK_COL, 0, -8, 0)
        painter.setPen(QColor(COLOR_TEXT if enabled else COLOR_TEXT_MUTED))
        painter.setFont(QFont(FONT_UI, SIZE_UI))
        elided = painter.fontMetrics().elidedText(
            text, Qt.TextElideMode.ElideRight, max(0, text_rect.width())
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            elided,
        )
        painter.restore()


class _FluentComboChrome(QObject):
    """Chevron MDL2 sobre o ComboBox (o clique continua no controle)."""

    def __init__(self, combo: QComboBox):
        super().__init__(combo)
        self._combo = combo
        self._open = False
        self._chevron = QLabel(_CHEVRON_DOWN, combo)
        self._chevron.setObjectName("fluentComboChevron")
        self._chevron.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._chevron.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._chevron.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._chevron.setFont(QFont("Segoe MDL2 Assets", ICON_FONT_PT - 2))
        self._sync_chevron()
        combo.installEventFilter(self)
        combo.view().installEventFilter(self)
        self._place()

    def eventFilter(self, obj, event) -> bool:
        combo = self._combo
        if combo is None or sip.isdeleted(combo):
            return False
        et = event.type()
        if obj is combo:
            if et in (
                QEvent.Type.Resize,
                QEvent.Type.Show,
                QEvent.Type.LayoutRequest,
            ):
                self._place()
            elif et in (
                QEvent.Type.Enter,
                QEvent.Type.Leave,
                QEvent.Type.EnabledChange,
                QEvent.Type.FocusIn,
                QEvent.Type.FocusOut,
            ):
                self._sync_chevron()
        else:
            try:
                view = combo.view()
            except RuntimeError:
                return False
            if obj is view:
                if et == QEvent.Type.Show:
                    self._set_open(True)
                elif et == QEvent.Type.Hide:
                    self._set_open(False)
        return False

    def _set_open(self, opened: bool) -> None:
        if self._open == opened:
            return
        self._open = opened
        self._chevron.setText(_CHEVRON_UP if opened else _CHEVRON_DOWN)
        self._sync_chevron()

    def _place(self) -> None:
        combo = self._combo
        h = max(combo.height(), 1)
        self._chevron.setFixedSize(COMBO_CHEVRON_WIDTH, max(h - 2, 16))
        self._chevron.move(
            combo.width() - COMBO_CHEVRON_WIDTH - 1,
            (h - self._chevron.height()) // 2,
        )
        self._chevron.raise_()

    def _sync_chevron(self) -> None:
        combo = self._combo
        if not combo.isEnabled():
            color = COLOR_TEXT_MUTED
        elif self._open or combo.hasFocus() or combo.underMouse():
            color = COLOR_ACCENT
        else:
            color = COLOR_TEXT_SECONDARY
        self._chevron.setStyleSheet(
            f"""
            QLabel#fluentComboChevron {{
                background: transparent;
                border: none;
                color: {color};
                padding: 0;
            }}
            """
        )


def install_fluent_combobox(combo: QComboBox) -> None:
    """Aplica chrome Fluent uma vez por ComboBox."""
    if getattr(combo, "_remoteops_fluent_combo", False):
        return
    combo._remoteops_fluent_combo = True
    combo.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
    combo.setMaxVisibleItems(12)
    view = combo.view()
    view.setFrameShape(QFrame.Shape.NoFrame)
    view.setMouseTracking(True)
    delegate = _FluentComboItemDelegate(combo)
    view.setItemDelegate(delegate)
    chrome = _FluentComboChrome(combo)
    combo._remoteops_fluent_delegate = delegate
    combo._remoteops_fluent_chrome = chrome


class FluentComboBox(QComboBox):
    """QComboBox com QSS Fluent, chevron MDL2 e flyout de itens."""

    def __init__(self, parent=None):
        super().__init__(parent)
        install_fluent_combobox(self)

    def showPopup(self) -> None:
        chrome = getattr(self, "_remoteops_fluent_chrome", None)
        if chrome is not None:
            chrome._set_open(True)
        super().showPopup()

    def hidePopup(self) -> None:
        chrome = getattr(self, "_remoteops_fluent_chrome", None)
        if chrome is not None:
            chrome._set_open(False)
        super().hidePopup()
