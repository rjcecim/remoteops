"""Aba Conectividade — ICMP Ping e TCP Ping via PsPing (diagnóstico).

Não usa o preview, o Console de Saída nem os botões Executar/Parar do PsExec.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from remoteops.utils.dates import format_now_datetime

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLineEdit,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    bind_card_stack,
    grid_in_card,
    make_card_stack,
)
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.spinbox import StepSpinBox
from remoteops.ui.widgets.table import (
    SortableTableItem,
    configure_standard_table,
    pause_table_sorting,
)
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.psping import (
    ALLOWED_ATTEMPTS,
    IpFamily,
    PsPingMode,
    PsPingResult,
    PsPingState,
    run_psping,
    snap_attempts,
    state_caption,
    validate_port,
)

_PRESET_PORTS = (
    (445, "445 — SMB/PsExec"),
    (135, "135 — RPC"),
    (139, "139 — NetBIOS/SMB legado"),
)
_CUSTOM_PORT_SENTINEL = 0


class _ConnectivityWorker(QThread):
    finishedResult = pyqtSignal(object)

    def __init__(
        self,
        *,
        host: str,
        mode: PsPingMode,
        port: Optional[int],
        attempts: int,
        family: IpFamily,
        parent=None,
    ):
        super().__init__(parent)
        self._host = host
        self._mode = mode
        self._port = port
        self._attempts = attempts
        self._family = family
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        result = run_psping(
            self._host,
            mode=self._mode,
            port=self._port,
            attempts=self._attempts,
            family=self._family,
            should_cancel=lambda: self._cancel or self.isInterruptionRequested(),
            use_cache=False,
            log=True,
        )
        self.finishedResult.emit(result)


class ConnectivityTab(QWidget):
    def __init__(
        self,
        parent=None,
        *,
        host_source: Optional[QLineEdit] = None,
        initial_host: str = "",
    ):
        super().__init__(parent)
        self._host_source = host_source
        self._worker: Optional[_ConnectivityWorker] = None
        self._generation = 0
        self._closing = False
        self._syncing_host = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)

        dest = self._build_destination_card()
        self.results_card = self._build_results_card()
        root.addWidget(dest, 0)
        root.addWidget(self.results_card, 1)
        bind_card_stack(root, (dest, self.results_card))

        if self._host_source is not None:
            self._host_source.textChanged.connect(self.sync_from_host)
        host = normalize_host(initial_host) or self._source_host()
        if host:
            self.host_edit.setText(host)
        self._on_mode_changed()
        self._on_port_preset_changed()
        self._refresh_actions()
        self.destroyed.connect(self._abort_worker)

    def _source_host(self) -> str:
        if self._host_source is None or sip.isdeleted(self._host_source):
            return ""
        return normalize_host(self._host_source.text())

    def sync_from_host(self, _text: str = "") -> None:
        if self._closing or not self._ui_alive():
            return
        host = self._source_host()
        self._syncing_host = True
        try:
            if host != normalize_host(self.host_edit.text()):
                self.host_edit.setText(host)
        finally:
            self._syncing_host = False

    def _ui_alive(self) -> bool:
        return not self._closing and not sip.isdeleted(self)

    def _build_destination_card(self) -> CardWidget:
        card = CardWidget("\uEA18", self.tr("Destino"))
        card.set_collapsible(True, collapsed=False)
        g = grid_in_card(card)

        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText(self.tr("Nome ou IP do computador"))
        self.host_edit.setToolTip(self.tr("Herdado da aba PsExec; pode ser alterado aqui."))
        add_row(g, 0, self.tr("Host"), self.host_edit)

        proto_wrap = QWidget()
        proto_row = QHBoxLayout(proto_wrap)
        proto_row.setContentsMargins(0, 0, 0, 0)
        proto_row.setSpacing(16)
        self.radio_icmp = QRadioButton(self.tr("ICMP Ping"))
        self.radio_tcp = QRadioButton(self.tr("TCP Ping"))
        self.radio_icmp.setChecked(True)
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.radio_icmp)
        self._mode_group.addButton(self.radio_tcp)
        self.radio_icmp.toggled.connect(self._on_mode_changed)
        proto_row.addWidget(self.radio_icmp)
        proto_row.addWidget(self.radio_tcp)
        proto_row.addStretch()
        add_row(g, 1, self.tr("Protocolo"), proto_wrap)

        family_wrap = QWidget()
        family_row = QHBoxLayout(family_wrap)
        family_row.setContentsMargins(0, 0, 0, 0)
        family_row.setSpacing(16)
        self.radio_ipv4 = QRadioButton(self.tr("IPv4"))
        self.radio_ipv6 = QRadioButton(self.tr("IPv6"))
        self.radio_ipv4.setChecked(True)
        self._family_group = QButtonGroup(self)
        self._family_group.addButton(self.radio_ipv4)
        self._family_group.addButton(self.radio_ipv6)
        family_row.addWidget(self.radio_ipv4)
        family_row.addWidget(self.radio_ipv6)
        family_row.addStretch()
        add_row(g, 2, self.tr("Família IP"), family_wrap)

        port_wrap = QWidget()
        port_row = QHBoxLayout(port_wrap)
        port_row.setContentsMargins(0, 0, 0, 0)
        port_row.setSpacing(8)
        self.port_combo = FluentComboBox()
        for value, label in _PRESET_PORTS:
            self.port_combo.addItem(label, value)
        self.port_combo.addItem(self.tr("Personalizada"), _CUSTOM_PORT_SENTINEL)
        self.port_combo.currentIndexChanged.connect(self._on_port_preset_changed)
        self.port_spin = StepSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(445)
        self.port_spin.setToolTip(self.tr("Porta TCP (1–65535)"))
        port_row.addWidget(self.port_combo, 1)
        port_row.addWidget(self.port_spin, 0)
        add_row(g, 3, self.tr("Porta TCP"), port_wrap)

        self.attempts_combo = FluentComboBox()
        for n in ALLOWED_ATTEMPTS:
            self.attempts_combo.addItem(str(n), n)
        self.attempts_combo.setCurrentIndex(0)
        add_row(g, 4, self.tr("Tentativas"), self.attempts_combo)
        return card

    def _build_results_card(self) -> CardWidget:
        card = CardWidget("\uE9F9", self.tr("Resultados"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(1)
        card.set_runnable(True)
        card.set_copyable(True)
        card.run_button.setToolTip(self.tr("Testar"))
        card.stop_button.setToolTip(self.tr("Parar"))
        card._copy_btn.setToolTip(self.tr("Copiar resultado"))
        card.runRequested.connect(self.start_test)
        card.stopRequested.connect(self.stop_test)
        card.copyRequested.connect(self.copy_results)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Data e hora"),
                self.tr("Protocolo"),
                self.tr("Destino"),
                self.tr("Porta"),
                self.tr("Tentativas"),
                self.tr("Resultado"),
                self.tr("Detalhe"),
            ]
        )
        configure_standard_table(
            self.table,
            stretch_columns=(6,),
        )
        self.table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.table.setMinimumHeight(80)
        card.content_layout.addWidget(self.table, 1)
        return card

    def _current_mode(self) -> PsPingMode:
        return PsPingMode.TCP if self.radio_tcp.isChecked() else PsPingMode.ICMP

    def _current_family(self) -> IpFamily:
        return IpFamily.IPV6 if self.radio_ipv6.isChecked() else IpFamily.IPV4

    def _current_port(self) -> Optional[int]:
        if self._current_mode() != PsPingMode.TCP:
            return None
        data = self.port_combo.currentData()
        if data == _CUSTOM_PORT_SENTINEL:
            return int(self.port_spin.value())
        try:
            return int(data)
        except (TypeError, ValueError):
            return 445

    def _on_mode_changed(self, _checked: bool = False) -> None:
        tcp = self._current_mode() == PsPingMode.TCP
        self.port_combo.setEnabled(tcp)
        self._on_port_preset_changed()

    def _on_port_preset_changed(self, _index: int = 0) -> None:
        tcp = self._current_mode() == PsPingMode.TCP
        custom = tcp and self.port_combo.currentData() == _CUSTOM_PORT_SENTINEL
        self.port_spin.setEnabled(custom)
        self.port_spin.setVisible(True)
        if tcp and not custom:
            data = self.port_combo.currentData()
            try:
                self.port_spin.setValue(int(data))
            except (TypeError, ValueError):
                pass

    def _busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _refresh_actions(self) -> None:
        busy = self._busy()
        self.results_card.run_button.setEnabled(not busy)
        self.results_card.stop_button.setEnabled(busy)

    def _abort_worker(self, _destroyed: object = None) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            worker.cancel()
        except Exception:
            pass

    def start_test(self) -> None:
        if not self._ui_alive() or self._busy():
            return
        host = normalize_host(self.host_edit.text())
        if not host or not is_valid_host(host):
            self._append_local_error(host, "Host inválido.")
            return
        mode = self._current_mode()
        port = self._current_port()
        if mode == PsPingMode.TCP:
            ok, port_n, err = validate_port(port)
            if not ok:
                self._append_local_error(host, err)
                return
            port = port_n
        attempts = snap_attempts(int(self.attempts_combo.currentData() or 1))
        family = self._current_family()

        self._generation += 1
        gen = self._generation
        self._worker = _ConnectivityWorker(
            host=host,
            mode=mode,
            port=port,
            attempts=attempts,
            family=family,
            parent=self,
        )
        self._worker.finishedResult.connect(
            lambda result, g=gen: self._on_result(result, g)
        )
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        self._refresh_actions()

    def stop_test(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.cancel()
        self._refresh_actions()

    def _on_worker_finished(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            worker.deleteLater()
        if self._ui_alive():
            self._refresh_actions()

    def _on_result(self, result: object, generation: int) -> None:
        if generation != self._generation or not self._ui_alive():
            return
        if not isinstance(result, PsPingResult):
            return
        self._append_result(result)

    def _append_local_error(self, host: str, message: str) -> None:
        fake = PsPingResult(
            host=normalize_host(host),
            mode=self._current_mode(),
            state=PsPingState.EXECUTION_ERROR,
            port=self._current_port() if self._current_mode() == PsPingMode.TCP else None,
            attempts=snap_attempts(int(self.attempts_combo.currentData() or 1)),
            ip_family=self._current_family(),
            message=message,
        )
        self._append_result(fake)

    def _append_result(self, result: PsPingResult) -> None:
        now = datetime.now()
        stamp = format_now_datetime()
        proto = "TCP" if result.mode == PsPingMode.TCP else "ICMP"
        port = str(result.port) if result.port else "—"
        outcome = state_caption(result.state)
        if result.state == PsPingState.CANCELLED:
            outcome = self.tr("Cancelado")
        detail = result.message or ""
        row_values = (
            stamp,
            proto,
            result.host,
            port,
            str(result.attempts),
            outcome,
            detail,
        )
        with pause_table_sorting(self.table):
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, text in enumerate(row_values):
                if col == 0:
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, now.timestamp())
                elif col == 4:
                    item = SortableTableItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, int(result.attempts))
                else:
                    item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row, col, item)
        self.table.scrollToBottom()

    def copy_results(self) -> None:
        if not self._ui_alive():
            return
        lines = [
            "\t".join(
                [
                    self.tr("Data e hora"),
                    self.tr("Protocolo"),
                    self.tr("Destino"),
                    self.tr("Porta"),
                    self.tr("Tentativas"),
                    self.tr("Resultado"),
                    self.tr("Detalhe"),
                ]
            )
        ]
        selected = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        rows = sorted({idx.row() for idx in selected}) if selected else list(
            range(self.table.rowCount())
        )
        for row in rows:
            cells = []
            for col in range(self.table.columnCount()):
                item = self.table.item(row, col)
                cells.append(item.text() if item is not None else "")
            lines.append("\t".join(cells))
        QApplication.clipboard().setText("\n".join(lines))

    def shutdown(self, wait_ms: int = 4000) -> None:
        self._closing = True
        self._generation += 1
        if self._host_source is not None:
            try:
                self._host_source.textChanged.disconnect(self.sync_from_host)
            except TypeError:
                pass
        worker = self._worker
        if worker is not None:
            try:
                worker.finishedResult.disconnect()
            except TypeError:
                pass
            try:
                worker.finished.disconnect(self._on_worker_finished)
            except TypeError:
                pass
            if worker.isRunning():
                worker.cancel()
                worker.wait(max(0, int(wait_ms)))
            self._worker = None
