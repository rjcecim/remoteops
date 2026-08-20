"""Card: origem dos hosts (faixa de IP ou hosts.json) e varredura.

Usado na aba Configurações; Pesquisa e Instalação em Lote leem esta origem.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import COLOR_ACCENT, COLOR_BORDER_HOVER, SIZE_UI_SMALL, make_icon_button
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    add_row_full_width,
    grid_in_card,
    make_field_label,
)
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.utils.app_settings import SettingsWriteError
from remoteops.utils.hosts import default_hosts_path, load_hosts_file
from remoteops.utils.network_range import (
    DEFAULT_SCAN_THREADS,
    MAX_SCAN_THREADS,
    MIN_SCAN_THREADS,
    get_network_range_config,
    network_range_search_mode,
    set_network_range_config,
    snap_scan_threads,
)
from remoteops.utils.search_settings import resolve_configured_hosts_path, set_search_hosts_path


def _caption(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("networkRangeCaption")
    lbl.setWordWrap(True)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    lbl.setStyleSheet(
        f"QLabel#networkRangeCaption {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
    )
    return lbl


def _open_in_explorer(path: str) -> None:
    target = (path or "").strip()
    if not target:
        return
    try:
        if os.path.isdir(target):
            os.startfile(target)  # type: ignore[attr-defined]
        elif os.path.isfile(target):
            subprocess.run(["explorer", "/select,", target], check=False)
        else:
            parent = os.path.dirname(target) or target
            if os.path.isdir(parent):
                os.startfile(parent)  # type: ignore[attr-defined]
    except Exception:
        pass


class NetworkRangeConfigWidget(CardWidget):
    """Origem dos hosts: faixa de IP (varredura) ou hosts.json."""

    configChanged = pyqtSignal()
    saveFailed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__("\uE968", "Origem dos hosts", parent)
        self._title_label.setText(self.tr("Origem dos hosts"))
        self.set_collapsible(True, collapsed=False)
        self.set_resettable(True, self.tr("Restaurar padrões deste card"))
        self.resetRequested.connect(self.reset_to_defaults)
        self._saving = False

        g = grid_in_card(self)
        row = 0

        self.enabled_check = QCheckBox(self.tr("Usar faixa de IP"))
        self.enabled_check.setToolTip(
            self.tr(
                "Marcada: varre a faixa abaixo. Desmarcada: usa o hosts.json. "
                "Os IPs configurados permanecem salvos."
            )
        )
        self.enabled_check.toggled.connect(self._on_enabled_toggled)
        add_row_full_width(g, row, self.enabled_check)
        row += 1

        self.start_ip_edit = QLineEdit()
        self.start_ip_edit.setPlaceholderText(self.tr("Ex.: 192.168.1.1"))
        self.start_ip_edit.setToolTip(self.tr("Primeiro endereço IPv4 da faixa (inclusivo)"))
        self.start_ip_edit.editingFinished.connect(self._on_ips_edited)

        self.end_ip_edit = QLineEdit()
        self.end_ip_edit.setPlaceholderText(self.tr("Ex.: 192.168.1.255"))
        self.end_ip_edit.setToolTip(self.tr("Último endereço IPv4 da faixa (inclusivo)"))
        self.end_ip_edit.editingFinished.connect(self._on_ips_edited)

        self._end_ip_label = make_field_label(self.tr("IP de fim"))
        self._end_ip_label.setMinimumWidth(0)
        self._end_ip_label.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )

        ips_wrap = QWidget()
        ips_row = QHBoxLayout(ips_wrap)
        ips_row.setContentsMargins(0, 0, 0, 0)
        ips_row.setSpacing(10)
        ips_row.addWidget(self.start_ip_edit, 1)
        ips_row.addWidget(self._end_ip_label, 0, Qt.AlignmentFlag.AlignVCenter)
        ips_row.addWidget(self.end_ip_edit, 1)
        add_row(g, row, self.tr("IP de início"), ips_wrap)
        row += 1

        self.ignored_edit = QLineEdit()
        self.ignored_edit.setPlaceholderText(self.tr("Ex.: 1; 2; 3"))
        self.ignored_edit.setToolTip(
            self.tr("Terceiro octeto a excluir da faixa. Separe vários com ;")
        )
        self.ignored_edit.editingFinished.connect(self._on_ignored_edited)
        add_row(g, row, self.tr("3º octeto (separar com ;)"), self.ignored_edit)
        row += 1

        ignore_cap = _caption(
            self.tr("Ex.: 9 ignora 192.168.9.0–254. Use ; para várias.")
        )
        g.addWidget(ignore_cap, row, 0, 1, 2)
        row += 1

        threads_wrap = QWidget()
        threads_lay = QVBoxLayout(threads_wrap)
        threads_lay.setContentsMargins(0, 0, 0, 0)
        threads_lay.setSpacing(4)

        value_row = QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.addStretch(1)
        self.threads_value = QLabel(str(DEFAULT_SCAN_THREADS))
        self.threads_value.setObjectName("scanThreadsValue")
        self.threads_value.setStyleSheet(
            "QLabel#scanThreadsValue { color: palette(highlight); font-weight: 600; }"
        )
        value_row.addWidget(self.threads_value, 0, Qt.AlignmentFlag.AlignVCenter)
        threads_lay.addLayout(value_row)

        self.threads_slider = QSlider(Qt.Orientation.Horizontal)
        self.threads_slider.setRange(MIN_SCAN_THREADS, MAX_SCAN_THREADS)
        self.threads_slider.setSingleStep(10)
        self.threads_slider.setPageStep(10)
        self.threads_slider.setTickInterval(10)
        self.threads_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.threads_slider.setToolTip(
            self.tr(
                "Quantidade de IPs verificados ao mesmo tempo na varredura de rede "
                f"(padrão {DEFAULT_SCAN_THREADS})."
            )
        )
        self.threads_slider.setStyleSheet(
            f"""
            QSlider::groove:horizontal {{
                height: 4px;
                background: {COLOR_BORDER_HOVER};
                border-radius: 2px;
            }}
            QSlider::sub-page:horizontal {{
                background: {COLOR_ACCENT};
                border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                width: 14px;
                height: 14px;
                margin: -6px 0;
                border-radius: 7px;
                background: {COLOR_ACCENT};
            }}
            """
        )
        self.threads_slider.valueChanged.connect(self._on_threads_changed)
        self.threads_slider.sliderReleased.connect(self._persist_threads)
        threads_lay.addWidget(self.threads_slider)

        ticks = QHBoxLayout()
        ticks.setContentsMargins(0, 0, 0, 0)
        ticks.addWidget(_caption(str(MIN_SCAN_THREADS)))
        ticks.addStretch(1)
        ticks.addWidget(_caption(str(MAX_SCAN_THREADS)))
        threads_lay.addLayout(ticks)

        add_row(g, row, self.tr("Threads simultâneas"), threads_wrap)
        row += 1

        self.mode_caption = _caption("")
        g.addWidget(self.mode_caption, row, 0, 1, 2)
        row += 1

        hosts_row = QHBoxLayout()
        hosts_row.setSpacing(4)
        hosts_row.setContentsMargins(0, 0, 0, 0)
        self.hosts_edit = QLineEdit()
        self.hosts_edit.setReadOnly(True)
        self.hosts_edit.setToolTip(
            self.tr(
                "Lista de computadores da Pesquisa de Aplicativos e da "
                "Instalação em Lote quando a faixa de IP estiver desativada."
            )
        )
        self.hosts_browse_btn = make_icon_button(
            "\uED25", self.tr("Selecionar outro hosts.json")
        )
        self.hosts_browse_btn.clicked.connect(self._browse_hosts_file)
        self.hosts_open_btn = make_icon_button(
            "\uED43", self.tr("Abrir pasta do hosts.json")
        )
        self.hosts_open_btn.clicked.connect(
            lambda: _open_in_explorer(self.hosts_edit.text())
        )
        hosts_row.addWidget(self.hosts_edit, 1)
        hosts_row.addWidget(self.hosts_browse_btn)
        hosts_row.addWidget(self.hosts_open_btn)
        hosts_wrap = QWidget()
        hosts_wrap.setLayout(hosts_row)
        add_row(g, row, self.tr("hosts.json"), hosts_wrap)
        self._hosts_row_label = g.itemAtPosition(row, 0).widget()
        self._hosts_wrap = hosts_wrap
        row += 1

        hosts_status_row = QHBoxLayout()
        hosts_status_row.setSpacing(8)
        hosts_status_row.setContentsMargins(2, 0, 0, 0)
        self.hosts_status_dot = _StatusDot()
        self.hosts_status_label = QLabel()
        self.hosts_status_label.setObjectName("hostsStatus")
        self.hosts_status_label.setStyleSheet(
            f"QLabel#hostsStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        hosts_status_row.addWidget(
            self.hosts_status_dot, 0, Qt.AlignmentFlag.AlignVCenter
        )
        hosts_status_row.addWidget(
            self.hosts_status_label, 0, Qt.AlignmentFlag.AlignVCenter
        )
        hosts_status_row.addStretch()
        hosts_status_wrap = QWidget()
        hosts_status_wrap.setLayout(hosts_status_row)
        add_row(g, row, self.tr("Status"), hosts_status_wrap)
        self._hosts_status_label_w = g.itemAtPosition(row, 0).widget()
        self._hosts_status_wrap = hosts_status_wrap

        self.reload()

    def reload(self) -> None:
        """Lê settings.ini / runtime e atualiza os campos."""
        cfg = get_network_range_config()
        self._saving = True
        try:
            self.enabled_check.blockSignals(True)
            self.enabled_check.setChecked(bool(cfg.enabled))
            self.enabled_check.blockSignals(False)
            self.start_ip_edit.setText(cfg.start_ip)
            self.end_ip_edit.setText(cfg.end_ip)
            self.ignored_edit.setText(cfg.ignored_subnets)
            threads = snap_scan_threads(cfg.scan_threads)
            self.threads_slider.blockSignals(True)
            self.threads_slider.setValue(threads)
            self.threads_slider.blockSignals(False)
            self.threads_value.setText(str(threads))
            self._apply_enabled_state(bool(cfg.enabled))
            self._refresh_hosts_ui()
        finally:
            self._saving = False
        self._refresh_mode_caption()

    def reset_to_defaults(self) -> None:
        """Limpa a faixa, exclusões e volta as threads ao padrão."""
        self._persist(
            enabled=True,
            start_ip="",
            end_ip="",
            ignored_subnets="",
            scan_threads=DEFAULT_SCAN_THREADS,
        )
        self.reload()

    def _apply_enabled_state(self, enabled: bool) -> None:
        for widget in (
            self.start_ip_edit,
            self.end_ip_edit,
            self._end_ip_label,
            self.ignored_edit,
            self.threads_slider,
            self.threads_value,
        ):
            widget.setEnabled(enabled)
        for widget in (
            self.hosts_edit,
            self.hosts_browse_btn,
            self.hosts_open_btn,
            self._hosts_row_label,
            self._hosts_wrap,
            self.hosts_status_dot,
            self.hosts_status_label,
            self._hosts_status_label_w,
            self._hosts_status_wrap,
        ):
            widget.setEnabled(not enabled)

    def _refresh_mode_caption(self) -> None:
        cfg = get_network_range_config()
        if not cfg.enabled:
            self.mode_caption.setText(
                self.tr(
                    "Faixa desativada: a Pesquisa de Aplicativos e a "
                    "Instalação em Lote usam o hosts.json. "
                    "Os IPs configurados permanecem salvos."
                )
            )
            return
        mode, err, count = network_range_search_mode()
        if mode == "network":
            self.mode_caption.setText(
                self.tr(
                    f"Faixa ativa ({count} IP(s)): a Pesquisa de Aplicativos e a "
                    "Instalação em Lote varrem a rede e não usam o hosts.json."
                )
            )
        elif mode == "invalid":
            self.mode_caption.setText(err or self.tr("Faixa inválida."))
        else:
            self.mode_caption.setText(
                self.tr(
                    "Sem faixa válida: a Pesquisa de Aplicativos e a "
                    "Instalação em Lote usam o hosts.json."
                )
            )

    def _on_enabled_toggled(self, checked: bool) -> None:
        self._apply_enabled_state(bool(checked))
        self._persist(enabled=bool(checked))

    def _persist(self, **kwargs) -> None:
        if self._saving:
            return
        try:
            set_network_range_config(**kwargs)
        except SettingsWriteError as exc:
            self.reload()
            self.saveFailed.emit(exc)
            return
        self._refresh_mode_caption()
        self.configChanged.emit()

    def _on_ips_edited(self) -> None:
        self._persist(
            start_ip=self.start_ip_edit.text(),
            end_ip=self.end_ip_edit.text(),
        )

    def _on_ignored_edited(self) -> None:
        self._persist(ignored_subnets=self.ignored_edit.text())

    def _on_threads_changed(self, value: int) -> None:
        self.threads_value.setText(str(snap_scan_threads(value)))
        if not self.threads_slider.isSliderDown():
            self._persist_threads()

    def _persist_threads(self) -> None:
        snapped = snap_scan_threads(int(self.threads_slider.value()))
        if int(self.threads_slider.value()) != snapped:
            self.threads_slider.blockSignals(True)
            self.threads_slider.setValue(snapped)
            self.threads_slider.blockSignals(False)
        self.threads_value.setText(str(snapped))
        self._persist(scan_threads=snapped)

    def _refresh_hosts_ui(self) -> None:
        path, origin = resolve_configured_hosts_path()
        self.hosts_edit.setText(path or default_hosts_path())
        self._set_hosts_status(origin, path)

    def _browse_hosts_file(self) -> None:
        start = self.hosts_edit.text().strip() or default_hosts_path()
        if start and not os.path.isdir(os.path.dirname(start)):
            start = default_hosts_path()
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Selecionar arquivo de hosts"),
            start,
            self.tr("JSON (*.json)"),
        )
        if not path:
            return
        try:
            set_search_hosts_path(path)
        except SettingsWriteError as exc:
            self.saveFailed.emit(exc)
            return
        self._refresh_hosts_ui()
        self.configChanged.emit()

    def _apply_hosts_status(self, state: str, text: str) -> None:
        color = _STATUS_COLORS.get(state, _STATUS_COLORS["idle"])
        self.hosts_status_dot.set_color(color)
        self.hosts_status_label.setText(text)
        self.hosts_status_dot.setToolTip(text)
        self.hosts_status_label.setToolTip(text)

    def _set_hosts_status(self, origin: str, path: Optional[str]) -> None:
        if origin == "missing" or not path or not os.path.isfile(path):
            self._apply_hosts_status("err", self.tr("Não encontrado"))
            return
        p = os.path.normpath(path)
        if len(p) >= 2 and p[1] == ":":
            p = p[0].upper() + p[1:]
        try:
            hosts = load_hosts_file(p)
        except Exception:
            self._apply_hosts_status("invalid", self.tr("Arquivo inválido"))
            return
        if not hosts:
            self._apply_hosts_status("warn", self.tr("Encontrado — lista vazia"))
            return
        self._apply_hosts_status(
            "ok", self.tr(f"Encontrado — {len(hosts)} host(s)")
        )
