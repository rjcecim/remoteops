"""Navegação lateral da aba Inventário."""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_BORDER,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    ICON_FONT_PT,
    RADIUS_MEDIUM,
    SPACE_SM,
    SPACE_XS,
)
from remoteops.ui.inventory.layout_constants import SIDEBAR_WIDTH
from remoteops.utils.inventory.models import InventorySection

NavItem = Tuple[str, str, InventorySection]

_NAV_GROUPS: List[Tuple[str, List[NavItem]]] = [
    (
        "",
        [("\uE8A5", "Visão geral", InventorySection.OVERVIEW)],
    ),
    (
        "Equipamento",
        [
            ("\uE770", "Sistema", InventorySection.SYSTEM),
            ("\uE964", "Hardware", InventorySection.HARDWARE),
            ("\uE8F1", "Memória", InventorySection.MEMORY),
            ("\uE7B8", "Armazenamento", InventorySection.STORAGE),
            ("\uE968", "Rede", InventorySection.NETWORK),
            ("\uE7F4", "Vídeo", InventorySection.VIDEO),
        ],
    ),
    (
        "Plataforma",
        [
            ("\uE946", "Firmware", InventorySection.FIRMWARE),
            ("\uE72E", "Segurança", InventorySection.SECURITY),
            ("\uE77B", "Identidade", InventorySection.IDENTITY),
            ("\uE895", "Atualizações", InventorySection.UPDATES),
        ],
    ),
]

_SECTION_ORDER: List[InventorySection] = [
    item[2] for _title, items in _NAV_GROUPS for item in items
]

_SECTION_LABELS: dict[InventorySection, str] = {
    item[2]: item[1] for _title, items in _NAV_GROUPS for item in items
}

_SECTION_ICONS: dict[InventorySection, str] = {
    item[2]: item[0] for _title, items in _NAV_GROUPS for item in items
}


def section_label(section: InventorySection) -> str:
    return _SECTION_LABELS.get(section, section.value)


def section_icon(section: InventorySection) -> str:
    return _SECTION_ICONS.get(section, "\uE946")


class _NavButton(QFrame):
    """Item de navegação com ícone MDL2."""

    clicked = pyqtSignal()

    def __init__(self, icon: str, label: str, section: InventorySection, parent=None):
        super().__init__(parent)
        self.section = section
        self._checked = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(30)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(8)
        self._icon_lbl = QLabel(icon)
        self._icon_lbl.setFont(QFont("Segoe MDL2 Assets", ICON_FONT_PT))
        self._icon_lbl.setFixedWidth(18)
        self._text_lbl = QLabel(label)
        lay.addWidget(self._icon_lbl)
        lay.addWidget(self._text_lbl, 1)
        self._apply_style()

    def _apply_style(self) -> None:
        if self._checked:
            self.setStyleSheet(
                f"""
                QFrame {{
                    background: {COLOR_ACCENT_SOFT};
                    border: none;
                    border-radius: {RADIUS_MEDIUM}px;
                }}
                """
            )
            color = COLOR_ACCENT
            weight = "600"
        else:
            self.setStyleSheet(
                f"""
                QFrame {{
                    background: transparent;
                    border: none;
                    border-radius: {RADIUS_MEDIUM}px;
                }}
                QFrame:hover {{
                    background: palette(alternateBase);
                }}
                """
            )
            color = COLOR_TEXT_SECONDARY
            weight = "normal"
        self._text_lbl.setStyleSheet(
            f"background: transparent; border: none; color: {color}; font-weight: {weight}; font-size: 8.5pt;"
        )
        self._icon_lbl.setStyleSheet(f"background: transparent; border: none; color: {color};")

    def set_nav_checked(self, checked: bool) -> None:
        self._checked = checked
        self._apply_style()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.isEnabled():
            return
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class InventorySidebar(QFrame):
    """Painel lateral fixo com grupos e seleção."""

    sectionSelected = pyqtSignal(object)  # InventorySection

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("inventorySidebar")
        self.setFixedWidth(SIDEBAR_WIDTH)
        self.setStyleSheet(
            f"""
            QFrame#inventorySidebar {{
                background: palette(base);
                border: 1px solid {COLOR_BORDER};
                border-radius: 10px;
            }}
            """
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)
        outer.setSpacing(2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(2)

        self._buttons: List[_NavButton] = []
        for group_title, items in _NAV_GROUPS:
            if group_title:
                if self._buttons:
                    div = QFrame()
                    div.setFrameShape(QFrame.Shape.HLine)
                    div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px; margin: 6px 4px;")
                    body_lay.addWidget(div)
                grp = QLabel(group_title.upper())
                grp.setStyleSheet(
                    f"color: {COLOR_TEXT_MUTED}; font-size: 8px; font-weight: 700; letter-spacing: 0.8px; padding: 4px 4px 2px 4px;"
                )
                body_lay.addWidget(grp)
            for icon, label, section in items:
                btn = _NavButton(icon, label, section)
                btn.clicked.connect(lambda _c=False, s=section: self._select(s))
                body_lay.addWidget(btn)
                self._buttons.append(btn)

        body_lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._current = InventorySection.OVERVIEW
        self._select(InventorySection.OVERVIEW, emit=False)

    def _select(self, section: InventorySection, *, emit: bool = True) -> None:
        self._current = section
        for btn in self._buttons:
            btn.set_nav_checked(btn.section == section)
        if emit:
            self.sectionSelected.emit(section)

    def set_section(self, section: InventorySection) -> None:
        self._select(section, emit=False)

    def current_section(self) -> InventorySection:
        return self._current

    def set_enabled_nav(self, enabled: bool) -> None:
        for btn in self._buttons:
            btn.setEnabled(enabled)
            btn.setCursor(
                Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
            )


def all_sections() -> List[InventorySection]:
    return list(_SECTION_ORDER)
