"""Tooltip Fluent (card claro) — substitui o QTipLabel nativo no Windows."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QHelpEvent
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QTableView,
    QToolTip,
    QWidget,
)

from remoteops.ui.style import (
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_TEXT,
    FONT_UI,
    RADIUS_MEDIUM,
    SIZE_UI_SMALL,
)

_MAX_WRAP_WIDTH = 360
_SHADOW_MARGIN = 14


class _FluentTipPopup(QWidget):
    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setObjectName("FluentTipPopup")

        self._label = QLabel(self)
        self._label.setObjectName("FluentTipLabel")
        self._label.setTextFormat(Qt.TextFormat.PlainText)
        self._label.setWordWrap(True)
        self._label.setStyleSheet(
            f"""
            QLabel#FluentTipLabel {{
                background-color: {COLOR_SURFACE};
                color: {COLOR_TEXT};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MEDIUM}px;
                padding: 8px 12px;
                font-family: "{FONT_UI}";
                font-size: {SIZE_UI_SMALL}pt;
                font-weight: 400;
            }}
            """
        )
        shadow = QGraphicsDropShadowEffect(self._label)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 42))
        self._label.setGraphicsEffect(shadow)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(
            _SHADOW_MARGIN, _SHADOW_MARGIN, _SHADOW_MARGIN, _SHADOW_MARGIN
        )
        lay.addWidget(self._label)

        self._anchor: QObject | None = None

    def show_text(
        self,
        global_pos: QPoint,
        text: str,
        *,
        wrap: bool = True,
        anchor: QObject | None = None,
    ) -> None:
        text = " ".join((text or "").split())
        if not text:
            self.hide_text()
            return
        QToolTip.hideText()
        self._anchor = anchor
        self._label.setWordWrap(bool(wrap))
        if wrap:
            self._label.setMaximumWidth(_MAX_WRAP_WIDTH)
        else:
            self._label.setMaximumWidth(16777215)
        self._label.setText(text)
        self.adjustSize()
        self.move(_clamped_pos(global_pos, self.size().width(), self.size().height()))
        self.show()
        self.raise_()

    def hide_text(self) -> None:
        self._anchor = None
        self.hide()


def _clamped_pos(global_pos: QPoint, width: int, height: int) -> QPoint:
    screen = QGuiApplication.screenAt(global_pos) or QGuiApplication.primaryScreen()
    geo = screen.availableGeometry() if screen else None
    x = global_pos.x() + 12
    y = global_pos.y() + 18
    if geo is None:
        return QPoint(x, y)
    if x + width > geo.right():
        x = max(geo.left(), geo.right() - width)
    if y + height > geo.bottom():
        y = max(geo.top(), global_pos.y() - height - 8)
    if x < geo.left():
        x = geo.left()
    if y < geo.top():
        y = geo.top()
    return QPoint(x, y)


_popup: _FluentTipPopup | None = None


def _tip() -> _FluentTipPopup:
    global _popup
    if _popup is None:
        _popup = _FluentTipPopup()
    return _popup


def show_fluent_tooltip(
    global_pos: QPoint,
    text: str,
    *,
    wrap: bool = True,
    anchor: QObject | None = None,
) -> None:
    _tip().show_text(global_pos, text, wrap=wrap, anchor=anchor)


def hide_fluent_tooltip() -> None:
    if _popup is not None:
        _popup.hide_text()


class _FluentToolTipFilter(QObject):
    """Troca o QTipLabel nativo pelo card Fluent para widgets com setToolTip()."""

    def eventFilter(self, obj, event) -> bool:  # noqa: ANN001
        if event.type() == QEvent.Type.Leave:
            popup = _popup
            if popup is not None and obj is popup._anchor:
                popup.hide_text()
            return False

        if event.type() != QEvent.Type.ToolTip:
            return False
        if not isinstance(event, QHelpEvent):
            return False
        if not isinstance(obj, QWidget):
            return False
        if obj.inherits("QTipLabel") or obj.objectName() == "FluentTipPopup":
            return True

        parent = obj.parent()
        if isinstance(parent, QTableView) and obj is parent.viewport():
            return False

        text = obj.toolTip() if hasattr(obj, "toolTip") else ""
        if not str(text or "").strip():
            return False

        show_fluent_tooltip(event.globalPos(), str(text), wrap=True, anchor=obj)
        return True


def install_fluent_tooltips(app: QApplication) -> None:
    existing = getattr(app, "_remoteops_fluent_tooltip", None)
    if existing is not None:
        return
    filt = _FluentToolTipFilter(app)
    app.installEventFilter(filt)
    app._remoteops_fluent_tooltip = filt
