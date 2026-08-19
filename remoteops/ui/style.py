from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PyQt6.QtWidgets import (
    QApplication,
    QProxyStyle,
    QPushButton,
    QStyle,
    QStyleFactory,
)

# ── Tipografia ──────────────────────────────────────────────────────────────
FONT_UI = "Segoe UI"
FONT_UI_FALLBACK = "Segoe UI, sans-serif"
FONT_MONO = "Consolas"
SIZE_UI = 10
SIZE_UI_SMALL = 9
SIZE_MONO = 9
ICON_FONT_PT = 13

# ── Densidade (preservar compactação atual) ─────────────────────────────────
CARD_GRID_VERTICAL_SPACING = 4
INPUT_HEIGHT = 32
HEADER_HEIGHT = 24
HEADER_BTN_SIZE = 22

# ── Raios ───────────────────────────────────────────────────────────────────
RADIUS_SMALL = 6
RADIUS_MEDIUM = 8
RADIUS_LARGE = 10
RADIUS_CARD = 12

# ── Espaçamento ─────────────────────────────────────────────────────────────
SPACE_XS = 4
SPACE_SM = 6
SPACE_MD = 8
SPACE_LG = 10
SPACE_XL = 12

# ── Paleta clara (Fluent / desktop profissional) ────────────────────────────
COLOR_BG = "#F3F3F3"
COLOR_SURFACE = "#FFFFFF"
COLOR_SURFACE_MUTED = "#F7F8FA"
COLOR_BORDER = "#E1E4E8"
COLOR_BORDER_HOVER = "#C9CED4"
COLOR_HOVER = "#F0F4F8"
COLOR_PRESSED = "#E8EEF5"
COLOR_TEXT = "#1B1B1B"
COLOR_TEXT_SECONDARY = "#5C6166"
COLOR_TEXT_MUTED = "#8A8F94"
COLOR_ACCENT = "#0063C4"
COLOR_ACCENT_SOFT = "#E6F1FB"
COLOR_FOCUS = "#0063C4"
COLOR_SCROLL = "#C5C9CE"
COLOR_SCROLL_HOVER = "#A8ADB3"

# ── Animações (desligáveis em um ponto) ─────────────────────────────────────
ANIMATIONS_ENABLED = True
ANIM_HOVER = 120
ANIM_PRESS = 100
ANIM_TAB = 160
ANIM_PAGE = 140
ANIM_CARD = 200


def animations_enabled() -> bool:
    return bool(ANIMATIONS_ENABLED)


def anim_ms(duration: int) -> int:
    """Duração efetiva; 0 quando as animações estão desligadas."""
    return int(duration) if animations_enabled() else 0


class _FluentStyle(QProxyStyle):
    """Fusion + checkbox arredondado (Fluent), sem perder o visto."""

    _BOX = 18
    _RADIUS = 5.0

    def __init__(self):
        super().__init__(QStyleFactory.create("Fusion"))

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802
        if metric in (
            QStyle.PixelMetric.PM_IndicatorWidth,
            QStyle.PixelMetric.PM_IndicatorHeight,
        ):
            return self._BOX
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element == QStyle.PrimitiveElement.PE_IndicatorCheckBox:
            _draw_rounded_checkbox(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)


def _draw_rounded_checkbox(option, painter: QPainter) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = option.rect.adjusted(1, 1, -1, -1)
    radius = _FluentStyle._RADIUS
    state = option.state
    checked = bool(state & QStyle.StateFlag.State_On)
    partial = bool(state & QStyle.StateFlag.State_NoChange)
    disabled = not bool(state & QStyle.StateFlag.State_Enabled)
    hover = bool(state & QStyle.StateFlag.State_MouseOver)
    sunken = bool(state & QStyle.StateFlag.State_Sunken)

    if checked or partial:
        fill = QColor(COLOR_ACCENT)
        if disabled:
            fill = QColor(COLOR_TEXT_MUTED)
        elif sunken:
            fill = fill.darker(108)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)

        mark = QPen(QColor(COLOR_SURFACE), 2.0)
        mark.setCapStyle(Qt.PenCapStyle.RoundCap)
        mark.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(mark)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        if partial:
            painter.drawLine(
                int(x + w * 0.22), int(y + h * 0.50),
                int(x + w * 0.78), int(y + h * 0.50),
            )
        else:
            path = QPainterPath()
            path.moveTo(x + w * 0.22, y + h * 0.52)
            path.lineTo(x + w * 0.42, y + h * 0.72)
            path.lineTo(x + w * 0.78, y + h * 0.30)
            painter.drawPath(path)
    else:
        fill = QColor(COLOR_SURFACE)
        border = QColor(COLOR_BORDER_HOVER)
        if hover and not disabled:
            border = QColor(COLOR_ACCENT)
            fill = QColor(COLOR_HOVER)
        if disabled:
            fill = QColor(COLOR_SURFACE_MUTED)
            border = QColor(COLOR_BORDER)
        if sunken:
            fill = QColor(COLOR_PRESSED)
        painter.setPen(QPen(border, 1.25))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, radius, radius)
    painter.restore()


def apply_light_palette(app: QApplication) -> None:
    """Força paleta clara (não segue o tema escuro do Windows)."""
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(COLOR_BG))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.Base, QColor(COLOR_SURFACE))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(COLOR_SURFACE_MUTED))
    pal.setColor(QPalette.ColorRole.Text, QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.Button, QColor(COLOR_SURFACE))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(COLOR_ACCENT))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor(COLOR_SURFACE))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(COLOR_TEXT_MUTED))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(COLOR_SURFACE))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(COLOR_TEXT))
    pal.setColor(QPalette.ColorRole.Light, QColor(COLOR_HOVER))
    pal.setColor(QPalette.ColorRole.Midlight, QColor(COLOR_BORDER))
    pal.setColor(QPalette.ColorRole.Mid, QColor(COLOR_BORDER_HOVER))
    pal.setColor(QPalette.ColorRole.Dark, QColor(COLOR_PRESSED))
    pal.setColor(QPalette.ColorRole.Shadow, QColor(COLOR_BORDER))
    pal.setColor(QPalette.ColorRole.BrightText, QColor(COLOR_SURFACE))
    pal.setColor(QPalette.ColorRole.Link, QColor(COLOR_ACCENT))
    disabled = QPalette.ColorGroup.Disabled
    pal.setColor(disabled, QPalette.ColorRole.Text, QColor(COLOR_TEXT_MUTED))
    pal.setColor(disabled, QPalette.ColorRole.WindowText, QColor(COLOR_TEXT_MUTED))
    pal.setColor(disabled, QPalette.ColorRole.ButtonText, QColor(COLOR_TEXT_MUTED))
    pal.setColor(disabled, QPalette.ColorRole.Highlight, QColor(COLOR_BORDER))
    app.setPalette(pal)


def accent_button_qss(
    selector: str = "QPushButton",
    *,
    radius: int | None = None,
    size: int | None = None,
    padding: str = "0",
) -> str:
    """Botão de ícone/ação no tema claro (hover, pressed, disabled, focus)."""
    r = RADIUS_MEDIUM if radius is None else radius
    size_rules = ""
    if size is not None:
        size_rules = f"""
            min-width: {size}px;
            max-width: {size}px;
            min-height: {size}px;
            max-height: {size}px;
        """
    return f"""
        {selector} {{
            border: 1px solid {COLOR_BORDER};
            border-radius: {r}px;
            background: {COLOR_SURFACE};
            color: {COLOR_ACCENT};
            padding: {padding};
            {size_rules}
        }}
        {selector}:hover {{
            background: {COLOR_HOVER};
            border-color: {COLOR_ACCENT};
        }}
        {selector}:pressed {{
            background: {COLOR_PRESSED};
        }}
        {selector}:disabled {{
            color: {COLOR_TEXT_MUTED};
            background: {COLOR_SURFACE_MUTED};
            border-color: {COLOR_BORDER};
        }}
        {selector}:focus {{
            border-color: {COLOR_ACCENT};
        }}
    """


def multiline_edit_qss(selector: str = "QPlainTextEdit") -> str:
    return f"""
        {selector} {{
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            padding: 6px 8px;
            background: {COLOR_SURFACE_MUTED};
            color: {COLOR_TEXT};
        }}
        {selector}:focus {{
            border-color: {COLOR_ACCENT};
        }}
    """


def table_frame_qss(selector: str = "QTableWidget") -> str:
    return f"""
        {selector} {{
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            background: {COLOR_SURFACE};
            gridline-color: {COLOR_BORDER};
            outline: none;
        }}
        {selector}::item {{
            padding: 4px 6px;
        }}
    """


def composite_field_qss(object_name: str = "AuthField") -> str:
    """Container visual de QLineEdit + ícone (host, senha, etc.)."""
    return f"""
        QWidget#{object_name} {{
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            background: {COLOR_SURFACE};
        }}
        QWidget#{object_name}:hover {{
            border-color: {COLOR_BORDER_HOVER};
        }}
        QWidget#{object_name}:focus-within {{
            border-color: {COLOR_ACCENT};
        }}
        QWidget#{object_name} QLineEdit {{
            border: none;
            background: transparent;
            padding: 0;
            min-height: 0px;
            max-height: {INPUT_HEIGHT}px;
        }}
    """


def make_icon_button(
    icon_char: str,
    tooltip: str = "",
    *,
    size: int = INPUT_HEIGHT,
    parent=None,
) -> QPushButton:
    """Botão quadrado só com ícone MDL2 — tamanho padrão = INPUT_HEIGHT."""
    btn = QPushButton(icon_char, parent)
    btn.setFont(QFont("Segoe MDL2 Assets", ICON_FONT_PT))
    btn.setFixedSize(size, size)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        accent_button_qss("QPushButton", radius=RADIUS_MEDIUM, size=size)
    )
    return btn


def apply_ui_defaults(app: QApplication) -> None:
    """
    Paleta clara, Fusion (desenho estável) e QSS global.
    Não define font-family em QPushButton/QToolButton para preservar MDL2.
    """
    held = _FluentStyle()
    app.setStyle(held)
    # Sem essa referência, o Qt descarta a subclasse e o checkbox volta quadrado.
    app._remoteops_style = held
    apply_light_palette(app)

    font = QFont(FONT_UI, SIZE_UI)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    content_h = INPUT_HEIGHT - 2
    app.setStyleSheet(
        f"""
        QWidget {{
            color: {COLOR_TEXT};
        }}
        QMainWindow, QDialog {{
            background-color: {COLOR_BG};
        }}
        QLabel#fieldLabel {{
            color: {COLOR_TEXT_SECONDARY};
        }}
        QLineEdit {{
            font-family: "Segoe UI";
            min-height: {content_h}px;
            max-height: {content_h}px;
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            padding: 0 10px;
            background: {COLOR_SURFACE};
            color: {COLOR_TEXT};
            selection-background-color: {COLOR_ACCENT_SOFT};
            selection-color: {COLOR_TEXT};
        }}
        QLineEdit:hover {{
            border-color: {COLOR_BORDER_HOVER};
        }}
        QLineEdit:focus {{
            border-color: {COLOR_ACCENT};
        }}
        QLineEdit:disabled {{
            background: {COLOR_SURFACE_MUTED};
            color: {COLOR_TEXT_MUTED};
            border-color: {COLOR_BORDER};
        }}
        QComboBox {{
            font-family: "Segoe UI";
            min-height: {content_h}px;
            max-height: {content_h}px;
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            padding: 0 8px;
            background: {COLOR_SURFACE};
            color: {COLOR_TEXT};
        }}
        QComboBox:hover {{
            border-color: {COLOR_BORDER_HOVER};
        }}
        QComboBox:focus, QComboBox:on {{
            border-color: {COLOR_ACCENT};
        }}
        QComboBox:disabled {{
            background: {COLOR_SURFACE_MUTED};
            color: {COLOR_TEXT_MUTED};
        }}
        QComboBox::drop-down {{
            border: none;
            width: 22px;
        }}
        QComboBox QAbstractItemView {{
            background: {COLOR_SURFACE};
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            selection-background-color: {COLOR_ACCENT_SOFT};
            selection-color: {COLOR_TEXT};
            outline: none;
            padding: 4px;
        }}
        QSpinBox {{
            font-family: "Segoe UI";
            min-height: {content_h}px;
            max-height: {content_h}px;
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            padding: 0 6px;
            background: {COLOR_SURFACE};
            color: {COLOR_TEXT};
        }}
        QSpinBox:hover {{
            border-color: {COLOR_BORDER_HOVER};
        }}
        QSpinBox:focus {{
            border-color: {COLOR_ACCENT};
        }}
        QSpinBox:disabled {{
            background: {COLOR_SURFACE_MUTED};
            color: {COLOR_TEXT_MUTED};
        }}
        QCheckBox {{
            font-family: "Segoe UI";
            min-height: 22px;
            spacing: 8px;
            color: {COLOR_TEXT};
        }}
        QPlainTextEdit, QTextEdit {{
            background: {COLOR_SURFACE_MUTED};
            color: {COLOR_TEXT};
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            padding: 6px 8px;
            selection-background-color: {COLOR_ACCENT_SOFT};
            selection-color: {COLOR_TEXT};
        }}
        QPlainTextEdit:focus, QTextEdit:focus {{
            border-color: {COLOR_ACCENT};
        }}
        QPushButton, QToolButton {{
            min-height: 22px;
        }}
        QTabWidget::pane {{
            border: none;
            background: transparent;
            top: 0px;
        }}
        QTabBar::tab {{
            padding: 0px;
            margin: 0px;
            min-width: 0px;
            border: none;
            background: transparent;
        }}
        QScrollBar:vertical {{
            background: transparent;
            width: 10px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical {{
            background: {COLOR_SCROLL};
            border-radius: 5px;
            min-height: 28px;
        }}
        QScrollBar::handle:vertical:hover {{
            background: {COLOR_SCROLL_HOVER};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: transparent;
        }}
        QScrollBar:horizontal {{
            background: transparent;
            height: 10px;
            margin: 2px;
        }}
        QScrollBar::handle:horizontal {{
            background: {COLOR_SCROLL};
            border-radius: 5px;
            min-width: 28px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background: {COLOR_SCROLL_HOVER};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            background: transparent;
        }}
        QToolTip {{
            background: {COLOR_SURFACE};
            color: {COLOR_TEXT};
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            padding: 6px 8px;
        }}
        QMenu {{
            background: {COLOR_SURFACE};
            color: {COLOR_TEXT};
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_LARGE}px;
            padding: 4px;
        }}
        QMenu::item {{
            padding: 6px 16px;
            border-radius: {RADIUS_SMALL}px;
        }}
        QMenu::item:selected {{
            background: {COLOR_HOVER};
        }}
        QMenu::separator {{
            height: 1px;
            background: {COLOR_BORDER};
            margin: 4px 8px;
        }}
        QHeaderView::section {{
            background: {COLOR_SURFACE_MUTED};
            color: {COLOR_TEXT_SECONDARY};
            border: none;
            border-bottom: 1px solid {COLOR_BORDER};
            padding: 4px 8px;
            font-weight: 600;
        }}
        QTableWidget {{
            background: {COLOR_SURFACE};
            gridline-color: {COLOR_BORDER};
            outline: none;
        }}
        QProgressBar {{
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_SMALL}px;
            background: {COLOR_SURFACE_MUTED};
            text-align: center;
            height: 16px;
        }}
        QProgressBar::chunk {{
            background: {COLOR_ACCENT};
            border-radius: {RADIUS_SMALL - 1}px;
        }}
        """
    )
