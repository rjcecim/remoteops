"""Card reutilizável de faixa de IP, exclusões e threads de varredura.

Usado na aba Configurações hoje; outras abas podem embutir o mesmo card.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import COLOR_ACCENT, COLOR_BORDER_HOVER, SIZE_UI_SMALL
from remoteops.ui.widgets.card import CardWidget, add_row, grid_in_card
from remoteops.utils.app_settings import SettingsWriteError
from remoteops.utils.network_range import (
    DEFAULT_SCAN_THREADS,
    MAX_SCAN_THREADS,
    MIN_SCAN_THREADS,
    get_network_range_config,
    network_range_search_mode,
    set_network_range_config,
    snap_scan_threads,
)


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


class NetworkRangeConfigWidget(CardWidget):
    """Um card: intervalo de IP, sub-redes ignoradas e desempenho."""

    configChanged = pyqtSignal()
    saveFailed = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__("\uE968", "Faixa de IP", parent)
        self._title_label.setText(self.tr("Faixa de IP"))
        self.set_collapsible(True, collapsed=False)
        self.set_resettable(True, self.tr("Restaurar padrões deste card"))
        self.resetRequested.connect(self.reset_to_defaults)
        self._saving = False

        g = grid_in_card(self)
        row = 0

        self.start_ip_edit = QLineEdit()
        self.start_ip_edit.setPlaceholderText(self.tr("Ex.: 192.168.1.1"))
        self.start_ip_edit.setToolTip(self.tr("Primeiro endereço IPv4 da faixa (inclusivo)"))
        self.start_ip_edit.editingFinished.connect(self._on_ips_edited)
        add_row(g, row, self.tr("IP de início"), self.start_ip_edit)
        row += 1

        self.end_ip_edit = QLineEdit()
        self.end_ip_edit.setPlaceholderText(self.tr("Ex.: 192.168.1.255"))
        self.end_ip_edit.setToolTip(self.tr("Último endereço IPv4 da faixa (inclusivo)"))
        self.end_ip_edit.editingFinished.connect(self._on_ips_edited)
        add_row(g, row, self.tr("IP de fim"), self.end_ip_edit)
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

        self.reload()

    def reload(self) -> None:
        """Lê settings.ini / runtime e atualiza os campos."""
        cfg = get_network_range_config()
        self._saving = True
        try:
            self.start_ip_edit.setText(cfg.start_ip)
            self.end_ip_edit.setText(cfg.end_ip)
            self.ignored_edit.setText(cfg.ignored_subnets)
            threads = snap_scan_threads(cfg.scan_threads)
            self.threads_slider.blockSignals(True)
            self.threads_slider.setValue(threads)
            self.threads_slider.blockSignals(False)
            self.threads_value.setText(str(threads))
        finally:
            self._saving = False
        self._refresh_mode_caption()

    def reset_to_defaults(self) -> None:
        """Limpa a faixa, exclusões e volta as threads ao padrão."""
        self._persist(
            start_ip="",
            end_ip="",
            ignored_subnets="",
            scan_threads=DEFAULT_SCAN_THREADS,
        )
        self.reload()

    def _refresh_mode_caption(self) -> None:
        mode, err, count = network_range_search_mode()
        if mode == "network":
            self.mode_caption.setText(
                self.tr(
                    f"Faixa salva ({count} IP(s)): a Pesquisa de Aplicativos varre "
                    "a rede e não usa o hosts.json."
                )
            )
        elif mode == "invalid":
            self.mode_caption.setText(err or self.tr("Faixa inválida."))
        else:
            self.mode_caption.setText(
                self.tr(
                    "Sem faixa válida: a Pesquisa de Aplicativos usa o hosts.json."
                )
            )

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
