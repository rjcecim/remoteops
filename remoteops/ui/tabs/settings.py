from __future__ import annotations

import os
import subprocess

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.style import SIZE_UI_SMALL, make_icon_button
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    add_row_full_width,
    finish_card_stack,
    grid_in_card,
    make_card_stack,
)
from remoteops.ui.widgets.content_tab_widget import ContentSizedTabWidget
from remoteops.ui.widgets.flow import FlowLayout
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.network_range import NetworkRangeConfigWidget
from remoteops.ui.widgets.spinbox import StepSpinBox
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.utils.app_logging import (
    get_log_dir,
    is_file_logging_enabled,
    set_file_logging_enabled,
)
from remoteops.utils.app_settings import SETTINGS_SAVE_ERROR_MSG, SettingsWriteError
from remoteops.utils.printer_settings import (
    DEFAULT_PRINT_LIST_TIMEOUT_S,
    DEFAULT_PRINT_SERVER,
    MAX_PRINT_LIST_TIMEOUT_S,
    MIN_PRINT_LIST_TIMEOUT_S,
    PRINT_SERVER_PLACEHOLDER,
    get_print_list_timeout,
    get_print_server,
    parse_print_server_input,
    set_print_list_timeout,
    set_print_server,
)
from remoteops.utils.printers import print_server_unc
from remoteops.utils.pstools import (
    DEFAULT_PSTOOLS_DIR,
    get_pstools_dir,
    probe_pstools,
    probe_rustdesk_local,
    set_pstools_dir,
)
from remoteops.utils.remote_registry_query import (
    MAX_REMOTE_REGISTRY_TIMEOUT_SECONDS,
    MIN_REMOTE_REGISTRY_TIMEOUT_SECONDS,
    REMOTE_REGISTRY_TIMEOUT_SECONDS,
    get_remote_registry_timeout,
    set_remote_registry_timeout,
)
from remoteops.utils.search_settings import (
    DEFAULT_SEARCH_MAX_WORKERS,
    MAX_SEARCH_MAX_WORKERS,
    MIN_SEARCH_MAX_WORKERS,
    get_search_max_workers,
    set_search_max_workers,
)


def _caption(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("settingsCaption")
    lbl.setWordWrap(True)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    lbl.setMinimumWidth(0)
    lbl.setStyleSheet(
        f"QLabel#settingsCaption {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
    )
    return lbl


def _add_caption(grid, row: int, text: str) -> None:
    """Legenda em largura total (sem AlignLeft, senão o texto é cortado)."""
    grid.addWidget(_caption(text), row, 0, 1, 2)


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


def _wrap_page(*widgets: QWidget) -> QWidget:
    """Página de aba interna: altura = card (sem vão vazio abaixo)."""
    page = QWidget()
    page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
    page.setMinimumWidth(0)
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    for widget in widgets:
        widget.setMinimumWidth(0)
        lay.addWidget(widget, 0)
    return page


class SettingsTab(QWidget):
    """Aba de configurações do aplicativo."""

    pstoolsPathChanged = pyqtSignal(str)
    networkRangeChanged = pyqtSignal()
    printServerChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._tools_flow: FlowLayout | None = None

        self._root_layout = make_card_stack(self)
        self._tabs = self._build_tabs()
        self._root_layout.addWidget(self._tabs, 0)
        self._printers_card = self._build_printers_card()
        self._root_layout.addWidget(self._printers_card, 0)

        self.network_range = NetworkRangeConfigWidget(self)
        self.network_range.configChanged.connect(self.networkRangeChanged.emit)
        self.network_range.saveFailed.connect(self._show_settings_save_error)
        self._root_layout.addWidget(self.network_range, 0)
        self._search_card = self._build_search_card()
        self._root_layout.addWidget(self._search_card, 0)
        finish_card_stack(self._root_layout)

        for card in (
            self._pstools_card,
            self._rustdesk_card,
            self._logs_card,
            self._printers_card,
            self.network_range,
            self._search_card,
        ):
            card.collapsedChanged.connect(self._on_settings_card_collapsed)

        self.refresh_pstools_status()
        self.refresh_rustdesk_status()
        self._reload_printer_fields()

    def _build_tabs(self) -> ContentSizedTabWidget:
        tabs = ContentSizedTabWidget()
        inner_bar = Mdl2TabBar(tabs)
        inner_bar.setExpanding(False)
        tabs.setTabBar(inner_bar)
        tabs.setUsesScrollButtons(True)
        tabs.setMinimumWidth(0)

        self._pstools_card = self._build_pstools_card()
        idx_ps = tabs.addTab(_wrap_page(self._pstools_card), self.tr("PSTools"))
        inner_bar.set_tab_meta(idx_ps, "\uE8B7")
        tabs.setTabToolTip(
            idx_ps, self.tr("Pasta do PsExec, PsInfo, PsPing, PsList, PsService e utilitários")
        )

        self._rustdesk_card = self._build_rustdesk_card()
        idx_rd = tabs.addTab(_wrap_page(self._rustdesk_card), self.tr("RustDesk"))
        inner_bar.set_tab_meta(idx_rd, "\uE774")
        tabs.setTabToolTip(idx_rd, self.tr("Instalação local do RustDesk"))

        self._logs_card = self._build_logs_card()
        idx_logs = tabs.addTab(_wrap_page(self._logs_card), self.tr("Logs"))
        inner_bar.set_tab_meta(idx_logs, "\uE7C3")
        tabs.setTabToolTip(idx_logs, self.tr("Gravação do log em arquivo"))
        inner_bar.refresh_layout()
        tabs.currentChanged.connect(self._on_inner_tab_changed)
        return tabs

    def _on_inner_tab_changed(self, _index: int) -> None:
        self._tabs.sync_content_height()
        self._root_layout.activate()
        self.updateGeometry()

    def _on_settings_card_collapsed(self, _collapsed: bool = False) -> None:
        self._tabs.sync_content_height()
        self._root_layout.activate()
        self.updateGeometry()

    def _build_pstools_card(self) -> CardWidget:
        card_ps = CardWidget("\uE8B7", self.tr("PSTools"))
        card_ps.set_collapsible(True, collapsed=False)
        card_ps.set_resettable(True, self.tr("Restaurar padrões deste card"))
        card_ps.resetRequested.connect(self._reset_pstools)
        g1 = grid_in_card(card_ps)
        row = 0

        path_row = QHBoxLayout()
        path_row.setSpacing(4)
        path_row.setContentsMargins(0, 0, 0, 0)
        self.pstools_edit = QLineEdit()
        self.pstools_edit.setReadOnly(True)
        self.pstools_edit.setText(get_pstools_dir())
        self.pstools_edit.setToolTip(
            self.tr("Pasta onde estão PsExec, PsInfo, PsPing, PsList e utilitários")
        )
        self.pstools_browse_btn = make_icon_button(
            "\uED25", self.tr("Alterar pasta PSTools")
        )
        self.pstools_browse_btn.clicked.connect(self._browse_pstools)
        self.pstools_open_btn = make_icon_button(
            "\uED43", self.tr("Abrir pasta no Explorer")
        )
        self.pstools_open_btn.clicked.connect(self._open_pstools_folder)
        path_row.addWidget(self.pstools_edit, 1)
        path_row.addWidget(self.pstools_browse_btn)
        path_row.addWidget(self.pstools_open_btn)
        path_wrap = QWidget()
        path_wrap.setLayout(path_row)
        path_wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        add_row(g1, row, self.tr("Caminho"), path_wrap)
        row += 1

        status_wrap = QWidget()
        status_wrap.setMinimumWidth(0)
        self._tools_flow = FlowLayout(status_wrap, margin=0, h_spacing=16, v_spacing=4)
        add_row(g1, row, self.tr("Status"), status_wrap)
        return card_ps

    def _build_rustdesk_card(self) -> CardWidget:
        card_rd = CardWidget("\uE774", self.tr("RustDesk"))
        card_rd.set_collapsible(True, collapsed=False)
        card_rd.set_resettable(True, self.tr("Atualizar status do RustDesk"))
        card_rd.resetRequested.connect(self.refresh_rustdesk_status)
        g_rd = grid_in_card(card_rd)

        rd_status_row = QHBoxLayout()
        rd_status_row.setSpacing(8)
        rd_status_row.setContentsMargins(2, 0, 0, 0)
        self.rustdesk_status_dot = _StatusDot()
        self.rustdesk_status_label = QLabel()
        self.rustdesk_status_label.setObjectName("rustdeskStatus")
        self.rustdesk_status_label.setStyleSheet(
            f"QLabel#rustdeskStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        rd_status_row.addWidget(
            self.rustdesk_status_dot, 0, Qt.AlignmentFlag.AlignVCenter
        )
        rd_status_row.addWidget(
            self.rustdesk_status_label, 0, Qt.AlignmentFlag.AlignVCenter
        )
        rd_status_row.addStretch()
        rd_status_wrap = QWidget()
        rd_status_wrap.setLayout(rd_status_row)
        add_row(g_rd, 0, self.tr("Status"), rd_status_wrap)

        rd_path_row = QHBoxLayout()
        rd_path_row.setSpacing(4)
        rd_path_row.setContentsMargins(0, 0, 0, 0)
        self.rustdesk_edit = QLineEdit()
        self.rustdesk_edit.setReadOnly(True)
        self.rustdesk_open_btn = make_icon_button(
            "\uED43", self.tr("Abrir pasta do RustDesk")
        )
        self.rustdesk_open_btn.clicked.connect(self._open_rustdesk_folder)
        rd_path_row.addWidget(self.rustdesk_edit, 1)
        rd_path_row.addWidget(self.rustdesk_open_btn)
        rd_path_wrap = QWidget()
        rd_path_wrap.setLayout(rd_path_row)
        add_row(g_rd, 1, self.tr("Caminho"), rd_path_wrap)
        _add_caption(
            g_rd,
            2,
            self.tr(
                "Instalação local em C:\\Program Files\\RustDesk\\ "
                "(não fica na pasta PSTools)."
            ),
        )
        return card_rd

    def _build_logs_card(self) -> CardWidget:
        card_logs = CardWidget("\uE7C3", self.tr("Logs"))
        card_logs.set_collapsible(True, collapsed=False)
        g2 = grid_in_card(card_logs)

        self.log_session_check = QCheckBox(self.tr("Salvar log em arquivo"))
        self.log_session_check.setChecked(is_file_logging_enabled())
        self.log_session_check.setToolTip(
            self.tr(
                "Marque para gravar as operações em arquivo "
                "(preferência salva no settings.ini)."
            )
        )
        self.log_session_check.toggled.connect(self._on_log_session_toggled)
        add_row_full_width(g2, 0, self.log_session_check)

        logs_row = QHBoxLayout()
        logs_row.setSpacing(4)
        logs_row.setContentsMargins(0, 0, 0, 0)
        self.logs_edit = QLineEdit()
        self.logs_edit.setReadOnly(True)
        try:
            self.logs_edit.setText(get_log_dir(create=False))
        except Exception:
            self.logs_edit.setText("")
        self.logs_open_btn = make_icon_button("\uED43", self.tr("Abrir pasta de logs"))
        self.logs_open_btn.clicked.connect(self._open_logs_folder)
        logs_row.addWidget(self.logs_edit, 1)
        logs_row.addWidget(self.logs_open_btn)
        logs_wrap = QWidget()
        logs_wrap.setLayout(logs_row)
        add_row(g2, 1, self.tr("Pasta"), logs_wrap)
        _add_caption(
            g2,
            2,
            self.tr(
                "Desmarcado: não salva em disco. "
                "O log na parte de baixo da janela continua aparecendo."
            ),
        )
        return card_logs

    def _build_printers_card(self) -> CardWidget:
        card = CardWidget("\uE749", self.tr("Configurações de Impressoras"))
        card.set_collapsible(True, collapsed=False)
        card.set_resettable(True, self.tr("Restaurar padrões deste card"))
        card.resetRequested.connect(self._reset_printers_card)
        grid = grid_in_card(card)

        self.print_server_edit = QLineEdit()
        self.print_server_edit.setPlaceholderText(PRINT_SERVER_PLACEHOLDER)
        self.print_server_edit.setToolTip(
            self.tr(
                "Servidor usado pelos comandos de gerenciamento de impressoras. "
                "Vazio até você informar o host. O texto de exemplo no campo "
                "não é gravado."
            )
        )
        self.print_server_edit.editingFinished.connect(
            self._on_print_server_editing_finished
        )
        add_row(grid, 0, self.tr("Servidor de impressão"), self.print_server_edit)

        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(4)
        timeout_row.setContentsMargins(0, 0, 0, 0)
        self.print_timeout_spin = StepSpinBox()
        self.print_timeout_spin.setRange(
            MIN_PRINT_LIST_TIMEOUT_S, MAX_PRINT_LIST_TIMEOUT_S
        )
        self.print_timeout_spin.setSingleStep(15)
        self.print_timeout_spin.setSuffix(self.tr(" s"))
        self.print_timeout_spin.setToolTip(
            self.tr(
                "Tempo máximo da listagem Get-Printer neste computador "
                f"(padrão {DEFAULT_PRINT_LIST_TIMEOUT_S} s)."
            )
        )
        self.print_timeout_spin.valueChanged.connect(self._on_print_timeout_changed)
        timeout_row.addWidget(self.print_timeout_spin)
        timeout_row.addStretch()
        timeout_wrap = QWidget()
        timeout_wrap.setLayout(timeout_row)
        add_row(grid, 1, self.tr("Tempo da consulta"), timeout_wrap)

        self.print_unc_lbl = _caption("")
        grid.addWidget(self.print_unc_lbl, 2, 0, 1, 2)
        _add_caption(
            grid,
            3,
            self.tr(
                "A aba Impressoras consulta este servidor com Get-Printer "
                "nesta máquina (sem PsExec). A instalação remota continua "
                "via PsExec no host alvo. UNC: \\\\servidor\\compartilhamento."
            ),
        )
        ref_w = self.print_timeout_spin.sizeHint().width()
        self.print_timeout_spin.setFixedWidth(max(ref_w, 72))
        return card

    def _build_search_card(self) -> CardWidget:
        card_search = CardWidget("\uE71D", self.tr("Remote Registry"))
        card_search.set_collapsible(True, collapsed=False)
        card_search.set_resettable(True, self.tr("Restaurar padrões deste card"))
        card_search.resetRequested.connect(self._reset_search_card)
        g3 = grid_in_card(card_search)

        workers_row = QHBoxLayout()
        workers_row.setSpacing(4)
        workers_row.setContentsMargins(0, 0, 0, 0)
        self.search_workers_spin = StepSpinBox()
        self.search_workers_spin.setRange(MIN_SEARCH_MAX_WORKERS, MAX_SEARCH_MAX_WORKERS)
        self.search_workers_spin.setSingleStep(1)
        self.search_workers_spin.setToolTip(
            self.tr(
                "Máximo de consultas Remote Registry em paralelo "
                f"(padrão {DEFAULT_SEARCH_MAX_WORKERS}). "
                "Vale na Pesquisa de Aplicativos e na Instalação em Lote."
            )
        )
        self.search_workers_spin.blockSignals(True)
        self.search_workers_spin.setValue(get_search_max_workers())
        self.search_workers_spin.blockSignals(False)
        self.search_workers_spin.valueChanged.connect(self._on_search_workers_changed)
        workers_row.addWidget(self.search_workers_spin)
        workers_row.addStretch()
        workers_wrap = QWidget()
        workers_wrap.setLayout(workers_row)
        add_row(g3, 0, self.tr("Consultas simultâneas"), workers_wrap)
        _add_caption(
            g3,
            1,
            self.tr(
                "Quantos computadores são consultados ao mesmo tempo via "
                "Remote Registry. Vale na Pesquisa de Aplicativos e na "
                "Instalação em Lote. Valores maiores aceleram a detecção e "
                "aumentam as conexões. A alteração vale na próxima consulta."
            ),
        )

        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(4)
        timeout_row.setContentsMargins(0, 0, 0, 0)
        self.rr_timeout_spin = StepSpinBox()
        self.rr_timeout_spin.setRange(
            int(MIN_REMOTE_REGISTRY_TIMEOUT_SECONDS),
            int(MAX_REMOTE_REGISTRY_TIMEOUT_SECONDS),
        )
        self.rr_timeout_spin.setSuffix(self.tr(" s"))
        self.rr_timeout_spin.setToolTip(
            self.tr(
                "Tempo máximo por computador na consulta Remote Registry "
                f"(padrão {int(REMOTE_REGISTRY_TIMEOUT_SECONDS)} s). "
                "Vale na Pesquisa, na aba Aplicativos e na Instalação em Lote."
            )
        )
        self.rr_timeout_spin.blockSignals(True)
        self.rr_timeout_spin.setValue(int(get_remote_registry_timeout()))
        self.rr_timeout_spin.blockSignals(False)
        self.rr_timeout_spin.valueChanged.connect(self._on_rr_timeout_changed)
        timeout_row.addWidget(self.rr_timeout_spin)
        timeout_row.addStretch()
        timeout_wrap = QWidget()
        timeout_wrap.setLayout(timeout_row)
        add_row(g3, 2, self.tr("Timeout Remote Registry"), timeout_wrap)
        _add_caption(
            g3,
            3,
            self.tr(
                "Limite por computador ao enumerar aplicativos no registro remoto. "
                "Vale na Pesquisa de Aplicativos, na aba Aplicativos e na "
                "Instalação em Lote. A alteração vale na próxima consulta."
            ),
        )
        ref_w = self.rr_timeout_spin.sizeHint().width()
        self.rr_timeout_spin.setFixedWidth(ref_w)
        self.search_workers_spin.setFixedWidth(ref_w)
        return card_search

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_pstools_status()
        self.refresh_rustdesk_status()
        self.log_session_check.setChecked(is_file_logging_enabled())
        try:
            self.logs_edit.setText(get_log_dir(create=False))
        except Exception:
            pass
        self.network_range.reload()
        self.search_workers_spin.blockSignals(True)
        self.search_workers_spin.setValue(get_search_max_workers())
        self.search_workers_spin.blockSignals(False)
        self.rr_timeout_spin.blockSignals(True)
        self.rr_timeout_spin.setValue(int(get_remote_registry_timeout()))
        self.rr_timeout_spin.blockSignals(False)
        self._reload_printer_fields()
        self._tabs.sync_content_height()
        self._root_layout.activate()

    def _reload_printer_fields(self) -> None:
        self.print_server_edit.blockSignals(True)
        self.print_server_edit.setText(get_print_server())
        self.print_server_edit.blockSignals(False)
        self.print_timeout_spin.blockSignals(True)
        self.print_timeout_spin.setValue(get_print_list_timeout())
        self.print_timeout_spin.blockSignals(False)
        self._refresh_print_unc_preview()

    def _refresh_print_unc_preview(self) -> None:
        server = get_print_server()
        if not server:
            self.print_unc_lbl.setText(
                self.tr("Nenhum servidor configurado. Informe o host para montar o UNC.")
            )
            return
        unc = print_server_unc(server)
        self.print_unc_lbl.setText(self.tr(f"UNC do servidor: {unc}"))

    def _show_settings_save_error(self, exc: BaseException | None = None) -> None:
        msg = SETTINGS_SAVE_ERROR_MSG
        if isinstance(exc, SettingsWriteError) and getattr(exc, "message", None):
            msg = exc.message
        QMessageBox.warning(self, self.tr("Configurações"), self.tr(msg))

    def _on_print_server_editing_finished(self) -> None:
        try:
            parsed = parse_print_server_input(self.print_server_edit.text())
        except ValueError:
            QMessageBox.warning(
                self,
                self.tr("Configurações"),
                self.tr("Servidor de impressão inválido."),
            )
            self.print_server_edit.setText(get_print_server())
            return
        if parsed.casefold() == get_print_server().casefold():
            self.print_server_edit.setText(get_print_server())
            self._refresh_print_unc_preview()
            return
        try:
            saved = set_print_server(parsed)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.print_server_edit.setText(get_print_server())
            return
        self.print_server_edit.setText(saved)
        self._refresh_print_unc_preview()
        self.printServerChanged.emit()

    def _on_print_timeout_changed(self, value: int) -> None:
        try:
            set_print_list_timeout(value)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.print_timeout_spin.blockSignals(True)
            self.print_timeout_spin.setValue(get_print_list_timeout())
            self.print_timeout_spin.blockSignals(False)

    def _reset_printers_card(self) -> None:
        try:
            server = set_print_server(DEFAULT_PRINT_SERVER)
            timeout = set_print_list_timeout(DEFAULT_PRINT_LIST_TIMEOUT_S)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self._reload_printer_fields()
            return
        self.print_server_edit.setText(server)
        self.print_timeout_spin.blockSignals(True)
        self.print_timeout_spin.setValue(timeout)
        self.print_timeout_spin.blockSignals(False)
        self._refresh_print_unc_preview()
        self.printServerChanged.emit()

    def _reset_search_card(self) -> None:
        """Restaura consultas simultâneas e timeout do Remote Registry."""
        try:
            normalized_workers = set_search_max_workers(DEFAULT_SEARCH_MAX_WORKERS)
            normalized_timeout = set_remote_registry_timeout(
                REMOTE_REGISTRY_TIMEOUT_SECONDS
            )
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.search_workers_spin.blockSignals(True)
            self.search_workers_spin.setValue(get_search_max_workers())
            self.search_workers_spin.blockSignals(False)
            self.rr_timeout_spin.blockSignals(True)
            self.rr_timeout_spin.setValue(int(get_remote_registry_timeout()))
            self.rr_timeout_spin.blockSignals(False)
            return
        self.search_workers_spin.blockSignals(True)
        self.search_workers_spin.setValue(normalized_workers)
        self.search_workers_spin.blockSignals(False)
        self.rr_timeout_spin.blockSignals(True)
        self.rr_timeout_spin.setValue(int(normalized_timeout))
        self.rr_timeout_spin.blockSignals(False)

    def _on_search_workers_changed(self, value: int) -> None:
        try:
            set_search_max_workers(value)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.search_workers_spin.blockSignals(True)
            self.search_workers_spin.setValue(get_search_max_workers())
            self.search_workers_spin.blockSignals(False)

    def _on_rr_timeout_changed(self, value: int) -> None:
        try:
            set_remote_registry_timeout(value)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.rr_timeout_spin.blockSignals(True)
            self.rr_timeout_spin.setValue(int(get_remote_registry_timeout()))
            self.rr_timeout_spin.blockSignals(False)

    def _browse_pstools(self) -> None:
        start = get_pstools_dir()
        folder = QFileDialog.getExistingDirectory(
            self,
            self.tr("Selecionar pasta PSTools"),
            start if os.path.isdir(start) else DEFAULT_PSTOOLS_DIR,
        )
        if not folder:
            return
        try:
            new_path = set_pstools_dir(folder)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self.pstools_edit.setText(new_path)
        self.refresh_pstools_status()
        self.pstoolsPathChanged.emit(new_path)

    def _reset_pstools(self) -> None:
        try:
            new_path = set_pstools_dir(DEFAULT_PSTOOLS_DIR)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            return
        self.pstools_edit.setText(new_path)
        self.refresh_pstools_status()
        self.pstoolsPathChanged.emit(new_path)

    def _on_log_session_toggled(self, checked: bool) -> None:
        try:
            set_file_logging_enabled(checked)
        except SettingsWriteError as exc:
            self._show_settings_save_error(exc)
            self.log_session_check.blockSignals(True)
            self.log_session_check.setChecked(is_file_logging_enabled())
            self.log_session_check.blockSignals(False)
            return
        try:
            self.logs_edit.setText(get_log_dir(create=checked))
        except Exception:
            pass

    def _open_logs_folder(self) -> None:
        try:
            path = get_log_dir(create=is_file_logging_enabled())
        except Exception:
            path = self.logs_edit.text()
        self.logs_edit.setText(path)
        _open_in_explorer(path)

    def _open_pstools_folder(self) -> None:
        _open_in_explorer(get_pstools_dir())

    def _open_rustdesk_folder(self) -> None:
        info = probe_rustdesk_local()
        path = str(info.get("path") or "")
        folder = os.path.dirname(path) if path else r"C:\Program Files\RustDesk"
        _open_in_explorer(folder if os.path.isdir(folder) else path)

    def refresh_rustdesk_status(self) -> None:
        info = probe_rustdesk_local()
        path = str(info.get("path") or "")
        self.rustdesk_edit.setText(path)
        if info.get("found"):
            self.rustdesk_status_dot.set_color(_STATUS_COLORS["ok"])
            self.rustdesk_status_label.setText(self.tr("Instalado"))
        else:
            self.rustdesk_status_dot.set_color(_STATUS_COLORS["err"])
            self.rustdesk_status_label.setText(
                self.tr("Não encontrado em C:\\Program Files\\RustDesk\\")
            )

    def _clear_tools_flow(self) -> None:
        flow = self._tools_flow
        if flow is None:
            return
        while flow.count():
            item = flow.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _add_pstools_chip(self, tool: dict, dir_ok: bool) -> None:
        flow = self._tools_flow
        if flow is None:
            return
        chip = QHBoxLayout()
        chip.setSpacing(6)
        chip.setContentsMargins(0, 0, 0, 0)
        dot = _StatusDot(diameter=8)
        name = QLabel(str(tool.get("label") or ""))
        name.setObjectName("pstoolsToolName")
        name.setStyleSheet(
            f"QLabel#pstoolsToolName {{ font-size: {SIZE_UI_SMALL}pt; }}"
        )
        detail = QLabel()
        detail.setObjectName("toolDetail")
        detail.setStyleSheet(
            f"QLabel#toolDetail {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        if tool.get("found"):
            dot.set_color(_STATUS_COLORS["ok"])
            path = str(tool.get("path") or "")
            detail.setText(os.path.basename(path))
            name.setToolTip(path)
            detail.setToolTip(path)
        else:
            dot.set_color(_STATUS_COLORS["err"])
            expected = " / ".join(tool.get("names") or [])
            if not dir_ok:
                detail.setText(self.tr("pasta ausente"))
                tip = self.tr("Pasta PSTools não encontrada")
            else:
                detail.setText(self.tr("ausente"))
                tip = self.tr(f"Ausente ({expected})")
            name.setToolTip(tip)
            detail.setToolTip(str(tool.get("path") or "") or tip)
        chip.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
        chip.addWidget(name, 0, Qt.AlignmentFlag.AlignVCenter)
        chip.addWidget(detail, 0, Qt.AlignmentFlag.AlignVCenter)
        wrap = QWidget()
        wrap.setLayout(chip)
        wrap.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        flow.addWidget(wrap)

    def refresh_pstools_status(self) -> None:
        info = probe_pstools(get_pstools_dir())
        self.pstools_edit.setText(str(info["dir"]))
        dir_ok = bool(info["dir_ok"])
        self._clear_tools_flow()
        for tool in list(info["tools"]):
            self._add_pstools_chip(tool, dir_ok)
