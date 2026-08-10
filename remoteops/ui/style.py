from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QPushButton
from PyQt6.QtGui import QFont

# Tipografia: uma fonte de UI, uma monoespaçada para comandos/log, e tamanho dos ícones
FONT_UI = "Segoe UI"
FONT_UI_FALLBACK = "Segoe UI, sans-serif"
FONT_MONO = "Consolas"
SIZE_UI = 10
SIZE_UI_SMALL = 9
SIZE_MONO = 9
ICON_FONT_PT = 13

# Espaçamento vertical entre linhas de campos dentro dos cards (referência: card Conexão)
CARD_GRID_VERTICAL_SPACING = 4

# Altura visual única dos campos de texto (inclui borda de 1px)
INPUT_HEIGHT = 32


def make_icon_button(
    icon_char: str,
    tooltip: str = "",
    *,
    size: int = INPUT_HEIGHT,
    parent=None,
) -> QPushButton:
    """Botão quadrado só com ícone MDL2 — tamanho padrão = INPUT_HEIGHT (ex.: RustDesk)."""
    btn = QPushButton(icon_char, parent)
    btn.setFont(QFont("Segoe MDL2 Assets", ICON_FONT_PT))
    btn.setFixedSize(size, size)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(f"""
        QPushButton {{
            border: 1px solid palette(mid);
            border-radius: 4px;
            background: palette(button);
            color: palette(highlight);
            padding: 0;
            min-width: {size}px;
            max-width: {size}px;
            min-height: {size}px;
            max-height: {size}px;
        }}
        QPushButton:hover {{ background: palette(light); }}
        QPushButton:pressed {{ background: palette(dark); }}
        QPushButton:disabled {{ color: palette(mid); }}
    """)
    return btn


def apply_ui_defaults(app: QApplication) -> None:
    """
    Padroniza fonte da interface e densidade dos widgets.
    - UI: Segoe UI para labels, botões, inputs (legível e nativo no Windows).
    - Ícones: Segoe MDL2 Assets continuam nos componentes que já usam.
    - Log/Preview: Consolas é aplicado nos widgets de texto (monoespaçado).
    """
    font = QFont(FONT_UI, SIZE_UI)
    font.setStyleHint(QFont.StyleHint.SansSerif)
    app.setFont(font)

    base = app.styleSheet() or ""
    # min/max-height no stylesheet é a área de conteúdo; a borda 1px soma 2px no total.
    content_h = INPUT_HEIGHT - 2
    # Não definir font-family em QLabel/QPushButton/QToolButton no stylesheet,
    # para não sobrescrever os ícones (Segoe MDL2 Assets) definidos com setFont().
    # O app.setFont() acima já define Segoe UI como padrão para o resto.
    app.setStyleSheet(
        base
        + f"""
        QLineEdit {{
            font-family: "Segoe UI";
            min-height: {content_h}px;
            max-height: {content_h}px;
            border: 1px solid palette(mid);
            border-radius: 4px;
            padding: 0 8px;
        }}
        QLineEdit:focus {{
            border-color: palette(highlight);
        }}
        QComboBox, QSpinBox {{
            font-family: "Segoe UI";
            min-height: {content_h}px;
            max-height: {content_h}px;
        }}
        QCheckBox {{
            font-family: "Segoe UI";
            min-height: 22px;
        }}
        QPlainTextEdit, QTextEdit {{
            padding: 2px;
        }}
        QPushButton, QToolButton {{
            min-height: 22px;
        }}
        QTabBar::tab {{
            padding: 4px 8px;
        }}
        """
    )

