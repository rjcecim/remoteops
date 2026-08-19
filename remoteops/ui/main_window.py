"""Janela principal do RemoteOps."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.builder import CommandBuilder
from remoteops.core.executor import Executor
from remoteops.services.ops import (
    CommandExecutionService,
    CredentialContext,
    RemoteUninstallService,
    RustDeskService,
)
from remoteops.ui.branding import APP_DISPLAY_NAME, app_icon
from remoteops.ui.style import SPACE_SM
from remoteops.ui.tabs.appsearch import AppSearchTab
from remoteops.ui.tabs.cmd import CmdTab
from remoteops.ui.tabs.hostapps import HostAppsTab
from remoteops.ui.tabs.msi import MsiTab
from remoteops.ui.tabs.powershell import PowerShellTab
from remoteops.ui.tabs.psexec import PsExecTab
from remoteops.ui.tabs.psinfo import PsInfoTab
from remoteops.ui.tabs.robocopy import RobocopyTab
from remoteops.ui.tabs.settings import SettingsTab
from remoteops.ui.tabs.winget import WinGetTab
from remoteops.ui.widgets.content_tab_widget import ContentSizedTabWidget
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.preview import CommandPreviewWidget
from remoteops.ui.widgets.selector import FileSelectorWidget
from remoteops.utils.app_logging import append_history, configure_logging
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import redact_command_text


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._updating_remote_cmd = False
        self._last_tab_widget = None
        self._keep_window_size = None
        self.setWindowTitle(APP_DISPLAY_NAME)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)
        central = QWidget()
        vbox = QVBoxLayout(central)
        vbox.setSpacing(SPACE_SM)
        vbox.setContentsMargins(4, 4, 4, 4)

        self.file_selector = FileSelectorWidget(self)
        vbox.addWidget(self.file_selector, 0)

        # Tabs (ícone = char Unicode em TabBar customizada)
        # stretch 0: formulário/abas não absorvem espaço vertical restante
        self.tabs = ContentSizedTabWidget()
        tab_bar = Mdl2TabBar(self.tabs)
        self.tabs.setTabBar(tab_bar)
        # QTabWidget pode reativar expanding ao associar a TabBar
        tab_bar.setExpanding(False)
        tab_bar.setElideMode(Qt.TextElideMode.ElideNone)
        tab_bar.setUsesScrollButtons(True)
        tab_bar.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideNone)
        self.tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.log_output = LogOutputWidget()
        self.psexec_tab = PsExecTab(log_output=self.log_output)
        self.psinfo_tab = None
        self.hostapps_tab = None
        self.winget_tab = None
        self.appsearch_tab = None
        self.settings_tab = None
        self.msi_tab = MsiTab()
        self.robocopy_tab = RobocopyTab()
        self.powershell_tab = PowerShellTab()
        self.cmd_tab = CmdTab()
        for _tab in (self.psexec_tab, self.msi_tab, self.robocopy_tab, self.powershell_tab, self.cmd_tab):
            _tab.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        # \uE8AF = Network/Computer (Segoe MDL2 Assets)
        self.tabs.addTab(self.psexec_tab, self.tr("PsExec"))
        self.tabs.tabBar().setTabData(0, "\uE8AF")
        self._refresh_tab_bar_layout()
        vbox.addWidget(self.tabs, 0)

        # Preview e Log: stretch 1 cada — dividem o espaço vertical restante
        self.command_preview = CommandPreviewWidget()
        self.run_button = self.command_preview.run_button
        self.stop_button = self.command_preview.stop_button
        self.stop_button.setEnabled(False)
        vbox.addWidget(self.command_preview, 1)

        vbox.addWidget(self.log_output, 1)
        self._main_layout = vbox
        self._bottom_stretch_idx = None

        self.command_preview.collapsedChanged.connect(self._redistribute_expandable_space)
        self.log_output.collapsedChanged.connect(self._redistribute_expandable_space)
        self._redistribute_expandable_space()

        self.setCentralWidget(central)
        self._apply_initial_geometry()
        
        # Instâncias auxiliares
        configure_logging()
        self.command_builder = CommandBuilder()
        self.executor = Executor()
        self._execution_service = CommandExecutionService(
            self.executor, log_fn=self.log_output.append_log
        )
        self._execution_service.set_button_callbacks(
            self._set_run_button_enabled, self.stop_button.setEnabled
        )
        self.run_button.setEnabled(False)  # só habilita com host Online
        self._uninstall_service = RemoteUninstallService()
        self._rustdesk_service = RustDeskService()
        self._rustdesk_collecting = False
        self._rustdesk_out_lines = []
        self._rustdesk_err_lines = []
        self._rustdesk_creds = None
        
        # Conexões
        self.file_selector.fileSelected.connect(self.on_file_selected)
        self.file_selector.fileCleared.connect(self.on_file_cleared)
        self.file_selector.appSearchRequested.connect(self.open_appsearch_tab)
        self.file_selector.settingsRequested.connect(self.open_settings_tab)
        self.psexec_tab.host_edit.textChanged.connect(self.update_command)
        self.psexec_tab.openHostAppsRequested.connect(self.open_hostapps_tab)
        self.psexec_tab.openWinGetRequested.connect(self.open_winget_tab)
        self.psexec_tab.openPsInfoRequested.connect(self.open_psinfo_tab)
        self.psexec_tab.openRustDeskRequested.connect(self.on_rustdesk_clicked)
        self.psexec_tab.hostOnlineChanged.connect(self._on_host_online_changed)
        self.psexec_tab.formLayoutChanged.connect(self._on_form_layout_changed)
        self.powershell_tab.formLayoutChanged.connect(self._on_form_layout_changed)
        self.cmd_tab.formLayoutChanged.connect(self._on_form_layout_changed)
        # Cards Autenticação/Desempenho já abrem recolhidos → ajusta Preview/Log
        self._on_form_layout_changed()
        self.psexec_tab.user_edit.textChanged.connect(self.update_command)
        self.psexec_tab.pass_edit.textChanged.connect(self.update_command)
        # Conexões dos checkboxes de elevação
        self.psexec_tab.flag_h.stateChanged.connect(self.update_command)
        self.psexec_tab.flag_s.stateChanged.connect(self.update_command)
        self.psexec_tab.flag_l.stateChanged.connect(self.update_command)
        self.psexec_tab.session_interactive.stateChanged.connect(self.update_command)
        self.psexec_tab.session_combo.currentIndexChanged.connect(self.update_command)
        self.psexec_tab.priority_combo.currentTextChanged.connect(self.update_command)
        self.psexec_tab.affinity_edit.textChanged.connect(self.update_command)
        self.psexec_tab.group_combo.currentTextChanged.connect(self.update_command)
        self.psexec_tab.timeout_spin.valueChanged.connect(self.update_command)
        for cb in [
            self.psexec_tab.flag_d,
            self.psexec_tab.flag_e,
            self.psexec_tab.flag_c,
            self.psexec_tab.flag_f,
            self.psexec_tab.flag_v,
            self.psexec_tab.flag_arm,
            self.psexec_tab.flag_accepteula,
            self.psexec_tab.flag_nobanner,
        ]:
            cb.stateChanged.connect(self.update_command)
        self.psexec_tab.extra_args.textChanged.connect(self.update_command)
        self.msi_tab.action_combo.currentTextChanged.connect(self.update_command)
        self.msi_tab.interface_combo.currentTextChanged.connect(self.update_command)
        self.msi_tab.restart_combo.currentTextChanged.connect(self.update_command)
        self.msi_tab.log_checkbox.stateChanged.connect(self.update_command)
        self.msi_tab.log_file_edit.textChanged.connect(self.update_command)
        self.msi_tab.repair_spin.textChanged.connect(self.update_command)
        self.msi_tab.update_edit.textChanged.connect(self.update_command)
        # Conexões do Robocopy
        self.robocopy_tab.dest_edit.textChanged.connect(self.update_command)
        for cb in self.robocopy_tab.switches:
            cb.stateChanged.connect(self.update_command)
        # Conexões da aba PowerShell
        self.powershell_tab.optionsChanged.connect(self.update_command)
        # Conexões da aba CMD
        self.cmd_tab.optionsChanged.connect(self.update_command)
        self.psexec_tab.remote_cmd_edit.textChanged.connect(self.on_remote_cmd_edit_changed)
        self.command_preview.runRequested.connect(self.on_run)
        self.command_preview.stopRequested.connect(self.on_stop)
        self.executor.outputReceived.connect(self.log_output.append_log)
        self.executor.errorReceived.connect(self.log_output.append_log)
        self.executor.partialOutput.connect(self.log_output.set_partial_line)
        self.executor.interactiveChanged.connect(self._on_console_interactive)
        self.log_output.inputSubmitted.connect(self.executor.send_input)
        self.log_output.interruptRequested.connect(self.executor.send_control)
        self.log_output.sessionExitRequested.connect(self._on_console_session_exit)
        self.log_output.consoleResized.connect(self.executor.resize_conpty)
        self.executor.finished.connect(self.on_process_finished)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        
        # Estado inicial: não adiciona a aba MSI nem Robocopy
        self.msi_tab_index = None
        self.robocopy_tab_index = None
        self.update_command()
        self._update_psinfo_mode_ui()
        self._last_tab_widget = self.tabs.currentWidget()

    def _set_run_button_enabled(self, enabled: bool) -> None:
        """Habilita Executar só se o host estiver Online (e o caller pediu enable)."""
        if not enabled:
            self.run_button.setEnabled(False)
            return
        self.run_button.setEnabled(bool(self.psexec_tab.is_host_online))

    def _on_host_online_changed(self, online: bool) -> None:
        # Em execução o Parar fica ativo — não reabilitar Executar no meio do processo.
        if self.stop_button.isEnabled():
            self.run_button.setEnabled(False)
            return
        self.run_button.setEnabled(bool(online))

    def _current_creds(self) -> CredentialContext:
        return CredentialContext(
            user=self.psexec_tab.auth_username(),
            password=self.psexec_tab.pass_edit.text() or "",
        )

    def _escape_quotes(self, s: str) -> str:
        return (s or "").replace('"', '\\"')

    def _quote_if_needed(self, s: str) -> str:
        s = (s or "").strip()
        if not s:
            return s
        return f'"{s}"' if " " in s else s

    def _build_psexec_exe(self) -> str:
        from remoteops.services.ops import resolve_psexec_exe

        return resolve_psexec_exe(get_pstools_dir())

    def on_rustdesk_clicked(self) -> None:
        """
        Fluxo:
        1) Executa remoto: rustdesk.exe --get-id (via PsExec com -h -s)
        2) Extrai ID do stdout
        3) Executa local: rustdesk.exe --connect <ID>
        """
        host = (self.psexec_tab.host_edit.text() or "").strip().strip("\\")
        if not host:
            ph = self.psexec_tab.host_edit.placeholderText()
            self.log_output.append_log(
                self.tr(f"[RUSTDESK] Por favor, insira um host ({ph}).")
            )
            return

        if getattr(self.executor, "future", None) is not None or getattr(
            self.executor, "process", None
        ) is not None:
            self.log_output.append_log(
                self.tr(
                    "[RUSTDESK] Aguarde a execução atual terminar antes de usar RustDesk."
                )
            )
            return

        if self._rustdesk_collecting:
            return

        self._rustdesk_collecting = True
        self._rustdesk_out_lines = []
        self._rustdesk_err_lines = []
        self._rustdesk_last_path = None
        self._rustdesk_creds = self._current_creds()

        svc = self._rustdesk_service
        paths = list(svc.remote_paths)

        def build_spec(remote_path: str):
            return svc.build_get_id_spec(
                host=host,
                pstools_path=get_pstools_dir(),
                creds=self._rustdesk_creds or CredentialContext(),
                remote_path=remote_path,
            )

        self.log_output.append_log(
            self.tr(f"[RUSTDESK] Conectando em {host} e coletando ID...")
        )

        def on_out(line: str) -> None:
            if line is None:
                return
            t = str(line).strip()
            if t:
                self._rustdesk_out_lines.append(t)

        def on_err(line: str) -> None:
            if line is None:
                return
            t = str(line).strip()
            if t:
                self._rustdesk_err_lines.append(t)

        def on_done(exit_code: int) -> None:
            try:
                self.executor.outputReceived.disconnect(on_out)
            except Exception:
                pass
            try:
                self.executor.errorReceived.disconnect(on_err)
            except Exception:
                pass
            try:
                self.executor.finished.disconnect(on_done)
            except Exception:
                pass

            self._rustdesk_collecting = False
            err_text = "\n".join(self._rustdesk_err_lines).strip()
            # Nunca ecoar stderr bruto se contiver senha
            err_safe = redact_command_text(
                err_text,
                passwords=(self._rustdesk_creds.passwords if self._rustdesk_creds else None),
            )

            rust_id = svc.extract_id(self._rustdesk_out_lines)
            is_not_found = svc.is_not_found(err_text)

            if (
                not rust_id
                and is_not_found
                and getattr(self, "_rustdesk_last_path", None) != paths[1]
            ):
                self._rustdesk_last_path = paths[1]
                self._rustdesk_collecting = True
                self._rustdesk_out_lines = []
                self._rustdesk_err_lines = []
                self.log_output.append_log(
                    self.tr(
                        "[RUSTDESK] Tentando caminho alternativo do RustDesk no host..."
                    )
                )
                self.executor.outputReceived.connect(on_out)
                self.executor.errorReceived.connect(on_err)
                self.executor.finished.connect(on_done)
                self.executor.run(
                    build_spec(paths[1]),
                    passwords=(
                        self._rustdesk_creds.passwords if self._rustdesk_creds else None
                    ),
                )
                return

            if not rust_id:
                if exit_code != 0 and svc.is_access_error(err_text):
                    self.log_output.append_log(
                        self.tr(
                            "[RUSTDESK] ERRO: Não foi possível conectar ao host "
                            "(rede/RPC/credenciais)."
                        )
                    )
                    if err_safe:
                        self.log_output.append_log(
                            self.tr(f"[RUSTDESK] Detalhes: {err_safe}")
                        )
                elif is_not_found:
                    self.log_output.append_log(
                        self.tr("[RUSTDESK] ERRO: RustDesk não encontrado no host.")
                    )
                elif err_safe:
                    self.log_output.append_log(
                        self.tr(f"[RUSTDESK] ERRO: {err_safe}")
                    )
                else:
                    self.log_output.append_log(
                        self.tr(
                            "[RUSTDESK] ERRO: Não foi possível obter o ID do RustDesk."
                        )
                    )
                if self._rustdesk_creds:
                    self._rustdesk_creds.clear()
                    self._rustdesk_creds = None
                return

            self.log_output.append_log(
                self.tr(f"[RUSTDESK] ID detectado: {rust_id}")
            )
            ok, detail = svc.open_local_connect(rust_id)
            if ok:
                self.log_output.append_log(
                    self.tr(f"[RUSTDESK] Executando local: {detail}")
                )
                self.log_output.append_log(self.tr("[RUSTDESK] Abrindo RustDesk..."))
            else:
                self.log_output.append_log(
                    self.tr(f"[RUSTDESK] ERRO ao abrir RustDesk local: {detail}")
                )
            if self._rustdesk_creds:
                self._rustdesk_creds.clear()
                self._rustdesk_creds = None

        self._rustdesk_last_path = paths[0]
        self.executor.outputReceived.connect(on_out)
        self.executor.errorReceived.connect(on_err)
        self.executor.finished.connect(on_done)
        self.executor.run(
            build_spec(paths[0]),
            passwords=(
                self._rustdesk_creds.passwords if self._rustdesk_creds else None
            ),
        )

    def open_psinfo_tab(self) -> None:
        """
        Cria a aba PsInfo sob demanda e foca nela.
        """
        host = self.psexec_tab.host_edit.text().strip()
        if not host:
            self.log_output.append_log(self.tr("[PSINFO] Preencha o Host remoto antes de abrir o PsInfo."))
            return

        self._remember_window_size()

        # Se já existe, apenas focar
        if self.psinfo_tab is not None:
            idx = self.tabs.indexOf(self.psinfo_tab)
            if idx != -1:
                self.tabs.setCurrentIndex(idx)
                # Sempre re-executar para o host atual
                self.psinfo_tab.run_psinfo()
                self._update_psinfo_mode_ui()
                return

        self.psinfo_tab = PsInfoTab(
            log_output=self.log_output,
            host_source=self.psexec_tab.host_edit,
            creds_provider=lambda: (
                self.psexec_tab.auth_username(),
                self.psexec_tab.pass_edit.text() or "",
            ),
        )
        # PsInfo deve ser sempre a última aba
        self.tabs.addTab(self.psinfo_tab, self.tr("PsInfo"))
        psinfo_idx = self.tabs.indexOf(self.psinfo_tab)
        self.tabs.tabBar().setTabData(psinfo_idx, "\uE946")  # Info
        self._refresh_tab_bar_layout()
        self.tabs.setCurrentIndex(psinfo_idx)
        self.psinfo_tab.run_psinfo()
        self._update_psinfo_mode_ui()

    def open_hostapps_tab(self) -> None:
        """Abre aba de aplicativos do host atual (Remote Registry + console próprio)."""
        host = self.psexec_tab.host_edit.text().strip()
        if not host:
            self.log_output.append_log(
                self.tr("[APPS] Preencha o Host remoto antes de listar aplicativos.")
            )
            return

        self._remember_window_size()

        if self.hostapps_tab is not None:
            idx = self.tabs.indexOf(self.hostapps_tab)
            if idx != -1:
                self.tabs.setCurrentIndex(idx)
                self.hostapps_tab.run_inventory()
                self._update_psinfo_mode_ui()
                return

        self.hostapps_tab = HostAppsTab(host_source=self.psexec_tab.host_edit)
        self.hostapps_tab.uninstallRequested.connect(self._on_hostapps_uninstall)
        self.tabs.addTab(self.hostapps_tab, self.tr("Aplicativos"))
        idx = self.tabs.indexOf(self.hostapps_tab)
        bar = self.tabs.tabBar()
        if isinstance(bar, Mdl2TabBar):
            bar.set_tab_meta(idx, "\uE71D", closable=True)
        else:
            bar.setTabData(idx, "\uE71D")
        self._refresh_tab_bar_layout()
        self.tabs.setCurrentIndex(idx)
        self.hostapps_tab.run_inventory()
        self._update_psinfo_mode_ui()

    def open_winget_tab(self) -> None:
        """Abre aba WinGet (pacotes remotos) reutilizando host/creds do PsExec."""
        host = self.psexec_tab.host_edit.text().strip()
        if not host:
            self.log_output.append_log(
                self.tr("[WINGET] Preencha o Host remoto antes de abrir o WinGet.")
            )
            return

        self._remember_window_size()

        if self.winget_tab is not None:
            idx = self.tabs.indexOf(self.winget_tab)
            if idx != -1:
                self.tabs.setCurrentIndex(idx)
                self._update_psinfo_mode_ui()
                return

        self.winget_tab = WinGetTab(
            host_source=self.psexec_tab.host_edit,
            creds_provider=lambda: (
                self.psexec_tab.auth_username(),
                self.psexec_tab.pass_edit.text() or "",
            ),
        )
        self.tabs.addTab(self.winget_tab, self.tr("WinGet"))
        idx = self.tabs.indexOf(self.winget_tab)
        bar = self.tabs.tabBar()
        if isinstance(bar, Mdl2TabBar):
            bar.set_tab_meta(idx, "\uE7B8", closable=True)
        else:
            bar.setTabData(idx, "\uE7B8")
        self._refresh_tab_bar_layout()
        self.tabs.setCurrentIndex(idx)
        self._update_psinfo_mode_ui()

    def open_appsearch_tab(self) -> None:
        """Cria a aba Pesquisa de Aplicativos sob demanda e foca nela."""
        self._remember_window_size()
        if self.appsearch_tab is not None:
            idx = self.tabs.indexOf(self.appsearch_tab)
            if idx != -1:
                self.tabs.setCurrentIndex(idx)
                self._update_psinfo_mode_ui()
                return

        self.appsearch_tab = AppSearchTab()
        self.appsearch_tab.uninstallRequested.connect(self._on_appsearch_uninstall)
        self._wire_network_range_status()
        self.tabs.addTab(self.appsearch_tab, self.tr("Pesquisa de Aplicativos"))
        idx = self.tabs.indexOf(self.appsearch_tab)
        bar = self.tabs.tabBar()
        if isinstance(bar, Mdl2TabBar):
            bar.set_tab_meta(idx, "\uE721", closable=True)
        else:
            bar.setTabData(idx, "\uE721")
        self._refresh_tab_bar_layout()
        self.tabs.setCurrentIndex(idx)
        self._update_psinfo_mode_ui()

    def _wire_network_range_status(self) -> None:
        """Atualiza o status da Pesquisa quando a faixa de IP é salva."""
        settings = self.settings_tab
        search = self.appsearch_tab
        if settings is None or search is None:
            return
        try:
            settings.networkRangeChanged.disconnect(search.refresh_hosts_status)
        except TypeError:
            pass
        settings.networkRangeChanged.connect(search.refresh_hosts_status)

    def _on_tab_close_requested(self, index: int) -> None:
        """Fecha abas com X no título (Aplicativos / Pesquisa / Configurações)."""
        widget = self.tabs.widget(index)
        if widget is None:
            return
        if widget is self.hostapps_tab:
            self._close_hostapps_tab()
        elif widget is self.winget_tab:
            self._close_winget_tab()
        elif widget is self.appsearch_tab:
            self._close_appsearch_tab()
        elif widget is self.settings_tab:
            self._close_settings_tab()

    def _close_hostapps_tab(self) -> None:
        if self.hostapps_tab is None:
            return
        idx = self.tabs.indexOf(self.hostapps_tab)
        if idx != -1:
            self.tabs.removeTab(idx)
        try:
            self.hostapps_tab.shutdown()
        except Exception:
            pass
        self.hostapps_tab.deleteLater()
        self.hostapps_tab = None
        self._update_psinfo_mode_ui()
        self._last_tab_widget = self.tabs.currentWidget()
        self._refresh_tab_bar_layout()

    def _close_winget_tab(self) -> None:
        if self.winget_tab is None:
            return
        idx = self.tabs.indexOf(self.winget_tab)
        if idx != -1:
            self.tabs.removeTab(idx)
        try:
            self.winget_tab.shutdown()
        except Exception:
            pass
        self.winget_tab.deleteLater()
        self.winget_tab = None
        self._update_psinfo_mode_ui()
        self._last_tab_widget = self.tabs.currentWidget()
        self._refresh_tab_bar_layout()

    def _close_appsearch_tab(self) -> None:
        """Fecha a Pesquisa somente pelo X ao lado do título da aba."""
        if self.appsearch_tab is None:
            return
        idx = self.tabs.indexOf(self.appsearch_tab)
        if idx != -1:
            self.tabs.removeTab(idx)
        try:
            self.appsearch_tab.shutdown()
        except Exception:
            pass
        self.appsearch_tab.deleteLater()
        self.appsearch_tab = None
        self._update_psinfo_mode_ui()
        self._last_tab_widget = self.tabs.currentWidget()
        self._refresh_tab_bar_layout()

    def _close_settings_tab(self) -> None:
        """Fecha Configurações somente pelo X ao lado do título da aba."""
        if self.settings_tab is None:
            return
        idx = self.tabs.indexOf(self.settings_tab)
        if idx != -1:
            self.tabs.removeTab(idx)
        self.settings_tab.deleteLater()
        self.settings_tab = None
        self._update_psinfo_mode_ui()
        self._last_tab_widget = self.tabs.currentWidget()
        self._refresh_tab_bar_layout()

    def _on_hostapps_uninstall(self, host: str, remote_cmd: str, app_label: str) -> None:
        """Desinstalação a partir da aba Aplicativos: terminal externo + console da aba."""
        log_fn = None
        if self.hostapps_tab is not None:
            log_fn = self.hostapps_tab.log_output.append_log
        self._run_remote_uninstall(
            host, remote_cmd, app_label, log_tag="APPS", log_fn=log_fn
        )

    def _on_appsearch_uninstall(self, host: str, remote_cmd: str, app_label: str) -> None:
        """Desinstalação a partir da pesquisa: terminal externo + console da própria aba."""
        log_fn = None
        if self.appsearch_tab is not None:
            log_fn = self.appsearch_tab.log_output.append_log
        self._run_remote_uninstall(
            host, remote_cmd, app_label, log_tag="PESQUISA", log_fn=log_fn
        )

    def _run_remote_uninstall(
        self,
        host: str,
        remote_cmd: str,
        app_label: str,
        log_tag: str = "PSINFO",
        log_fn=None,
    ) -> None:
        """Desinstalação remota via serviço (sem senha em arquivos temporários)."""
        creds = self._current_creds()
        try:
            self._uninstall_service.run(
                host=host,
                remote_cmd=remote_cmd,
                app_label=app_label,
                pstools_path=get_pstools_dir(),
                creds=creds,
                log_tag=log_tag,
                log_fn=log_fn or self.log_output.append_log,
            )
        finally:
            creds.clear()

    def open_settings_tab(self) -> None:
        """Cria a aba Configurações sob demanda e foca nela."""
        self._remember_window_size()
        if self.settings_tab is not None:
            idx = self.tabs.indexOf(self.settings_tab)
            if idx != -1:
                self.tabs.setCurrentIndex(idx)
                self._update_psinfo_mode_ui()
                return

        self.settings_tab = SettingsTab()
        self.settings_tab.pstoolsPathChanged.connect(self._on_pstools_path_changed)
        self._wire_network_range_status()
        self.tabs.addTab(self.settings_tab, self.tr("Configurações"))
        idx = self.tabs.indexOf(self.settings_tab)
        bar = self.tabs.tabBar()
        if isinstance(bar, Mdl2TabBar):
            bar.set_tab_meta(idx, "\uE713", closable=True)  # Settings / engrenagem
        else:
            bar.setTabData(idx, "\uE713")
        self._refresh_tab_bar_layout()
        self.tabs.setCurrentIndex(idx)
        self._update_psinfo_mode_ui()

    def _on_pstools_path_changed(self, _path: str) -> None:
        self.update_command()

    def _on_tab_changed(self, _index: int) -> None:
        # PsInfo: fecha ao sair da aba.
        # Aplicativos / Pesquisa / Configurações: permanecem; fecham pelo X.
        prev = self._last_tab_widget
        current = self.tabs.currentWidget()
        if self.psinfo_tab is not None:
            if prev == self.psinfo_tab and current != self.psinfo_tab:
                idx = self.tabs.indexOf(self.psinfo_tab)
                if idx != -1:
                    self.tabs.removeTab(idx)
                try:
                    self.psinfo_tab.shutdown()
                except Exception:
                    pass
                self.psinfo_tab.deleteLater()
                self.psinfo_tab = None
        self._update_psinfo_mode_ui()
        self._last_tab_widget = self.tabs.currentWidget()
        # Troca de aba: altura do formulário muda → Preview/Log redistribuem
        self._on_form_layout_changed()
        # Aba PowerShell/CMD ativa muda o método de montagem do comando
        self.update_command()

    def _on_form_layout_changed(self) -> None:
        """Formulário recolhido/expandido: abas encolhem e Preview/Log absorvem a sobra."""
        # Após o layout dos cards estabilizar (inclui cards que nascem minimizados).
        QTimer.singleShot(0, self._apply_form_layout_change)

    def _apply_form_layout_change(self) -> None:
        frozen = self._keep_window_size if self._keep_window_size is not None else self.size()
        self.tabs.sync_content_height()
        if self._main_layout is not None:
            self._main_layout.activate()
        if self.centralWidget() is not None:
            self.centralWidget().updateGeometry()
        self._redistribute_expandable_space()
        if self.size() != frozen:
            self.resize(frozen)

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
        """
        Divide o espaço vertical restante entre Pré-visualização e Log abertos.
        Com ambos recolhidos, um stretch final absorve a sobra (formulário não estica).
        Em abas em tela cheia (PsInfo/Pesquisa/Configurações), as abas
        absorvem o espaço (preview/log ocultos).
        """
        lay = getattr(self, "_main_layout", None)
        if lay is None:
            return

        # Não usar isVisible(): antes do show() os widgets estão ocultos e
        # isso ativaria o stretch final por engano no startup.
        is_psinfo = (
            self.psinfo_tab is not None
            and self.tabs.currentWidget() == self.psinfo_tab
        )
        is_hostapps = (
            self.hostapps_tab is not None
            and self.tabs.currentWidget() == self.hostapps_tab
        )
        is_winget = (
            self.winget_tab is not None
            and self.tabs.currentWidget() == self.winget_tab
        )
        is_appsearch = (
            self.appsearch_tab is not None
            and self.tabs.currentWidget() == self.appsearch_tab
        )
        is_settings = (
            self.settings_tab is not None
            and self.tabs.currentWidget() == self.settings_tab
        )
        is_fullscreen_tab = is_psinfo or is_hostapps or is_winget or is_appsearch or is_settings

        tabs_idx = lay.indexOf(self.tabs)
        if is_fullscreen_tab:
            if tabs_idx >= 0:
                lay.setStretch(tabs_idx, 1)
            self.tabs.set_fill_available(True)
            for w in (self.command_preview, self.log_output):
                idx = lay.indexOf(w)
                if idx >= 0:
                    lay.setStretch(idx, 0)
            if self._bottom_stretch_idx is not None:
                lay.setStretch(self._bottom_stretch_idx, 0)
            lay.activate()
            if self.centralWidget() is not None:
                self.centralWidget().updateGeometry()
            return

        if tabs_idx >= 0:
            lay.setStretch(tabs_idx, 0)
        # Sai do Expanding de tela cheia e limita altura ao conteúdo da aba
        # (evita o vão vazio entre o formulário e a Pré-visualização).
        self.tabs.set_fill_available(False)

        expandables = [self.command_preview, self.log_output]
        open_cards = [w for w in expandables if not w.is_collapsed]

        for w in expandables:
            idx = lay.indexOf(w)
            if idx < 0:
                continue
            stretch = 1 if (w in open_cards) else 0
            w.set_layout_stretch(1)
            lay.setStretch(idx, stretch)

        # Stretch final: só quando nenhum expansível está aberto
        need_tail = len(open_cards) == 0
        if need_tail:
            if self._bottom_stretch_idx is None:
                lay.addStretch(1)
                self._bottom_stretch_idx = lay.count() - 1
            else:
                lay.setStretch(self._bottom_stretch_idx, 1)
        elif self._bottom_stretch_idx is not None:
            lay.setStretch(self._bottom_stretch_idx, 0)

        # Recalcular teto após Preview/Log voltarem a participar do layout
        self.tabs.sync_content_height()
        lay.activate()
        if self.centralWidget() is not None:
            self.centralWidget().updateGeometry()

    def _remember_window_size(self) -> None:
        """Guarda o tamanho atual para restaurar após addTab/layout de tela cheia."""
        if self._keep_window_size is None:
            self._keep_window_size = self.size()

    def _restore_kept_window_size(self) -> None:
        frozen = self._keep_window_size
        if frozen is not None and self.size() != frozen:
            self.resize(frozen)

    def _finish_kept_window_size(self) -> None:
        self._restore_kept_window_size()
        self._keep_window_size = None

    def _update_psinfo_mode_ui(self) -> None:
        """
        Abas de inventário/pesquisa/configurações ocupam a janela;
        preview, log e Run ficam ocultos.

        Mantém o tamanho da janela — esconder Preview/Log não deve
        redimensionar o app (nem ao abrir PsInfo/WinGet).
        """
        frozen = (
            self._keep_window_size
            if self._keep_window_size is not None
            else self.size()
        )
        current = self.tabs.currentWidget()
        is_fullscreen = (
            (self.psinfo_tab is not None and current == self.psinfo_tab)
            or (self.hostapps_tab is not None and current == self.hostapps_tab)
            or (self.winget_tab is not None and current == self.winget_tab)
            or (self.appsearch_tab is not None and current == self.appsearch_tab)
            or (self.settings_tab is not None and current == self.settings_tab)
        )
        self.command_preview.setVisible(not is_fullscreen)
        self.log_output.setVisible(not is_fullscreen)
        self._redistribute_expandable_space()
        if self.size() != frozen:
            self.resize(frozen)
        if self._keep_window_size is not None:
            QTimer.singleShot(0, self._finish_kept_window_size)

    def _apply_initial_geometry(self):
        """Abre em 720×960 (reduz se a área útil da tela for menor)."""
        screen = QApplication.primaryScreen()
        if not screen:
            self.resize(720, 960)
            return

        avail = screen.availableGeometry()
        w = min(720, avail.width() - 20)
        h = min(960, avail.height() - 20)
        w = max(400, w)
        h = max(400, h)
        self.resize(w, h)
        x = avail.x() + (avail.width() - w) // 2
        y = avail.y() + (avail.height() - h) // 2
        self.move(x, y)

    def on_file_cleared(self):
        """Reset do card: remove o arquivo/pasta e fecha abas que dependiam dele."""
        self.command_builder.set_file_selection(None)
        self._sync_psexec_copy_allowed()
        self.update_tab_visibility(False, False)
        self.update_command()

    def on_file_selected(self, selection):
        # selection: {'mode': 'file'|'folder', 'file': caminho, 'folder': caminho ou None}
        if isinstance(selection, dict):
            self.command_builder.set_file_selection(selection)
            file_path = selection['file']
            is_msi = file_path.lower().endswith('.msi')
            is_exe = file_path.lower().endswith('.exe')
        else:
            # Compatibilidade retroativa
            self.command_builder.set_file({'mode': 'file', 'file': selection, 'folder': None})
            file_path = selection
            is_msi = file_path.lower().endswith('.msi')
            is_exe = file_path.lower().endswith('.exe')
        # -c/-f/-v são recalculados pela aba (Robocopy vs cópia do PsExec)
        self._sync_psexec_copy_allowed()
        # NOVO: Preencher campo -File da aba PowerShell automaticamente
        self.update_tab_visibility(is_msi, is_exe)
        self.update_command()

    def should_enable_robocopy(self):
        selected_file = getattr(self.file_selector, 'selected_file', None)
        if not selected_file:
            return False
        ext = selected_file.lower().split('.')[-1] if '.' in selected_file else ''
        if ext == 'exe':
            return False
        # CORREÇÃO: Só desabilitar robocopy se houver comando remoto manual
        # Não desabilitar se houver apenas parâmetros da aba CMD ou PowerShell
        remote_cmd = self.psexec_tab.remote_cmd_edit.text().strip()
        if remote_cmd and remote_cmd != 'Comando gerado automaticamente':
            # Verificar se é realmente um comando manual ou apenas parâmetros de aba
            # Se o comando contém apenas parâmetros como cmd, powershell, etc., não desabilitar
            if remote_cmd.lower() in ['cmd', 'cmd.exe', 'powershell', 'powershell.exe']:
                return True
            # Se contém outros comandos, desabilitar robocopy
            return False
        return True

    def _sync_psexec_copy_allowed(self) -> None:
        selection = getattr(self.file_selector, "selected_file", None)
        if selection:
            self.psexec_tab.set_copy_allowed(not self.should_enable_robocopy())
        else:
            self.psexec_tab.set_copy_allowed(True)

    def update_tab_visibility(self, is_msi, is_exe):
        """Atualiza a visibilidade das abas mantendo a ordem: PsExec, MSI, PowerShell, CMD, Robocopy, (PsInfo opcional por último)"""
        robocopy_enabled = self.should_enable_robocopy()
        selected_file = self.file_selector.selected_file if hasattr(self.file_selector, 'selected_file') else None
        ext = selected_file.lower().split('.')[-1] if selected_file and '.' in selected_file else ''
        remote_cmd = self.psexec_tab.remote_cmd_edit.text().strip().lower()
        show_powershell_tab = False
        show_cmd_tab = False
        powershell_by_file = False
        cmd_by_file = False
        # Lógica para exibir abas extras
        if ext == 'ps1':
            show_powershell_tab = True
            powershell_by_file = True
        if ext == 'bat':
            show_cmd_tab = True
            cmd_by_file = True
        # Se comando remoto for powershell ou cmd, mostrar aba correspondente
        if remote_cmd in ['powershell', 'powershell.exe']:
            show_powershell_tab = True
            powershell_by_file = False
        if remote_cmd in ['cmd', 'cmd.exe']:
            show_cmd_tab = True
            cmd_by_file = False
        # Remover abas dinâmicas, preservando PsExec e abas especiais sob demanda
        psinfo_widget = self.psinfo_tab
        hostapps_widget = self.hostapps_tab
        winget_widget = self.winget_tab
        appsearch_widget = self.appsearch_tab
        settings_widget = self.settings_tab
        for i in range(self.tabs.count() - 1, -1, -1):
            w = self.tabs.widget(i)
            if w is self.psexec_tab:
                continue
            if psinfo_widget is not None and w is psinfo_widget:
                continue
            if hostapps_widget is not None and w is hostapps_widget:
                continue
            if winget_widget is not None and w is winget_widget:
                continue
            if appsearch_widget is not None and w is appsearch_widget:
                continue
            if settings_widget is not None and w is settings_widget:
                continue
            self.tabs.removeTab(i)

        # Índice onde os tabs dinâmicos serão inseridos (antes das abas especiais)
        insert_at = self.tabs.count()
        for special in (
            psinfo_widget,
            hostapps_widget,
            winget_widget,
            appsearch_widget,
            settings_widget,
        ):
            if special is None:
                continue
            idx = self.tabs.indexOf(special)
            if idx != -1:
                insert_at = min(insert_at, idx)

        def _insert_tab(widget, title: str, icon_char: str) -> None:
            nonlocal insert_at
            self.tabs.insertTab(insert_at, widget, title)
            bar = self.tabs.tabBar()
            bar.setTabData(insert_at, icon_char)
            # setTabData não relayouta; reaplicar o texto força layoutTabs()
            # com o sizeHint já incluindo o ícone (evita "PowerShe" cortado).
            bar.setTabText(insert_at, title)
            insert_at += 1

        # Adiciona MSI / PowerShell / CMD / Robocopy (ícone = char Unicode)
        # \uE8A5 = Package/MSI, \uE756 = PowerShell, \uE7ED = CMD/Console, \uE8B7 = Copy/Robocopy
        if is_msi:
            _insert_tab(self.msi_tab, self.tr("MSI"), "\uE8A5")
        if show_powershell_tab:
            _insert_tab(self.powershell_tab, self.tr("PowerShell"), "\uE756")
            self.powershell_tab.set_command_fields_enabled(not powershell_by_file)
        if show_cmd_tab:
            _insert_tab(self.cmd_tab, self.tr("CMD"), "\uE7ED")
            self.cmd_tab.set_command_field_enabled(not cmd_by_file)
        if robocopy_enabled:
            _insert_tab(self.robocopy_tab, self.tr("Robocopy"), "\uE8B7")

        # Garante larguras corretas após inserções dinâmicas (comando remoto / arquivo)
        self._refresh_tab_bar_layout()

    def _refresh_tab_bar_layout(self) -> None:
        """Recalcula larguras das abas após setTabData/insert dinâmico."""
        bar = self.tabs.tabBar()
        bar.setExpanding(False)
        bar.setElideMode(Qt.TextElideMode.ElideNone)
        bar.setUsesScrollButtons(True)
        if isinstance(bar, Mdl2TabBar):
            bar.refresh_layout()
        else:
            for i in range(bar.count()):
                bar.setTabText(i, bar.tabText(i))
            bar.updateGeometry()
        bar.raise_()
        self.tabs.updateGeometry()

    def build_command_for_execution(self):
        """
        Centraliza a lógica de montagem do comando para execução, preview e log.
        Sempre retorna representação SANITIZADA (senha mascarada).
        """
        specs = self.build_specs_for_execution()
        passwords = []
        pwd = self.psexec_tab.pass_edit.text() or ""
        if pwd.strip():
            passwords.append(pwd)
        return "\n".join(s.sanitized_display(passwords) for s in specs)

    def build_specs_for_execution(self):
        """Mesma lógica de build_command_for_execution, retornando CommandSpec(s)."""
        selection = getattr(self.file_selector, 'selected_file', None)
        remote_cmd = self.psexec_tab.remote_cmd_edit.text().strip().lower()
        specs = []

        if selection:
            ext = selection.lower().split('.')[-1] if '.' in selection else ''
            if self.should_enable_robocopy():
                return self.command_builder.build_execution_plan()
            if ext == 'bat' and self.tabs.currentWidget() == self.cmd_tab:
                specs.append(self.command_builder._build_psexec_bat_script_spec())
                return specs
            if ext == 'ps1':
                specs.append(self.command_builder._build_psexec_ps_script_spec())
                return specs
            specs.append(self.command_builder.build_psexec_spec())
            return specs

        if remote_cmd in ['powershell', 'powershell.exe']:
            specs.append(self.command_builder._build_psexec_ps_script_spec())
            return specs
        if remote_cmd in ['cmd', 'cmd.exe']:
            specs.append(self.command_builder._build_psexec_bat_script_spec())
            return specs
        if self.tabs.currentWidget() == self.cmd_tab:
            specs.append(self.command_builder._build_psexec_bat_script_spec())
            return specs
        if self.tabs.currentWidget() == self.powershell_tab:
            specs.append(self.command_builder._build_psexec_ps_script_spec())
            return specs
        specs.append(self.command_builder.build_psexec_spec())
        return specs

    def update_command(self):
        from PyQt6.QtWidgets import QApplication
        # Parâmetros MSI
        msi_params = {
            'enable': True,  # Sempre habilitado se for MSI
            'action': self.msi_tab.action_combo.currentText(),
            'interface': self.msi_tab.interface_combo.currentText(),
            'restart': self.msi_tab.restart_combo.currentText(),
            'log': self.msi_tab.log_checkbox.isChecked(),
            'log_file': self.msi_tab.log_file_edit.text(),
            'repair': self.msi_tab.repair_spin.text(),
            'update': self.msi_tab.update_edit.text(),
        }
        self.command_builder.set_msi_params(msi_params)

        # Recupera seleção do FileSelectorWidget
        selection = getattr(self.file_selector, 'selected_file', None)
        selection_folder = getattr(self.file_selector, 'selected_folder', None)
        selection_mode = getattr(self.file_selector, 'selection_mode', None)
        # Monta dict de seleção
        if selection_mode == 'folder' and selection and selection_folder:
            file_selection = {'mode': 'folder', 'file': selection, 'folder': selection_folder}
        elif selection:
            file_selection = {'mode': 'file', 'file': selection, 'folder': None}
        else:
            file_selection = None
        robocopy_enabled = self.should_enable_robocopy()
        # --- CORREÇÃO: Sempre preservar parâmetros da aba PowerShell se ela existir ---
        if hasattr(self, 'powershell_tab') and self.powershell_tab:
            self.command_builder.set_powershell_params(self.powershell_tab.get_params())
        else:
            self.command_builder.set_powershell_params({})
        # --- CORREÇÃO: Sempre preservar parâmetros da aba CMD se ela existir ---
        if hasattr(self, 'cmd_tab') and self.cmd_tab:
            self.command_builder.set_cmd_params(self.cmd_tab.get_params())
        else:
            self.command_builder.set_cmd_params({})
        if file_selection:
            self.command_builder.set_file_selection(file_selection)
            self._updating_remote_cmd = True
            self.psexec_tab.remote_cmd_edit.setReadOnly(True)
            self.psexec_tab.remote_cmd_edit.setText('Comando gerado automaticamente')
            QApplication.processEvents()
            self.psexec_tab.remote_cmd_edit.repaint()
            self._updating_remote_cmd = False
            self._sync_psexec_copy_allowed()
            psexec_params = self.psexec_tab.collect_builder_params(
                host=self.psexec_tab.host_edit.text(),
                psexec_path=get_pstools_dir(),
                remote_cmd=self.psexec_tab.remote_cmd_edit.text(),
            )
            robocopy_params = self.robocopy_tab.get_params() if robocopy_enabled else None
            self.command_builder.set_robocopy_params(robocopy_params)
            self.command_builder.set_psexec_params(psexec_params)
            # Atualizar preview SEMPRE com representação sanitizada
            command = self.build_command_for_execution()
            self.command_preview.set_command(command)
        else:
            self.psexec_tab.remote_cmd_edit.setReadOnly(False)
            if self.psexec_tab.remote_cmd_edit.text().strip() == "Comando gerado automaticamente":
                self._updating_remote_cmd = True
                self.psexec_tab.remote_cmd_edit.clear()
                self._updating_remote_cmd = False
            self._sync_psexec_copy_allowed()
            psexec_params = self.psexec_tab.collect_builder_params(
                host=self.psexec_tab.host_edit.text(),
                psexec_path=get_pstools_dir(),
                remote_cmd=self.psexec_tab.remote_cmd_edit.text(),
            )
            self.command_builder.set_psexec_params(psexec_params)
            self.command_builder.set_robocopy_params(None)
            # Atualizar preview SEMPRE com representação sanitizada
            command = self.build_command_for_execution()
            self.command_preview.set_command(command)

    def log_to_file(self, text: str):
        """Grava histórico sanitizado (nunca senha)."""
        pwd = self.psexec_tab.pass_edit.text() if hasattr(self, "psexec_tab") else ""
        passwords = [pwd] if pwd else None
        append_history(text, passwords=passwords)

    def on_run(self):
        if not self.psexec_tab.is_host_online:
            self.log_output.append_log(
                self.tr("[HOST] Host remoto precisa estar Online para executar.")
            )
            return
        if (
            self.stop_button.isEnabled()
            or self.executor.is_busy
            or self._execution_service.awaiting_followup
        ):
            self.log_output.append_log(
                self.tr(
                    "Um comando ainda está em execução. "
                    "Aguarde terminar antes de iniciar outro."
                )
            )
            return
        self.update_command()
        errors = self.command_builder.validate_psexec_params()
        if errors:
            for err in errors:
                self.log_output.append_log(self.tr(f"[PSEXEC] {err}"))
            return
        if self.tabs.currentWidget() == self.cmd_tab:
            cmd_errors = self.command_builder.validate_cmd_params()
            if cmd_errors:
                for err in cmd_errors:
                    self.log_output.append_log(self.tr(f"[CMD] {err}"))
                return
        selection = getattr(self.file_selector, "selected_file", None) or ""
        remote_cmd = self.psexec_tab.remote_cmd_edit.text().strip().lower()
        if (
            self.tabs.currentWidget() == self.powershell_tab
            or remote_cmd in ("powershell", "powershell.exe")
            or str(selection).lower().endswith(".ps1")
        ):
            ps_errors = self.command_builder.validate_powershell_params()
            if ps_errors:
                for err in ps_errors:
                    self.log_output.append_log(self.tr(f"[POWERSHELL] {err}"))
                return
        # Credencial efêmera: coletada só na execução; limpa após o lançamento.
        creds = self._current_creds()

        self.log_output.clear_log()
        self.log_output.set_session_status("connecting")
        self._set_run_button_enabled(False)
        self.stop_button.setEnabled(True)

        plan = self.build_specs_for_execution()
        if not plan:
            self.log_output.append_log(self.tr("Nenhum comando para executar."))
            self._set_run_button_enabled(True)
            self.stop_button.setEnabled(False)
            creds.clear()
            return

        try:
            result = self._execution_service.launch_plan(
                plan, passwords=creds.passwords, creds=creds
            )
            # Mensagem de status já vai ao log via log_fn do serviço
            if not result.robocopy_started and not result.ok:
                self._set_run_button_enabled(True)
                self.stop_button.setEnabled(False)
                self.log_output.set_session_status("error")
        finally:
            creds.clear()

    def on_process_finished(self, exit_code):
        # Robocopy de um .ps1/.bat termina antes do PsExec: não liberar a UI.
        if self._execution_service.awaiting_followup or self.executor.is_busy:
            return
        self.stop_button.setEnabled(False)
        self._set_run_button_enabled(True)
        self.log_output.set_interactive(False)
        self.log_output.set_session_status("error" if exit_code else "exited")
        self.log_output.append_log(self.tr(f"Processo finalizado com código {exit_code}"))

    def _on_console_interactive(self, active: bool) -> None:
        self.log_output.set_interactive(active)
        if not active:
            return
        cmd_session = (
            self.tabs.currentWidget() == self.cmd_tab and self.cmd_tab.mode_k.isChecked()
        )
        ps_session = (
            self.tabs.currentWidget() == self.powershell_tab
            and self.powershell_tab.is_session_mode()
        )
        self.log_output.set_session_status(
            "session" if (cmd_session or ps_session) else "running"
        )

    def _on_console_session_exit(self) -> None:
        if not self.executor.send_input("exit"):
            self.executor.stop()

    def on_stop(self):
        self._execution_service.cancel_plan()
        self.executor.stop()
        self.log_output.set_interactive(False)
        self.log_output.set_session_status("exited")
        self._set_run_button_enabled(True)
        self.stop_button.setEnabled(False)
        self.log_output.append_log(
            self.tr(
                "Parada solicitada: processo local encerrado. "
                "Se a operação já havia sido enviada via PsExec, "
                "o processo remoto pode continuar em execução."
            )
        )

    def on_remote_cmd_changed(self, text):
        # Evita loop de atualização
        if getattr(self, '_updating_remote_cmd', False):
            return
        # Desabilita o botão de browser se o campo de comando remoto não estiver vazio
        self.file_selector.browse_button.setEnabled(text.strip() == "")

    def on_remote_cmd_edit_changed(self, text):
        selected_file = self.file_selector.selected_file
        is_msi = selected_file and selected_file.lower().endswith('.msi')
        is_exe = selected_file and selected_file.lower().endswith('.exe')
        # Não há mais checkbox manual, apenas atualizar abas e comando
        self.update_tab_visibility(is_msi, is_exe)
        self._sync_psexec_copy_allowed()
        self.update_command()

    def closeEvent(self, event):
        # Encerra workers Qt antes de destruir widgets (evita
        # "QThread: Destroyed while thread is still running").
        for tab in (
            getattr(self, "appsearch_tab", None),
            getattr(self, "hostapps_tab", None),
            getattr(self, "winget_tab", None),
            getattr(self, "psinfo_tab", None),
        ):
            if tab is not None:
                try:
                    tab.shutdown(wait_ms=10000)
                except Exception:
                    pass
        if hasattr(self, "psexec_tab") and self.psexec_tab is not None:
            for attr in ("_host_status_worker", "_session_worker", "_domain_worker"):
                worker = getattr(self.psexec_tab, attr, None)
                if worker is not None and worker.isRunning():
                    try:
                        worker.wait(2000)
                    except Exception:
                        pass
        self.executor.stop()
        if hasattr(self.executor, "shutdown"):
            try:
                self.executor.shutdown(wait=False)
            except Exception:
                pass
        elif hasattr(self.executor, 'executor') and hasattr(self.executor.executor, 'shutdown'):
            try:
                self.executor.executor.shutdown(wait=False)
            except Exception:
                pass
        super().closeEvent(event)

