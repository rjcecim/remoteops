"""Widgets reutilizáveis para a aba Inventário."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_BORDER,
    COLOR_SURFACE,
    COLOR_SURFACE_MUTED,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    ICON_FONT_PT,
    RADIUS_MEDIUM,
    RADIUS_SMALL,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    make_icon_button,
)
from remoteops.ui.inventory.layout_constants import STAT_TILE_COLUMNS
from remoteops.ui.widgets.card import CardWidget
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.ui.widgets.table import SortableTableItem, configure_standard_table
from remoteops.utils.inventory.models import (
    FieldValue,
    MemoryModule,
    MemorySummary,
    PhysicalDisk,
    QueryStatus,
    SecurityData,
    SecurityItem,
    StorageData,
    VolumeInfo,
    NetworkAdapter,
    NetworkData,
    VideoAdapter,
    VideoData,
    MonitorInfo,
    FirmwareData,
    IdentityData,
    UpdatesData,
)
from remoteops.utils.psinfo import PsInfoHotfix
from remoteops.utils.inventory.formatters import is_invalid_psinfo_uptime

_MDL2 = "Segoe MDL2 Assets"


def mdl2_label(char: str, *, size: int = 14, color: str = "") -> QLabel:
    lbl = QLabel(char)
    lbl.setFont(QFont(_MDL2, size))
    if color:
        lbl.setStyleSheet(f"color: {color}; background: transparent; border: none;")
    return lbl


def muted_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return lbl


def value_label(text: str, *, bold: bool = False, size: int = 0) -> QLabel:
    lbl = QLabel(text or "—")
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    f = QFont(lbl.font())
    if bold:
        f.setBold(True)
    if size:
        f.setPointSize(size)
    lbl.setFont(f)
    return lbl


# Aliases internos
_muted_label = muted_label
_value_label = value_label


@dataclass
class OverviewMetricData:
    icon: str
    title: str
    primary: str
    secondary: str = ""


class OverviewMetricTile(QWidget):
    """Linha compacta de métrica — altura natural, sem card pesado."""

    def __init__(
        self,
        icon: str,
        title: str,
        primary: str,
        secondary: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        lay.addWidget(mdl2_label(icon, size=13, color=COLOR_ACCENT), 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)

        title_lbl = QLabel(title.upper())
        title_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt; font-weight: 600; letter-spacing: 0.4px;"
        )
        col.addWidget(title_lbl)

        pri = value_label(primary or "—", bold=True)
        pri.setStyleSheet(f"font-size: 9.5pt; color: {COLOR_TEXT};")
        pri.setWordWrap(True)
        col.addWidget(pri)

        sec = (secondary or "").strip()
        if sec and sec != "—":
            s = muted_label(sec)
            s.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
            s.setWordWrap(True)
            col.addWidget(s)

        lay.addLayout(col, 1)


class OverviewPanel(QWidget):
    """
    Visão geral — identidade + grade 2×N de métricas.
    Ocupa só o espaço necessário (não estica para preencher a janela).
    """

    def __init__(
        self,
        hostname: str,
        device_line: str = "",
        os_line: str = "",
        meta_line: str = "",
        metrics: Optional[Sequence[OverviewMetricData]] = None,
        identity_rows: Optional[Sequence[Tuple[str, str]]] = None,
        state_banner: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("overviewPanel")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        # — Identidade —
        host_lbl = value_label(hostname or "—", bold=True, size=13)
        root.addWidget(host_lbl)

        if device_line:
            dev = muted_label(device_line)
            dev.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(dev)

        if os_line:
            os_lbl = muted_label(os_line)
            os_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
            root.addWidget(os_lbl)

        if meta_line:
            meta = muted_label(meta_line)
            meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            root.addWidget(meta)

        if identity_rows:
            id_wrap = QWidget()
            id_grid = QGridLayout(id_wrap)
            id_grid.setContentsMargins(0, 2, 0, 0)
            id_grid.setHorizontalSpacing(16)
            id_grid.setVerticalSpacing(4)
            id_grid.setColumnStretch(1, 1)
            id_grid.setColumnStretch(3, 1)
            for i, (label, val) in enumerate(identity_rows):
                r, c = divmod(i, 2)
                k = muted_label(label)
                k.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
                v = value_label(val or "Não disponível")
                v.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
                id_grid.addWidget(k, r, c * 2, Qt.AlignmentFlag.AlignTop)
                id_grid.addWidget(v, r, c * 2 + 1, Qt.AlignmentFlag.AlignTop)
            root.addWidget(id_wrap)

        if state_banner:
            banner = muted_label(state_banner)
            banner.setObjectName("overviewCollectionState")
            banner.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
            root.addWidget(banner)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
        root.addWidget(div)

        hint = QLabel("RESUMO")
        hint.setStyleSheet(
            f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt; font-weight: 700; letter-spacing: 0.6px;"
        )
        root.addWidget(hint)

        grid_wrap = QWidget()
        grid_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        grid = QGridLayout(grid_wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        for i, m in enumerate(metrics or []):
            tile = OverviewMetricTile(m.icon, m.title, m.primary, m.secondary)
            grid.addWidget(tile, i // 2, i % 2, Qt.AlignmentFlag.AlignTop)

        root.addWidget(grid_wrap)


class _SystemGroupSection(QFrame):
    """Bloco compacto de um grupo PsInfo (rótulo + grade de campos)."""

    def __init__(self, icon: str, title: str, fields: Sequence[Tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"""
            QFrame {{
                background: {COLOR_SURFACE_MUTED};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MEDIUM}px;
            }}
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(mdl2_label(icon, size=12, color=COLOR_ACCENT), 0, Qt.AlignmentFlag.AlignTop)
        title_lbl = value_label(title, bold=True)
        title_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9.5pt;")
        header.addWidget(title_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(4)
        grid.setColumnStretch(1, 1)
        for row, (label, val) in enumerate(fields):
            k = muted_label(label)
            k.setMinimumWidth(118)
            k.setMaximumWidth(140)
            k.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            v = value_label(val or "—")
            v.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
            grid.addWidget(k, row, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, row, 1, Qt.AlignmentFlag.AlignTop)
        root.addLayout(grid)


class SystemPanel(QWidget):
    """
    Seção Sistema — SO/memória via CIM quando disponível; demais campos via PsInfo.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    _GROUP_ORDER: Tuple[str, ...] = (
        "Sistema operacional",
        "Hardware",
        "Registro",
        "Outros",
    )
    _GROUP_ICONS: dict[str, str] = {
        "Sistema operacional": "\uE770",
        "Hardware": "\uE950",
        "Registro": "\uE13D",
        "Outros": "\uE946",
    }
    _HERO_LABELS: frozenset[str] = frozenset({
        "Sistema operacional",
        "Versão",
        "Build",
        "Arquitetura",
        "Versão do kernel",
        "Tipo do produto",
        "Versão do produto",
        "Build do kernel",
        "Tempo ligado",
        "Data de instalação",
        "Status de ativação",
        "Host",
        "PsInfo",
    })

    def __init__(
        self,
        rows: Sequence[Tuple[str, str, str]],
        source_note: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("systemPanel")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        by_group: dict[str, List[Tuple[str, str]]] = {}
        by_label: dict[str, str] = {}
        for group, label, value in rows:
            by_group.setdefault(group, []).append((label, value or "—"))
            by_label[label] = value or "—"

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        os_caption = (by_label.get("Sistema operacional") or "").strip()
        kernel = (by_label.get("Versão do kernel") or "").strip()
        title_text = os_caption if os_caption and os_caption != "—" else kernel
        if title_text:
            title = value_label(title_text, bold=True, size=12)
            root.addWidget(title)

        subtitle_parts: List[str] = []
        if os_caption and os_caption != "—":
            version = (by_label.get("Versão") or "").strip()
            build = (by_label.get("Build") or "").strip()
            arch = (by_label.get("Arquitetura") or "").strip()
            if version and version != "—":
                subtitle_parts.append(version)
            if build and build != "—":
                subtitle_parts.append(f"Build {build}")
            if arch and arch != "—":
                subtitle_parts.append(arch)
        else:
            product_type = by_label.get("Tipo do produto", "")
            product_ver = by_label.get("Versão do produto", "")
            build = by_label.get("Build do kernel", "")
            if product_type:
                subtitle_parts.append(product_type)
            if product_ver:
                subtitle_parts.append(product_ver)
            if build:
                subtitle_parts.append(f"Build {build}")
        if subtitle_parts:
            sub = muted_label(" · ".join(subtitle_parts))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(sub)

        meta_parts: List[str] = []
        for key in ("Tempo ligado", "Data de instalação", "Status de ativação"):
            val = by_label.get(key, "").strip()
            if not val or val == "—" or (key == "Tempo ligado" and is_invalid_psinfo_uptime(val)):
                continue
            meta_parts.append(f"{key}: {val}")
        if meta_parts:
            meta = muted_label(" · ".join(meta_parts))
            meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            meta.setWordWrap(True)
            root.addWidget(meta)

        geral_bits: List[str] = []
        host = by_label.get("Host", "").strip()
        psinfo_ver = by_label.get("PsInfo", "").strip()
        if host:
            geral_bits.append(host)
        if psinfo_ver:
            geral_bits.append(f"PsInfo {psinfo_ver}")
        if geral_bits:
            src = muted_label(" · ".join(geral_bits))
            src.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
            root.addWidget(src)

        div = QFrame()
        div.setFrameShape(QFrame.Shape.HLine)
        div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
        root.addWidget(div)

        for group_name in self._GROUP_ORDER:
            fields = [
                (label, val)
                for label, val in by_group.get(group_name, [])
                if label not in self._HERO_LABELS
            ]
            if not fields:
                continue
            icon = self._GROUP_ICONS.get(group_name, "\uE946")
            root.addWidget(_SystemGroupSection(icon, group_name, fields))

        for group_name, fields in by_group.items():
            if group_name in self._GROUP_ORDER or group_name == "Geral" or not fields:
                continue
            filtered = [(label, val) for label, val in fields if label not in self._HERO_LABELS]
            if not filtered:
                continue
            root.addWidget(_SystemGroupSection("\uE946", group_name, filtered))

        foot = QLabel(source_note or "Fonte: PsInfo (Sysinternals)")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


def _field_value_text(field: FieldValue) -> str:
    if field.status == QueryStatus.UNAVAILABLE:
        return "Não foi possível consultar"
    text = (field.value or "").strip()
    return text if text else "—"


def _field_lookup(fields: Sequence[FieldValue]) -> Dict[str, FieldValue]:
    return {f.label: f for f in fields}


def _field_text(fields: Sequence[FieldValue], label: str, *, default: str = "") -> str:
    field = _field_lookup(fields).get(label)
    if field is None:
        return default
    text = _field_value_text(field)
    return default if text == "—" else text


class HardwarePanel(QWidget):
    """
    Seção Hardware — WMI via PsExec.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    _PROC_HERO_LABELS: frozenset[str] = frozenset({
        "Modelo",
        "Núcleos",
        "Processadores lógicos",
        "Clock atual",
        "Arquitetura",
    })

    def __init__(self, fields: Dict[str, List[FieldValue]], parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        computer = fields.get("Computador", [])
        processor = fields.get("Processador", [])
        board = fields.get("Placa-mãe", [])

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        manufacturer = _field_text(computer, "Fabricante")
        model = _field_text(computer, "Modelo")
        title_text = " ".join(p for p in (manufacturer, model) if p)
        if title_text:
            root.addWidget(value_label(title_text, bold=True, size=12))

        subtitle_parts: List[str] = []
        for key in ("Tipo", "Nome"):
            val = _field_text(computer, key)
            if val:
                subtitle_parts.append(val)
        if subtitle_parts:
            sub = muted_label(" · ".join(subtitle_parts))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(sub)

        cpu_name = _field_text(processor, "Modelo")
        if cpu_name:
            cpu = value_label(cpu_name)
            cpu.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9.5pt; font-weight: 600;")
            cpu.setWordWrap(True)
            root.addWidget(cpu)

        tiles: List[Tuple[str, str]] = []
        for tile_label, field_label in (
            ("Núcleos", "Núcleos"),
            ("Lógicos", "Processadores lógicos"),
            ("Clock", "Clock atual"),
            ("Arquitetura", "Arquitetura"),
        ):
            val = _field_text(processor, field_label)
            if val:
                tiles.append((tile_label, val))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        has_body = bool(processor or computer or board)
        if has_body:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

        proc_detail = [
            (f.label, _field_value_text(f))
            for f in processor
            if f.label not in self._PROC_HERO_LABELS and _field_value_text(f) != "—"
        ]
        if proc_detail:
            root.addWidget(_SystemGroupSection("\uE9F5", "Processador", proc_detail))

        ident: List[Tuple[str, str]] = []
        serial = _field_text(computer, "Número de série")
        uuid = _field_text(computer, "UUID")
        if serial:
            ident.append(("Número de série", serial))
        if uuid:
            ident.append(("UUID", uuid))
        if ident:
            root.addWidget(_SystemGroupSection("\uE7C3", "Identificação", ident))

        board_pairs = [
            (f.label, _field_value_text(f))
            for f in board
            if _field_value_text(f) != "—"
        ]
        if board_pairs:
            root.addWidget(_SystemGroupSection("\uE967", "Placa-mãe", board_pairs))

        foot = QLabel("Fonte: WMI (Win32_*) via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


class _MemoryModuleRow(QFrame):
    """Linha compacta de um módulo DIMM."""

    def __init__(self, module: MemoryModule, *, striped: bool = False, parent=None):
        super().__init__(parent)
        bg = COLOR_SURFACE_MUTED if striped else "transparent"
        self.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)

        slot = value_label(module.slot or "—", bold=True)
        slot.setMinimumWidth(72)
        slot.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
        row.addWidget(slot, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)

        spec_parts = [p for p in (module.capacity_gb, module.memory_type) if p and p != "—"]
        if module.speed_mhz and module.speed_mhz != "—":
            spec_parts.append(f"{module.speed_mhz} MHz")
        spec = value_label(" · ".join(spec_parts) if spec_parts else "—")
        spec.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
        col.addWidget(spec)

        detail_parts = [p for p in (module.manufacturer, module.part_number) if p and p != "—"]
        if detail_parts:
            detail = muted_label(" · ".join(detail_parts))
            detail.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
            col.addWidget(detail)

        row.addLayout(col, 1)


class _MemoryModulesBlock(QFrame):
    """Lista de módulos instalados."""

    def __init__(self, modules: Sequence[MemoryModule], parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"""
            QFrame {{
                background: {COLOR_SURFACE_MUTED};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MEDIUM}px;
            }}
            """
        )
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(mdl2_label("\uE8F1", size=12, color=COLOR_ACCENT), 0, Qt.AlignmentFlag.AlignTop)
        title = value_label("Módulos instalados", bold=True)
        title.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9.5pt;")
        header.addWidget(title, 1)
        root.addLayout(header)

        if not modules:
            empty = muted_label("Nenhum módulo detectado.")
            empty.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9pt;")
            root.addWidget(empty)
            return

        for i, mod in enumerate(modules):
            root.addWidget(_MemoryModuleRow(mod, striped=i % 2 == 0))


class MemoryPanel(QWidget):
    """
    Seção Memória — WMI via PsExec.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    def __init__(self, data: MemorySummary, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        total = (data.total_gb or "").strip()
        if total and total != "—":
            root.addWidget(value_label(f"{total} instalados", bold=True, size=12))

        subtitle_parts: List[str] = []
        types = {
            m.memory_type
            for m in data.modules
            if m.memory_type and m.memory_type != "—"
        }
        if len(types) == 1:
            subtitle_parts.append(next(iter(types)))
        elif len(types) > 1:
            subtitle_parts.append("Tipos mistos")

        speeds = [
            int(m.speed_mhz)
            for m in data.modules
            if m.speed_mhz and m.speed_mhz != "—" and str(m.speed_mhz).isdigit()
        ]
        if speeds:
            subtitle_parts.append(f"até {max(speeds)} MHz")

        if data.slots_used and data.slots_total and data.slots_total != "—":
            subtitle_parts.append(f"{data.slots_used}/{data.slots_total} slots")
        if subtitle_parts:
            sub = muted_label(" · ".join(subtitle_parts))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(sub)

        tiles: List[Tuple[str, str]] = []
        for label, val in (
            ("Total", data.total_gb),
            ("Slots", data.slots_total),
            ("Ocupados", data.slots_used),
            ("Livres", data.slots_free),
        ):
            if val and val != "—":
                tiles.append((label, val))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        if data.modules:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)
            root.addWidget(_MemoryModulesBlock(data.modules))

        foot = QLabel("Fonte: WMI (Win32_PhysicalMemory) via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


class _StorageListBlock(QFrame):
    """Bloco com cabeçalho e lista de linhas."""

    def __init__(self, icon: str, title: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setStyleSheet(
            f"""
            QFrame {{
                background: {COLOR_SURFACE_MUTED};
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MEDIUM}px;
            }}
            """
        )
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(10, 8, 10, 8)
        self._body.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(mdl2_label(icon, size=12, color=COLOR_ACCENT), 0, Qt.AlignmentFlag.AlignTop)
        title_lbl = value_label(title, bold=True)
        title_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9.5pt;")
        header.addWidget(title_lbl, 1)
        self._body.addLayout(header)

    def add_row(self, widget: QWidget) -> None:
        self._body.addWidget(widget)


def _physical_disk_row(disk: PhysicalDisk, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(2)

    top = QHBoxLayout()
    name = value_label(disk.model or "—", bold=True)
    name.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
    name.setWordWrap(True)
    top.addWidget(name, 1)
    cap = value_label(disk.capacity or "—")
    cap.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt; font-weight: 600;")
    top.addWidget(cap, 0, Qt.AlignmentFlag.AlignRight)
    lay.addLayout(top)

    detail_parts = [
        p for p in (disk.media_type, disk.interface, disk.status) if p and p != "—"
    ]
    if detail_parts:
        detail = muted_label(" · ".join(detail_parts))
        detail.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        lay.addWidget(detail)
    return frame


def _volume_row(volume: VolumeInfo, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(4)

    top = QHBoxLayout()
    title_parts = [p for p in (volume.letter, volume.label) if p and p != "—"]
    title = value_label(" ".join(title_parts) if title_parts else "—", bold=True)
    title.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
    top.addWidget(title, 1)
    cap = value_label(volume.capacity or "—")
    cap.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt; font-weight: 600;")
    top.addWidget(cap, 0, Qt.AlignmentFlag.AlignRight)
    lay.addLayout(top)

    meta_parts = [p for p in (volume.filesystem, f"{volume.used} usado", f"{volume.free} livre") if p and p != "—"]
    if meta_parts:
        meta = muted_label(" · ".join(meta_parts))
        meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        lay.addWidget(meta)

    lay.addWidget(VolumeUsageBar(volume.used_pct))
    return frame


def _sort_volumes(volumes: Sequence[VolumeInfo]) -> List[VolumeInfo]:
    def _key(v: VolumeInfo) -> tuple:
        letter = (v.letter or "").strip().upper().rstrip(":")
        if letter == "C":
            return (0, letter)
        if letter:
            return (1, letter)
        return (2, v.label or "")

    return sorted(volumes, key=_key)


def _pick_main_volume(volumes: Sequence[VolumeInfo], system_root: str) -> Optional[VolumeInfo]:
    if not volumes:
        return None
    root_letter = ""
    root = (system_root or "").strip()
    if len(root) >= 2 and root[1] == ":":
        root_letter = root[:2].upper()
    if root_letter:
        for v in volumes:
            if (v.letter or "").upper() == root_letter:
                return v
    for v in volumes:
        if (v.letter or "").upper().startswith("C:"):
            return v
    return volumes[0]


class StoragePanel(QWidget):
    """
    Seção Armazenamento — WMI + PsInfo.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    def __init__(self, data: StorageData, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        volumes = _sort_volumes(data.volumes)
        main = _pick_main_volume(volumes, data.system_root)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        if main:
            hero = " ".join(
                p for p in (main.letter, main.label) if p and p != "—"
            ) or main.letter or "Volume principal"
            root.addWidget(value_label(hero, bold=True, size=12))
            sub_parts: List[str] = []
            if main.filesystem and main.filesystem != "—":
                sub_parts.append(main.filesystem)
            if main.capacity and main.capacity != "—":
                sub_parts.append(main.capacity)
            if main.free and main.free != "—":
                sub_parts.append(f"{main.free} livre")
            if sub_parts:
                sub = muted_label(" · ".join(sub_parts))
                sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
                root.addWidget(sub)

        summary_parts: List[str] = []
        if data.physical_disks:
            n = len(data.physical_disks)
            summary_parts.append(f"{n} disco{'s' if n != 1 else ''}")
        if volumes:
            n = len(volumes)
            summary_parts.append(f"{n} volume{'s' if n != 1 else ''}")
        if data.system_root and data.system_root != "—":
            summary_parts.append(f"Sistema em {data.system_root}")
        if summary_parts:
            meta = muted_label(" · ".join(summary_parts))
            meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            root.addWidget(meta)

        tiles: List[Tuple[str, str]] = []
        if data.physical_disks:
            tiles.append(("Discos", str(len(data.physical_disks))))
        if volumes:
            tiles.append(("Volumes", str(len(volumes))))
        if main and main.used_pct is not None:
            tiles.append(("Uso (principal)", f"{main.used_pct:.0f}%"))
        if main and main.free and main.free != "—":
            tiles.append(("Livre", main.free))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        has_sections = bool(data.physical_disks or volumes)
        if has_sections:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

        if data.physical_disks:
            block = _StorageListBlock("\uE7B8", "Discos físicos")
            for i, disk in enumerate(data.physical_disks):
                block.add_row(_physical_disk_row(disk, striped=i % 2 == 0))
            root.addWidget(block)

        if volumes:
            block = _StorageListBlock("\uE8B7", "Volumes")
            for i, vol in enumerate(volumes):
                block.add_row(_volume_row(vol, striped=i % 2 == 0))
            root.addWidget(block)

        if data.error:
            warn = muted_label(data.error)
            warn.setStyleSheet(f"color: #B86E00; font-size: 8.5pt;")
            warn.setWordWrap(True)
            root.addWidget(warn)

        sources = []
        if data.physical_disks or any(v for v in volumes if v):
            sources.append("WMI")
        if data.psinfo_disks_raw:
            sources.append("PsInfo")
        foot_text = f"Fonte: {' + '.join(sources) or 'WMI'} via PsExec"
        foot = QLabel(foot_text)
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


def _split_ipv4(text: str) -> List[str]:
    return [p.strip() for p in (text or "").replace(";", ",").split(",") if p.strip() and p.strip() != "—"]


def _is_apipa(ip: str) -> bool:
    return (ip or "").startswith("169.254.")


def _primary_ipv4(adapter: NetworkAdapter) -> str:
    """IPv4 preferível — ignora APIPA (169.254.x.x) quando houver alternativa."""
    ips = _split_ipv4(adapter.ipv4)
    for ip in ips:
        if not _is_apipa(ip):
            return ip
    return ips[0] if ips else ""


def _is_active_adapter(adapter: NetworkAdapter) -> bool:
    """Ativo = interface Up/Connected (não basta ter APIPA em adaptador desconectado)."""
    status = (adapter.status or "").lower()
    return status in ("up", "connected")


def _adapter_priority(adapter: NetworkAdapter) -> tuple:
    """Menor tupla = adaptador mais relevante para exibir como principal."""
    status = (adapter.status or "").lower()
    name = f"{adapter.name} {adapter.description}".lower()
    ip = _primary_ipv4(adapter)
    has_gateway = bool((adapter.gateway or "").strip() and adapter.gateway != "—")

    if status not in ("up", "connected"):
        return (3, 2, 0, name)

    rank = 0
    if _is_apipa(ip) or not ip:
        rank = 2
    elif has_gateway:
        rank = 0
    else:
        rank = 1

    penalty = 0
    if any(k in name for k in ("bluetooth", "vpn", "virtual", "vethernet", "loopback", "pseudo")):
        penalty += 1
    if "wi-fi" in name or "wifi" in name or "wireless" in name or "ethernet" in name:
        penalty -= 1

    return (rank, penalty, 0 if ip else 1, name)


def _network_status_badge(status: str) -> QLabel:
    state = (status or "—").strip() or "—"
    low = state.lower()
    if low in ("up", "connected"):
        bg, fg = "#E8F5E9", "#1B7A2A"
    elif low in ("down", "disconnected", "not present", "disabled"):
        bg, fg = COLOR_SURFACE_MUTED, COLOR_TEXT_MUTED
    else:
        bg, fg = COLOR_SURFACE_MUTED, COLOR_TEXT_SECONDARY
    badge = QLabel(state)
    badge.setStyleSheet(
        f"""
        background: {bg};
        color: {fg};
        border-radius: 10px;
        padding: 2px 10px;
        font-size: 8pt;
        font-weight: 600;
        """
    )
    return badge


def _network_adapter_row(adapter: NetworkAdapter, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(3)

    top = QHBoxLayout()
    name = value_label(adapter.name or adapter.description or "—", bold=True)
    name.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
    name.setWordWrap(True)
    top.addWidget(name, 1)
    top.addWidget(_network_status_badge(adapter.status), 0, Qt.AlignmentFlag.AlignTop)
    lay.addLayout(top)

    line2_parts = [
        p for p in (_primary_ipv4(adapter) or adapter.ipv4, adapter.mac, adapter.link_speed)
        if p and p != "—"
    ]
    if line2_parts:
        line2 = value_label(" · ".join(line2_parts))
        line2.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 8.5pt;")
        line2.setWordWrap(True)
        lay.addWidget(line2)

    line3_parts = [
        p for p in (
            f"Gateway {adapter.gateway}" if adapter.gateway and adapter.gateway != "—" else "",
            f"DNS {adapter.dns}" if adapter.dns and adapter.dns != "—" else "",
            adapter.dhcp if adapter.dhcp and adapter.dhcp != "—" else "",
        ) if p
    ]
    if line3_parts:
        line3 = muted_label(" · ".join(line3_parts))
        line3.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        line3.setWordWrap(True)
        lay.addWidget(line3)

    desc = (adapter.description or "").strip()
    if desc and desc != "—" and desc != (adapter.name or "").strip():
        extra = muted_label(desc)
        extra.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        extra.setWordWrap(True)
        lay.addWidget(extra)

    return frame


def _pick_primary_adapter(adapters: Sequence[NetworkAdapter]) -> Optional[NetworkAdapter]:
    if not adapters:
        return None
    ranked = sorted(adapters, key=_adapter_priority)
    return ranked[0]


def _sort_adapters(adapters: Sequence[NetworkAdapter]) -> List[NetworkAdapter]:
    return sorted(adapters, key=_adapter_priority)


class NetworkPanel(QWidget):
    """
    Seção Rede — Get-NetAdapter / Get-NetIPConfiguration via PsExec.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    def __init__(self, data: NetworkData, parent=None):
        super().__init__(parent)
        self._data = data
        self._show_inactive = False
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        adapters = _sort_adapters(data.adapters)
        active = [a for a in adapters if _is_active_adapter(a)]
        primary = _pick_primary_adapter(adapters)

        if primary and _primary_ipv4(primary):
            root.addWidget(value_label(_primary_ipv4(primary), bold=True, size=12))
        elif primary:
            root.addWidget(value_label(primary.name or "Adaptador principal", bold=True, size=12))

        if primary:
            sub_parts = [p for p in (primary.name, primary.link_speed) if p and p != "—"]
            if primary.gateway and primary.gateway != "—":
                sub_parts.append(f"Gateway {primary.gateway}")
            if sub_parts:
                sub = muted_label(" · ".join(sub_parts))
                sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
                root.addWidget(sub)

        meta_parts: List[str] = []
        if active:
            meta_parts.append(f"{len(active)} ativo{'s' if len(active) != 1 else ''}")
        if adapters:
            meta_parts.append(f"{len(adapters)} adaptador{'es' if len(adapters) != 1 else ''}")
        if meta_parts:
            meta = muted_label(" · ".join(meta_parts))
            meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            root.addWidget(meta)

        tiles: List[Tuple[str, str]] = []
        if active:
            tiles.append(("Ativos", str(len(active))))
        if adapters:
            tiles.append(("Total", str(len(adapters))))
        if primary and primary.mac and primary.mac != "—":
            tiles.append(("MAC", primary.mac))
        if primary and primary.link_speed and primary.link_speed != "—":
            tiles.append(("Velocidade", primary.link_speed))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        inactive_count = len(adapters) - len(active)
        if inactive_count > 0:
            self._toggle = QPushButton(f"Mostrar {inactive_count} adaptador(es) inativo(s)")
            self._toggle.setStyleSheet(
                "text-align: left; padding: 4px 0; border: none; color: palette(highlight); font-size: 9pt;"
            )
            self._toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            self._toggle.clicked.connect(self._on_toggle)
            root.addWidget(self._toggle)
        else:
            self._toggle = None

        if adapters:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

        self._list_host = QWidget()
        self._list_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(0)
        root.addWidget(self._list_host)
        self._refresh_adapters()

        foot = QLabel("Fonte: Get-NetAdapter / Get-NetIPConfiguration via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)

    def _on_toggle(self) -> None:
        self._show_inactive = not self._show_inactive
        if self._toggle is not None:
            inactive = len(self._data.adapters) - len(
                [a for a in self._data.adapters if _is_active_adapter(a)]
            )
            if self._show_inactive:
                self._toggle.setText("Ocultar adaptadores inativos")
            else:
                self._toggle.setText(f"Mostrar {inactive} adaptador(es) inativo(s)")
        self._refresh_adapters()

    def _refresh_adapters(self) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        adapters = _sort_adapters(self._data.adapters)
        if not self._show_inactive:
            adapters = [a for a in adapters if _is_active_adapter(a)]

        if not adapters:
            empty = muted_label("Nenhum adaptador para exibir.")
            empty.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9pt;")
            self._list_layout.addWidget(empty)
            return

        block = _StorageListBlock("\uE968", "Adaptadores")
        for i, adapter in enumerate(adapters):
            block.add_row(_network_adapter_row(adapter, striped=i % 2 == 0))
        self._list_layout.addWidget(block)


def _gpu_priority(adapter: VideoAdapter) -> tuple:
    name = (adapter.name or "").lower()
    penalty = 0
    if "basic display" in name or name.startswith("microsoft basic"):
        penalty += 3
    if "remote" in name or "virtual" in name:
        penalty += 2
    if adapter.resolution and adapter.resolution != "—":
        penalty -= 2
    if adapter.video_memory and adapter.video_memory != "—":
        penalty -= 1
    if adapter.driver_version and adapter.driver_version != "—":
        penalty -= 1
    return (penalty, name)


def _pick_primary_gpu(adapters: Sequence[VideoAdapter]) -> Optional[VideoAdapter]:
    if not adapters:
        return None
    return sorted(adapters, key=_gpu_priority)[0]


def _video_adapter_row(gpu: VideoAdapter, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(3)

    top = QHBoxLayout()
    name = value_label(gpu.name or "Placa de vídeo", bold=True)
    name.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
    name.setWordWrap(True)
    top.addWidget(name, 1)
    if gpu.resolution and gpu.resolution != "—":
        res = value_label(gpu.resolution)
        res.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt; font-weight: 600;")
        top.addWidget(res, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
    lay.addLayout(top)

    line2_parts = [
        p for p in (gpu.video_memory, gpu.manufacturer) if p and p != "—"
    ]
    if line2_parts:
        line2 = muted_label(" · ".join(line2_parts))
        line2.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 8.5pt;")
        lay.addWidget(line2)

    driver_parts = [
        p for p in (
            gpu.driver if gpu.driver and gpu.driver != gpu.name else "",
            f"v{gpu.driver_version}" if gpu.driver_version and gpu.driver_version != "—" else "",
            gpu.driver_date if gpu.driver_date and gpu.driver_date != "—" else "",
        ) if p
    ]
    if driver_parts:
        driver = muted_label(" · ".join(driver_parts))
        driver.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        driver.setWordWrap(True)
        lay.addWidget(driver)

    return frame


def _monitor_row(monitor: MonitorInfo, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(3)

    model = monitor.model if monitor.model and monitor.model != "—" else ""
    manufacturer = monitor.manufacturer if monitor.manufacturer and monitor.manufacturer != "—" else ""
    serial = monitor.serial if monitor.serial and monitor.serial != "—" else ""
    title = model or manufacturer or serial or "Monitor"

    top = QHBoxLayout()
    name = value_label(title, bold=True)
    name.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
    name.setWordWrap(True)
    top.addWidget(name, 1)
    if serial and serial != title:
        serial_lbl = value_label(serial)
        serial_lbl.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt; font-weight: 600;")
        serial_lbl.setToolTip("Número de série")
        top.addWidget(serial_lbl, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
    lay.addLayout(top)

    detail_parts = []
    if manufacturer and manufacturer != title:
        detail_parts.append(manufacturer)
    if model and model != title:
        detail_parts.append(model)
    if detail_parts:
        detail = muted_label(" · ".join(detail_parts))
        detail.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 8.5pt;")
        lay.addWidget(detail)

    return frame


class VideoPanel(QWidget):
    """
    Seção Vídeo — Win32_VideoController e WmiMonitorID via PsExec.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    def __init__(self, data: VideoData, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        adapters = list(data.adapters)
        monitors = list(data.monitors)
        primary = _pick_primary_gpu(adapters)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        if primary and (primary.name or "").strip():
            root.addWidget(value_label(primary.name, bold=True, size=12))

        if primary:
            sub_parts = [
                p for p in (primary.resolution, primary.video_memory, primary.manufacturer)
                if p and p != "—"
            ]
            if sub_parts:
                sub = muted_label(" · ".join(sub_parts))
                sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
                root.addWidget(sub)

        meta_parts: List[str] = []
        if adapters:
            meta_parts.append(
                f"{len(adapters)} adaptador{'es' if len(adapters) != 1 else ''} de vídeo"
            )
        if monitors:
            meta_parts.append(
                f"{len(monitors)} monitor{'es' if len(monitors) != 1 else ''}"
            )
        if meta_parts:
            meta = muted_label(" · ".join(meta_parts))
            meta.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            root.addWidget(meta)

        tiles: List[Tuple[str, str]] = []
        if adapters:
            tiles.append(("GPUs", str(len(adapters))))
        if monitors:
            tiles.append(("Monitores", str(len(monitors))))
        if primary and primary.resolution and primary.resolution != "—":
            tiles.append(("Resolução", primary.resolution))
        if primary and primary.video_memory and primary.video_memory != "—":
            tiles.append(("Memória", primary.video_memory))
        if primary and primary.driver_version and primary.driver_version != "—":
            tiles.append(("Driver", primary.driver_version))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        if adapters:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

            block = _StorageListBlock("\uE7F4", "Adaptadores de vídeo")
            for i, gpu in enumerate(sorted(adapters, key=_gpu_priority)):
                block.add_row(_video_adapter_row(gpu, striped=i % 2 == 0))
            root.addWidget(block)
        else:
            empty = muted_label("Nenhuma placa de vídeo detectada.")
            empty.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9pt;")
            root.addWidget(empty)

        div_mon = QFrame()
        div_mon.setFrameShape(QFrame.Shape.HLine)
        div_mon.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
        root.addWidget(div_mon)

        mon_block = _StorageListBlock("\uE7F4", "Monitores")
        if monitors:
            for i, monitor in enumerate(monitors):
                mon_block.add_row(_monitor_row(monitor, striped=i % 2 == 0))
        else:
            empty_mon = muted_label("Nenhum monitor detectado.")
            empty_mon.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9pt;")
            mon_block.add_row(empty_mon)
        root.addWidget(mon_block)

        foot = QLabel("Fonte: Win32_VideoController e WmiMonitorID via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


def _firmware_state_badge(field: FieldValue) -> QLabel:
    text = _field_value_text(field)
    low = text.lower()
    if field.status == QueryStatus.UNAVAILABLE:
        bg, fg = "#FFF8E1", "#B86E00"
    elif low in ("ativado", "presente") or text.startswith("2."):
        bg, fg = "#E8F5E9", "#1B7A2A"
    elif low in ("desativado", "indisponível"):
        bg, fg = COLOR_SURFACE_MUTED, COLOR_TEXT_MUTED
    else:
        bg, fg = COLOR_SURFACE_MUTED, COLOR_TEXT_SECONDARY
    badge = QLabel(text)
    badge.setStyleSheet(
        f"""
        background: {bg};
        color: {fg};
        border-radius: 10px;
        padding: 2px 10px;
        font-size: 8pt;
        font-weight: 600;
        """
    )
    if field.tooltip:
        badge.setToolTip(field.tooltip)
    return badge


def _firmware_feature_row(label: str, field: FieldValue, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    row = QHBoxLayout(frame)
    row.setContentsMargins(8, 6, 8, 6)
    name = value_label(label)
    name.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: 500; font-size: 9pt;")
    name.setMinimumWidth(120)
    row.addWidget(name)
    row.addStretch(1)
    row.addWidget(_firmware_state_badge(field))
    return frame


class FirmwarePanel(QWidget):
    """
    Seção Firmware — BIOS, Secure Boot e TPM via PsExec.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    _BIOS_HERO_LABELS: frozenset[str] = frozenset({"Fabricante", "Versão", "Data"})

    def __init__(self, data: FirmwareData, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        bios = data.fields.get("BIOS", [])
        manufacturer = _field_text(bios, "Fabricante")
        version = _field_text(bios, "Versão")
        date = _field_text(bios, "Data")
        serial = _field_text(bios, "Serial")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = " · ".join(p for p in (manufacturer, version) if p) or "Firmware"
        root.addWidget(value_label(title, bold=True, size=12))

        subtitle_parts = [p for p in (date, f"S/N {serial}" if serial else "") if p]
        if subtitle_parts:
            sub = muted_label(" · ".join(subtitle_parts))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(sub)

        tiles: List[Tuple[str, str]] = []
        sb_text = _field_value_text(data.secure_boot)
        tpm_text = _field_value_text(data.tpm)
        if sb_text != "—":
            tiles.append(("Secure Boot", sb_text))
        if tpm_text != "—":
            tiles.append(("TPM", tpm_text))
        if version:
            tiles.append(("BIOS", version))
        if manufacturer:
            tiles.append(("Fabricante", manufacturer))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        has_body = bool(bios) or data.secure_boot or data.tpm
        if has_body:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

        platform = _StorageListBlock("\uE72E", "Plataforma")
        platform.add_row(_firmware_feature_row("Secure Boot", data.secure_boot, striped=False))
        platform.add_row(_firmware_feature_row("TPM", data.tpm, striped=True))
        root.addWidget(platform)

        bios_detail = [
            (f.label, _field_value_text(f))
            for f in bios
            if f.label not in self._BIOS_HERO_LABELS and _field_value_text(f) != "—"
        ]
        if bios_detail:
            root.addWidget(_SystemGroupSection("\uE7B8", "BIOS", bios_detail))

        foot = QLabel("Fonte: Win32_BIOS / Secure Boot / TPM via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


def _security_is_positive(state: str) -> bool:
    low = (state or "").lower()
    if low in ("ativado", "ativo", "presente"):
        return True
    if "sem volumes bitlocker" in low:
        return True
    # Versão TPM (ex.: 2.0, 7.85.4555.0) indica hardware presente
    if re.fullmatch(r"[\d.]+", (state or "").strip()):
        return True
    return False


def _security_is_negative(state: str) -> bool:
    low = (state or "").lower()
    return low in ("desativado", "indisponível")


def _security_is_warning(item: SecurityItem) -> bool:
    if item.status == QueryStatus.UNAVAILABLE:
        return True
    low = (item.state or "").lower()
    return any(
        token in low
        for token in ("não foi possível", "parcial", "indeterminado", "ressalva")
    )


def _security_state_badge(item: SecurityItem) -> QLabel:
    state = (item.state or "—").strip() or "—"
    if _security_is_warning(item):
        bg, fg = "#FFF8E1", "#B86E00"
    elif _security_is_positive(state):
        bg, fg = "#E8F5E9", "#1B7A2A"
    elif _security_is_negative(state):
        bg, fg = "#FDE7E9", "#C42B1C"
    else:
        bg, fg = COLOR_SURFACE_MUTED, COLOR_TEXT_SECONDARY
    badge = QLabel(state)
    badge.setStyleSheet(
        f"""
        background: {bg};
        color: {fg};
        border-radius: 10px;
        padding: 2px 10px;
        font-size: 8pt;
        font-weight: 600;
        """
    )
    if item.detail:
        badge.setToolTip(item.detail)
    return badge


def _security_item_row(item: SecurityItem, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    row = QHBoxLayout(frame)
    row.setContentsMargins(8, 6, 8, 6)
    name = value_label(item.name or "—")
    name.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: 500; font-size: 9pt;")
    name.setMinimumWidth(120)
    row.addWidget(name)
    row.addStretch(1)
    row.addWidget(_security_state_badge(item))
    return frame


def _security_summary(items: Sequence[SecurityItem]) -> tuple[int, int, int]:
    active = warning = disabled = 0
    for item in items:
        if _security_is_warning(item):
            warning += 1
        elif _security_is_negative(item.state):
            disabled += 1
        elif _security_is_positive(item.state):
            active += 1
        else:
            warning += 1
    return active, warning, disabled


class SecurityPanel(QWidget):
    """
    Seção Segurança — BitLocker, Defender, Firewall, UAC, Secure Boot, TPM.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    def __init__(self, data: SecurityData, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        items = list(data.items)
        active, warning, disabled = _security_summary(items)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        if disabled == 0 and warning == 0 and active > 0:
            title = "Controles de segurança ativos"
        elif disabled > 0:
            title = f"{disabled} controle{'s' if disabled != 1 else ''} desativado{'s' if disabled != 1 else ''}"
        elif warning > 0:
            title = f"{warning} item{'ns' if warning != 1 else ''} com ressalva"
        else:
            title = "Estado de segurança"
        root.addWidget(value_label(title, bold=True, size=12))

        disabled_names = [i.name for i in items if _security_is_negative(i.state)]
        if disabled_names:
            sub = muted_label("Desativado: " + ", ".join(disabled_names))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            sub.setWordWrap(True)
            root.addWidget(sub)

        tiles: List[Tuple[str, str]] = []
        if active:
            tiles.append(("Ativos", str(active)))
        if warning:
            tiles.append(("Atenção", str(warning)))
        if disabled:
            tiles.append(("Desativados", str(disabled)))
        if items:
            tiles.append(("Total", str(len(items))))
        if tiles:
            root.addWidget(stat_tile_grid(tiles))

        if items:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

            block = _StorageListBlock("\uE72E", "Controles")
            for i, item in enumerate(items):
                block.add_row(_security_item_row(item, striped=i % 2 == 0))
            root.addWidget(block)
        else:
            empty = muted_label("Nenhum controle consultado.")
            empty.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9pt;")
            root.addWidget(empty)

        if data.error:
            warn = muted_label(data.error)
            warn.setStyleSheet(f"color: #B86E00; font-size: 8.5pt;")
            warn.setWordWrap(True)
            root.addWidget(warn)

        foot = QLabel("Fonte: BitLocker / Defender / Firewall / UAC / TPM via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)


def _identity_info_row(label: str, value: str, *, monospace: bool = False) -> QFrame:
    frame = QFrame()
    frame.setStyleSheet(f"background: transparent; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 4, 8, 4)
    lay.setSpacing(1)
    lbl = muted_label(label)
    lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
    lay.addWidget(lbl)
    val = value_label(value or "—")
    style = "font-size: 9pt;"
    if monospace:
        style += " font-family: Consolas, monospace;"
    val.setStyleSheet(style)
    val.setWordWrap(True)
    lay.addWidget(val)
    return frame


class IdentitySectionPanel(QWidget):
    """
    Seção Identidade — computador, usuário e consulta PsGetSid.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    copy_requested = pyqtSignal(str)
    sid_query_requested = pyqtSignal(str)
    open_contas_locais_requested = pyqtSignal(str)

    def __init__(self, data: IdentityData, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._data = data

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = (data.computer_name or "").strip().rstrip("$") or "—"
        root.addWidget(value_label(title, bold=True, size=12))

        subtitle_parts: List[str] = []
        if data.computer_domain and data.computer_domain != "—":
            subtitle_parts.append(data.computer_domain)
        if data.user_name and data.user_name != "—":
            subtitle_parts.append(data.user_name)
        if subtitle_parts:
            sub = muted_label(" · ".join(subtitle_parts))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(sub)

        has_ids = any(
            v and v != "—"
            for v in (data.computer_sid, data.user_sid, data.computer_name, data.user_name)
        )
        if has_ids:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

        comp = _StorageListBlock("\uE8EA", "Computador")
        comp.add_row(_identity_info_row("Nome", data.computer_name))
        comp.add_row(_identity_info_row("Domínio", data.computer_domain or "—"))
        sid_field = CopyableField("SID", data.computer_sid or "—")
        sid_field.copied.connect(self.copy_requested.emit)
        comp.add_row(sid_field)
        root.addWidget(comp)

        user = _StorageListBlock("\uE77B", "Usuário no console")
        user.add_row(
            _identity_info_row(
                "Sessão interativa",
                data.user_name or "Ninguém no console",
            )
        )
        user_sid = CopyableField("SID", data.user_sid or "—")
        user_sid.copied.connect(self.copy_requested.emit)
        user.add_row(user_sid)
        root.addWidget(user)

        query = _StorageListBlock("\uE721", "Consultar SID")
        query_wrap = QWidget()
        qw = QVBoxLayout(query_wrap)
        qw.setContentsMargins(0, 0, 0, 0)
        qw.setSpacing(6)
        self._sid_query_edit = QLineEdit()
        self._sid_query_edit.setPlaceholderText("Conta, grupo ou SID")
        self._sid_query_edit.returnPressed.connect(self._emit_sid_query)
        query_btn = make_icon_button("\uE721", "Consultar SID", size=28)
        query_btn.clicked.connect(self._emit_sid_query)
        search_row = QHBoxLayout()
        search_row.setContentsMargins(0, 0, 0, 0)
        search_row.setSpacing(6)
        search_row.addWidget(self._sid_query_edit, 1)
        search_row.addWidget(query_btn, 0)
        qw.addLayout(search_row)
        self._sid_query_result = value_label("")
        self._sid_query_result.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 9pt; color: palette(text);"
        )
        self._sid_query_result.setWordWrap(True)
        qw.addWidget(self._sid_query_result)
        query.add_row(query_wrap)
        root.addWidget(query)

        accounts_btn = make_icon_button(
            "\uE77B",
            "Abrir em Contas Locais",
            size=28,
        )
        accounts_btn.clicked.connect(self._emit_open_contas_locais)
        console_user = (data.user_name or "").strip()
        computer = (data.computer_name or "").strip().rstrip("$")
        from remoteops.utils.local_accounts import is_probably_local_session_user

        if console_user and console_user != "—":
            if is_probably_local_session_user(
                console_user,
                data.computer_domain or "",
                computer_name=computer,
            ):
                accounts_btn.setToolTip(
                    "Listar contas locais e selecionar a conta do console, se existir."
                )
            else:
                accounts_btn.setEnabled(False)
                accounts_btn.setToolTip(
                    "Contas de domínio não podem ser alteradas por este módulo."
                )
        else:
            accounts_btn.setToolTip(
                "Abrir Contas Locais sem pré-selecionar conta."
            )
        root.addWidget(accounts_btn)

        if data.error and not data.computer_sid and not data.user_sid:
            warn = muted_label(data.error)
            warn.setStyleSheet(f"color: #B86E00; font-size: 8.5pt;")
            warn.setWordWrap(True)
            root.addWidget(warn)

        foot = QLabel(
            "Fonte: PsGetSid (SID) · WMI (console). Sessões e compartilhamentos: aba Sessões."
        )
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)

    def _emit_sid_query(self) -> None:
        account = (self._sid_query_edit.text() or "").strip()
        if account:
            self.sid_query_requested.emit(account)

    def _emit_open_contas_locais(self) -> None:
        user = (self._data.user_name or "").strip()
        if user == "—":
            user = ""
        if "\\" in user:
            user = user.split("\\", 1)[-1]
        self.open_contas_locais_requested.emit(user)

    def set_sid_query_result(self, text: str) -> None:
        self._sid_query_result.setText(text or "")


def _hotfix_row(hf: PsInfoHotfix, *, striped: bool = False) -> QFrame:
    frame = QFrame()
    bg = COLOR_SURFACE_MUTED if striped else "transparent"
    frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(8, 6, 8, 6)
    lay.setSpacing(2)

    top = QHBoxLayout()
    kb = value_label(hf.id or "—", bold=True)
    kb.setStyleSheet(f"color: {COLOR_TEXT}; font-size: 9pt;")
    top.addWidget(kb, 1)
    if hf.installed and hf.installed != "—":
        date = muted_label(hf.installed)
        date.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        top.addWidget(date, 0, Qt.AlignmentFlag.AlignRight)
    lay.addLayout(top)

    if hf.description and hf.description != "—":
        desc = muted_label(hf.description)
        desc.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 8pt;")
        desc.setWordWrap(True)
        lay.addWidget(desc)
    return frame


class UpdatesPanel(QWidget):
    """
    Seção Atualizações — hotfixes via PsInfo (-h) ou Get-HotFix.
    Layout compacto alinhado ao topo (não estica para preencher a janela).
    """

    def __init__(self, data: UpdatesData, parent=None):
        super().__init__(parent)
        self._hotfixes = list(data.hotfixes)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)
        root.setAlignment(Qt.AlignmentFlag.AlignTop)

        title = data.last_update or (f"{data.count} hotfix(es)" if data.count else "Atualizações")
        root.addWidget(value_label(title, bold=True, size=12))

        sub_parts: List[str] = []
        if data.last_update_date and data.last_update_date != "—":
            sub_parts.append(data.last_update_date)
        if data.count:
            sub_parts.append(f"{data.count} instalado{'s' if data.count != 1 else ''}")
        if sub_parts:
            sub = muted_label(" · ".join(sub_parts))
            sub.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY}; font-size: 9pt;")
            root.addWidget(sub)

        tiles: List[Tuple[str, str]] = []
        if data.last_update:
            tiles.append(("Última KB", data.last_update))
        if data.last_update_date and data.last_update_date != "—":
            tiles.append(("Data", data.last_update_date))
        tiles.append(("Total", str(data.count)))
        root.addWidget(stat_tile_grid(tiles))

        if data.hotfixes:
            div = QFrame()
            div.setFrameShape(QFrame.Shape.HLine)
            div.setStyleSheet(f"color: {COLOR_BORDER}; max-height: 1px;")
            root.addWidget(div)

            search_row = QHBoxLayout()
            search_row.setContentsMargins(0, 0, 0, 0)
            search_row.setSpacing(6)
            self._search = QLineEdit()
            self._search.setPlaceholderText("Buscar hotfix (KB/Q)...")
            self._search.textChanged.connect(self._refresh_list)
            filter_btn = make_icon_button("\uE721", "Filtrar hotfixes", size=28)
            filter_btn.clicked.connect(self._search.setFocus)
            search_row.addWidget(self._search, 1)
            search_row.addWidget(filter_btn, 0)
            root.addLayout(search_row)

            self._list_host = QWidget()
            self._list_host.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
            self._list_layout = QVBoxLayout(self._list_host)
            self._list_layout.setContentsMargins(0, 0, 0, 0)
            self._list_layout.setSpacing(0)
            root.addWidget(self._list_host)
            self._refresh_list()

        if data.error and not data.hotfixes:
            warn = muted_label(data.error)
            warn.setStyleSheet("color: #B86E00; font-size: 8.5pt;")
            warn.setWordWrap(True)
            root.addWidget(warn)

        foot = QLabel(data.note or "Fonte: PsInfo (-h) ou Get-HotFix via PsExec")
        foot.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 7.5pt;")
        root.addWidget(foot)

    def _filtered(self) -> List[PsInfoHotfix]:
        q = (self._search.text() if hasattr(self, "_search") else "").strip().lower()
        if not q:
            return list(self._hotfixes)
        return [
            h
            for h in self._hotfixes
            if q in f"{h.id} {h.description} {h.installed}".lower()
        ]

    def _refresh_list(self, *_args) -> None:
        while self._list_layout.count():
            item = self._list_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        visible = self._filtered()
        if not visible:
            empty = muted_label("Nenhum hotfix corresponde à busca.")
            empty.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9pt;")
            self._list_layout.addWidget(empty)
            return

        block = _StorageListBlock("\uE895", f"Hotfixes ({len(visible)})")
        for i, hf in enumerate(visible):
            block.add_row(_hotfix_row(hf, striped=i % 2 == 0))
        self._list_layout.addWidget(block)


class SectionHeader(QWidget):
    """Cabeçalho compacto — host fica na toolbar da aba."""

    def __init__(
        self,
        icon: str,
        title: str,
        subtitle: str = "",
        host: str = "",
        parent=None,
        *,
        compact: bool = True,
    ):
        super().__init__(parent)
        _ = host  # host exibido na toolbar; evita duplicar em 768px
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, SPACE_SM)
        lay.setSpacing(8)

        icon_wrap = QFrame()
        icon_wrap.setFixedSize(28, 28)
        icon_wrap.setStyleSheet(
            f"background: {COLOR_ACCENT_SOFT}; border-radius: {RADIUS_SMALL}px; border: none;"
        )
        iw_lay = QHBoxLayout(icon_wrap)
        iw_lay.setContentsMargins(0, 0, 0, 0)
        iw_lay.addWidget(mdl2_label(icon, size=13, color=COLOR_ACCENT), 0, Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon_wrap)

        text_col = QVBoxLayout()
        text_col.setSpacing(0)
        text_col.addWidget(value_label(title, bold=True, size=11))
        if subtitle and not compact:
            sub = muted_label(subtitle)
            sub.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
            text_col.addWidget(sub)
        lay.addLayout(text_col, 1)


class StatTile(QFrame):
    """Bloco numérico compacto para resumos."""

    def __init__(self, label: str, value: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"""
            background: {COLOR_SURFACE_MUTED};
            border: 1px solid {COLOR_BORDER};
            border-radius: {RADIUS_MEDIUM}px;
            """
        )
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(1)
        lbl = muted_label(label)
        lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8pt;")
        lay.addWidget(lbl)
        val = value_label(value, bold=True)
        val.setStyleSheet("font-size: 9.5pt;")
        lay.addWidget(val)


class FieldGridWidget(QWidget):
    """Grid compacto label/valor — otimizado para 768×960."""

    def __init__(self, groups: dict[str, List[FieldValue]], parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACE_SM)

        for group_name, fields in groups.items():
            if not fields:
                continue
            title = value_label(group_name, bold=True)
            title.setStyleSheet("color: palette(highlight); font-size: 9.5pt;")
            root.addWidget(title)

            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 4)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(3)
            grid.setColumnStretch(1, 1)
            for row, field in enumerate(fields):
                val_text = field.value or "—"
                if field.status == QueryStatus.UNAVAILABLE:
                    val_text = "Não foi possível consultar"
                k = muted_label(field.label)
                k.setMinimumWidth(112)
                k.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 8.5pt;")
                v = value_label(val_text)
                v.setStyleSheet("font-size: 9pt;")
                if field.tooltip:
                    v.setToolTip(field.tooltip)
                grid.addWidget(k, row, 0, Qt.AlignmentFlag.AlignTop)
                grid.addWidget(v, row, 1, Qt.AlignmentFlag.AlignTop)
            wrap = QWidget()
            wrap.setLayout(grid)
            root.addWidget(wrap)


class SecurityStatusWidget(QWidget):
    """Painel de segurança com linhas alternadas."""

    def __init__(self, items: Sequence[SecurityItem], parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        card = CardWidget("\uE72E", "Estado de segurança")
        card.set_collapsible(False)
        card.set_expanding(True)

        for i, item in enumerate(items):
            row_frame = QFrame()
            bg = COLOR_SURFACE_MUTED if i % 2 == 0 else "transparent"
            row_frame.setStyleSheet(f"background: {bg}; border-radius: {RADIUS_SMALL}px;")
            row = QHBoxLayout(row_frame)
            row.setContentsMargins(8, 6, 8, 6)

            name = value_label(item.name)
            name.setStyleSheet(f"color: {COLOR_TEXT}; font-weight: 500; font-size: 9pt;")
            name.setMinimumWidth(140)

            state = item.state
            pill_bg = COLOR_SURFACE_MUTED
            pill_fg = COLOR_TEXT_SECONDARY
            if item.status == QueryStatus.UNAVAILABLE:
                pill_bg = "#FFF8E1"
                pill_fg = "#B86E00"
            elif state.lower() in ("ativado", "ativo", "presente") or state.startswith("2."):
                pill_bg = "#E8F5E9"
                pill_fg = "#1B7A2A"
            elif state.lower() == "desativado":
                pill_bg = COLOR_SURFACE_MUTED
                pill_fg = COLOR_TEXT_MUTED

            badge = QLabel(state)
            badge.setStyleSheet(
                f"""
                background: {pill_bg};
                color: {pill_fg};
                border-radius: 10px;
                padding: 3px 12px;
                font-size: 9pt;
                font-weight: 600;
                """
            )
            if item.detail:
                badge.setToolTip(item.detail)

            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(badge)
            card.content_layout.addWidget(row_frame)

        lay.addWidget(card)


class VolumeUsageBar(QWidget):
    """Barra de utilização para volumes."""

    def __init__(self, used_pct: Optional[float], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 0, 0)
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setFixedHeight(8)
        pct = int(used_pct) if used_pct is not None else 0
        bar.setValue(min(100, max(0, pct)))
        chunk = COLOR_ACCENT
        if used_pct is not None and used_pct >= 90:
            chunk = "#C42B1C"
        elif used_pct is not None and used_pct >= 75:
            chunk = "#CA5010"
        bar.setStyleSheet(
            f"QProgressBar {{ background: {COLOR_SURFACE_MUTED}; border: none; border-radius: 4px; }}"
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 4px; }}"
        )
        lay.addWidget(bar, 1)
        if used_pct is not None:
            lay.addWidget(muted_label(f"{used_pct:.0f}%"))


class CopyableField(QFrame):
    """Campo com valor técnico e botão copiar."""

    copied = pyqtSignal(str)

    def __init__(self, label: str, value: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background: {COLOR_SURFACE_MUTED}; border: 1px solid {COLOR_BORDER}; border-radius: {RADIUS_MEDIUM}px; }}"
        )
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 6, 6)
        lay.setSpacing(SPACE_SM)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(muted_label(label))
        val = value_label(value)
        val.setStyleSheet("font-family: Consolas, monospace;")
        col.addWidget(val)
        lay.addLayout(col, 1)
        btn = make_icon_button("\uE8C8", "Copiar", size=28)
        btn.clicked.connect(lambda: self.copied.emit(value))
        lay.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)


class IdentityPanel(CardWidget):
    """Card de identidade compacto."""

    def __init__(self, icon: str, title: str, fields: List[Tuple[str, str]], parent=None):
        super().__init__(icon, title, parent)
        self.set_collapsible(False)
        self.set_expanding(False)
        self.content_layout.setSpacing(4)
        for label, val in fields:
            self.content_layout.addWidget(CopyableField(label, val))


def build_standard_table(columns: Sequence[str], stretch: Tuple[int, ...] = ()) -> QTableWidget:
    table = QTableWidget()
    table.setColumnCount(len(columns))
    table.setHorizontalHeaderLabels(list(columns))
    configure_standard_table(table, stretch_columns=stretch)
    table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    table.setMinimumHeight(72)
    table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    return table


def stat_tile_grid(pairs: Sequence[Tuple[str, str]], *, columns: int = STAT_TILE_COLUMNS) -> QWidget:
    """Grade 2×N de StatTiles para caber em ~572px de largura."""
    wrap = QWidget()
    grid = QGridLayout(wrap)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setSpacing(SPACE_SM)
    cols = max(1, columns)
    for i, (label, value) in enumerate(pairs):
        grid.addWidget(StatTile(label, value), i // cols, i % cols)
    return wrap


def scroll_content(widget: QWidget, *, align_top: bool = False) -> QScrollArea:
    """Área rolável; com ``align_top``, o conteúdo não estica para preencher a altura."""
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    if align_top:
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    scroll.setWidget(widget)
    return scroll


def wrap_section_page(
    header: SectionHeader,
    body: QWidget,
    *,
    margin_bottom: int = 0,
) -> QWidget:
    """Empacota cabeçalho + corpo scrollável."""
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, margin_bottom)
    lay.setSpacing(0)
    lay.addWidget(header)
    lay.addWidget(body, 1)
    return page


def section_error_widget(message: str) -> QWidget:
    frame = QFrame()
    frame.setStyleSheet(
        f"""
        QFrame {{
            background: #FEF2F2;
            border: 1px solid #FECACA;
            border-radius: {RADIUS_MEDIUM}px;
        }}
        """
    )
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
    lay.setSpacing(SPACE_MD)
    lay.addWidget(mdl2_label("\uE783", size=18, color="#C42B1C"))
    col = QVBoxLayout()
    col.addWidget(value_label("Não foi possível consultar", bold=True))
    msg = QLabel(message or "Verifique conectividade e credenciais.")
    msg.setWordWrap(True)
    msg.setStyleSheet(f"color: {COLOR_TEXT_SECONDARY};")
    col.addWidget(msg)
    lay.addLayout(col, 1)
    wrap = QWidget()
    outer = QVBoxLayout(wrap)
    outer.setContentsMargins(0, SPACE_MD, 0, 0)
    outer.addWidget(frame)
    outer.addStretch(1)
    return wrap


def section_loading_widget(message: str = "Coletando informações...") -> QWidget:
    """Indicador de carregamento único (spinner + mensagem)."""
    wrap = QWidget()
    wrap.setObjectName("inventoryLoadingState")
    wrap.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    lay = QVBoxLayout(wrap)
    lay.setContentsMargins(0, 24, 0, 24)
    lay.setSpacing(10)
    lay.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

    spinner_row = QHBoxLayout()
    spinner_row.addStretch()
    spinner_row.addWidget(DotsSpinner())
    spinner_row.addStretch()
    lay.addLayout(spinner_row)

    lbl = QLabel(message)
    lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    lbl.setStyleSheet(f"color: {COLOR_TEXT_MUTED}; font-size: 9.5pt;")
    lay.addWidget(lbl)
    return wrap
