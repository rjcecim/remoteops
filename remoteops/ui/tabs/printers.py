"""Aba Impressoras — instalação silenciosa de impressoras de rede.

Tela especial/fullscreen. Preview e Console compartilhados do PsExec
permanecem na aba PsExec.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from remoteops.services.ops import CredentialContext
from remoteops.services.printers import (
    InstallPrinterRequest,
    InstallPrinterResult,
    PrinterService,
)
from remoteops.ui.style import (
    COLOR_BORDER,
    COLOR_SURFACE_MUTED,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    FONT_MONO,
    RADIUS_MEDIUM,
    SIZE_MONO,
    SIZE_UI_SMALL,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    make_card_stack,
)
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.spinner import LoadingEllipsisLabel
from remoteops.ui.widgets.status_dot import STATUS_COLORS, StatusDot
from remoteops.ui.widgets.table import configure_standard_table, pause_table_sorting
from remoteops.utils.installed_printers import (
    MSG_IDENTIFYING,
    MSG_INVALID_HOST,
    MSG_OFFLINE,
    InstalledPrintersPayload,
    display_or_dash,
    installed_query_readiness,
    result_belongs_to_current,
    session_account,
)
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.printer_settings import (
    PRINT_SERVER_REQUIRED_MSG,
    get_print_server,
)
from remoteops.utils.printers import (
    SCOPE_ALL,
    SCOPE_USER,
    NetworkPrinter,
    active_sessions,
    can_install_printer,
    effective_set_default,
    install_block_reason,
    interactive_sessions,
    is_session_active,
    logical_printui_preview,
    new_operation_id,
    print_server_unc,
    printer_matches_query,
    printer_unc,
    qualify_interactive_user,
    select_default_active_session,
    share_status_label,
)
from remoteops.utils.redaction import redact_command_text
from remoteops.utils.sessions import RemoteSession, list_remote_sessions

LOG_PREFIX = "[IMPRESSORAS]"
_EMPTY_FACT = "—"
_TAB_AVAILABLE = 0
_TAB_INSTALLED = 1

_COMMAND_QSS = f"""
QLabel#printersCommandPreview {{
    background-color: {COLOR_SURFACE_MUTED};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MEDIUM}px;
    padding: 4px 8px;
    color: {COLOR_TEXT};
}}
"""
_COMMAND_MUTED_QSS = _COMMAND_QSS.replace(
    f"color: {COLOR_TEXT};", f"color: {COLOR_TEXT_MUTED};"
)


_CAPTION_QSS = f"""
QLabel#printerFactCaption {{
    color: {COLOR_TEXT_SECONDARY};
    font-size: {SIZE_UI_SMALL}pt;
    background: transparent;
    border: none;
}}
"""


def _caption_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("printerFactCaption")
    lbl.setStyleSheet(_CAPTION_QSS)
    return lbl


def _value_label() -> QLabel:
    lbl = QLabel(_EMPTY_FACT)
    lbl.setObjectName("printerFactValue")
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    lbl.setMinimumHeight(lbl.fontMetrics().height())
    return lbl


def _set_fact_value(label: QLabel, text: str, *, muted: bool | None = None) -> None:
    value = (text or "").strip() or _EMPTY_FACT
    label.setText(value)
    label.setToolTip(value if value != _EMPTY_FACT else "")
    if muted is None:
        muted = value == _EMPTY_FACT
    label.setStyleSheet(
        f"color: {COLOR_TEXT_MUTED if muted else COLOR_TEXT}; background: transparent;"
    )


def _fact_column(caption: str) -> tuple[QWidget, QLabel]:
    """Legenda em cima, valor embaixo — usa a largura da coluna, sem caixa que corte."""
    cell = QWidget()
    cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = QVBoxLayout(cell)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(_caption_label(caption), 0)
    value = _value_label()
    lay.addWidget(value, 0)
    return cell, value


def _column_grid(card: CardWidget, columns: int) -> QGridLayout:
    grid = QGridLayout()
    grid.setContentsMargins(0, 2, 0, 2)
    grid.setHorizontalSpacing(SPACE_MD + 4)
    grid.setVerticalSpacing(SPACE_SM)
    for col in range(columns):
        grid.setColumnStretch(col, 1)
        grid.setColumnMinimumWidth(col, 120)
    card.content_layout.addLayout(grid)
    return grid


class _PrinterListWorker(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._abort = False
        self._service = PrinterService()

    def abort(self) -> None:
        self._abort = True
        self._service.cancel()

    def run(self) -> None:
        printers, error = self._service.list_server_printers(
            should_cancel=lambda: self._abort
        )
        if self._abort:
            return
        if error:
            self.finished_err.emit(error)
            return
        self.finished_ok.emit(printers)


class _InstalledPrintersWorker(QThread):
    finished_payload = pyqtSignal(object)

    def __init__(
        self,
        host: str,
        *,
        username: str = "",
        domain: str = "",
        session_id: Optional[int] = None,
        session_name: str = "",
        creds: Optional[CredentialContext] = None,
        generation: int = 0,
        parent=None,
    ):
        super().__init__(parent)
        self._host = host
        self._username = username
        self._domain = domain
        self._session_id = session_id
        self._session_name = session_name
        self._creds = creds if creds is not None else CredentialContext()
        self._generation = generation
        self._abort = False
        self._service = PrinterService()

    def abort(self) -> None:
        self._abort = True
        self._service.cancel()

    def run(self) -> None:
        try:
            payload = self._service.list_host_installed_printers(
                self._host,
                username=self._username,
                domain=self._domain,
                session_id=self._session_id,
                session_name=self._session_name,
                creds=self._creds,
                should_cancel=lambda: self._abort,
            )
            payload.generation = self._generation
            if self._abort:
                return
            self.finished_payload.emit(payload)
        finally:
            self._creds.clear()


class _SessionListWorker(QThread):
    result = pyqtSignal(str, object, str)

    def __init__(self, host: str, user: str = "", password: str = "", parent=None):
        super().__init__(parent)
        self._host = host
        self._user = user
        self._password = password

    def run(self) -> None:
        try:
            sessions, error = list_remote_sessions(
                self._host,
                user=self._user,
                password=self._password,
                allow_psexec_fallback=False,
            )
            self.result.emit(self._host, sessions, error)
        finally:
            self._password = ""


class _InstallWorker(QThread):
    log_line = pyqtSignal(str)
    finished_result = pyqtSignal(object)

    def __init__(
        self,
        request: InstallPrinterRequest,
        creds: CredentialContext,
        parent=None,
    ):
        super().__init__(parent)
        self._request = request
        self._creds = creds
        self._abort = False
        self._service = PrinterService()

    def abort(self) -> None:
        self._abort = True
        self._service.cancel()

    def run(self) -> None:
        try:
            result = self._service.install(
                self._request,
                self._creds,
                progress=self.log_line.emit,
                should_cancel=lambda: self._abort,
                passwords=self._creds.passwords,
            )
            self.finished_result.emit(result)
        finally:
            self._creds.clear()


class PrintersTab(QWidget):
    def __init__(
        self,
        parent=None,
        *,
        host_source: Optional[QLineEdit] = None,
        creds_provider: Optional[Callable[[], Tuple[str, str]]] = None,
        online_provider: Optional[Callable[[], bool]] = None,
    ):
        super().__init__(parent)
        self._host_source = host_source
        self._creds_provider = creds_provider
        self._online_provider = online_provider
        self._printers: List[NetworkPrinter] = []
        self._installed_printers: List[NetworkPrinter] = []
        self._sessions: List[RemoteSession] = []
        self._sessions_host = ""
        self._sessions_loading = False
        self._selected: Optional[NetworkPrinter] = None
        self._list_worker: Optional[_PrinterListWorker] = None
        self._session_worker: Optional[_SessionListWorker] = None
        self._install_worker: Optional[_InstallWorker] = None
        self._installed_worker: Optional[_InstalledPrintersWorker] = None
        self._installing = False
        self._install_host = ""
        self._install_generation = 0
        self._installed_generation = 0
        self._installed_query_key = ""
        self._closing = False
        self._host_online = bool(online_provider() if online_provider else False)
        self._bottom_stretch_idx = None

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)

        self.catalog_card = self._build_catalog_card()
        self.install_card = self._build_install_card()
        self.params_card = self._build_params_card()
        self.log_output = LogOutputWidget()
        self.log_output.set_layout_stretch(2)
        self.log_output.set_interactive(False)

        root.addWidget(self.catalog_card, 3)
        root.addWidget(self.install_card, 0)
        root.addWidget(self.params_card, 0)
        root.addWidget(self.log_output, 2)

        for card in (self.catalog_card, self.install_card, self.params_card, self.log_output):
            card.collapsedChanged.connect(self._redistribute_expandable_space)
        self._redistribute_expandable_space()

        if self._host_source is not None:
            self._host_source.textChanged.connect(self._on_host_text_changed)

        self.destroyed.connect(self._abort_workers)
        self._refresh_server_chrome()
        self._update_install_panel()
        self._update_installed_facts()
        self._refresh_actions()

    def start_initial_load(self) -> None:
        host = self._get_host()
        self._log(f"Host remoto: {host or '—'}.")
        self.refresh_printers()
        self.refresh_sessions()

    def set_host_online(self, online: bool) -> None:
        self._host_online = bool(online)
        if not online:
            if self._installing:
                self._cancel_install()
            self._invalidate_sessions()
            self._invalidate_installed(MSG_OFFLINE)
        self._refresh_actions()

    def sync_from_host(self) -> None:
        host = self._get_host()
        if host.casefold() != (self._sessions_host or "").casefold():
            if self._installing and self._install_host.casefold() != host.casefold():
                self._cancel_install()
            self._invalidate_sessions()
            self._invalidate_installed("")
            if self._is_online():
                self.refresh_sessions()
            else:
                self._set_installed_status(
                    MSG_OFFLINE if host else MSG_INVALID_HOST
                )
        self._refresh_actions()

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._closing = True
        try:
            self.destroyed.disconnect(self._abort_workers)
        except TypeError:
            pass
        if self._host_source is not None:
            try:
                self._host_source.textChanged.disconnect(self._on_host_text_changed)
            except TypeError:
                pass
        if self._installing:
            self._cancel_install()
        self._abort_workers()
        remaining = max(0, int(wait_ms))
        for attr in ("_list_worker", "_session_worker", "_install_worker", "_installed_worker"):
            worker = getattr(self, attr, None)
            if worker is None:
                continue
            try:
                worker.log_line.disconnect(self._on_install_log)
            except (TypeError, AttributeError):
                pass
            try:
                worker.finished_result.disconnect()
            except (TypeError, AttributeError):
                pass
            try:
                worker.finished_ok.disconnect()
            except (TypeError, AttributeError):
                pass
            try:
                worker.finished_err.disconnect()
            except (TypeError, AttributeError):
                pass
            try:
                worker.finished_payload.disconnect()
            except (TypeError, AttributeError):
                pass
            try:
                worker.result.disconnect()
            except (TypeError, AttributeError):
                pass
            try:
                if worker.isRunning() and remaining > 0:
                    worker.wait(remaining)
            except Exception:
                pass
            setattr(self, attr, None)

    def _ui_alive(self) -> bool:
        return not sip.isdeleted(self) and not self._closing

    def _get_host(self) -> str:
        if self._host_source is None or sip.isdeleted(self._host_source):
            return ""
        return normalize_host(self._host_source.text() or "")

    def _creds(self) -> Tuple[str, str]:
        if self._creds_provider is None:
            return "", ""
        try:
            user, password = self._creds_provider()
            return (user or "").strip(), password or ""
        except Exception:
            return "", ""

    def _is_online(self) -> bool:
        host = self._get_host()
        if not host or not is_valid_host(host):
            return False
        if self._online_provider is not None:
            try:
                return bool(self._online_provider())
            except Exception:
                return False
        return bool(self._host_online)

    def _domain_hint(self) -> str:
        user, _password = self._creds()
        if "\\" in user:
            return user.split("\\", 1)[0]
        session = self._selected_session()
        if session is not None and "\\" in (session.username or ""):
            return session.username.split("\\", 1)[0]
        return ""

    def _log(self, message: str) -> None:
        _user, password = self._creds()
        text = redact_command_text(message or "", passwords=[password] if password else None)
        if not text.startswith(LOG_PREFIX):
            text = f"{LOG_PREFIX} {text}"
        self.log_output.append_log(text)

    def _build_catalog_card(self) -> CardWidget:
        card = CardWidget("\uE749", self.tr("Impressoras"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(3)
        self.refresh_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar lista de impressoras")
        )
        self.refresh_btn.clicked.connect(self._on_catalog_refresh_clicked)
        card.add_header_button(self.refresh_btn)

        self._inner_tabs = QTabWidget()
        inner_bar = Mdl2TabBar(self._inner_tabs)
        inner_bar.setExpanding(False)
        self._inner_tabs.setTabBar(inner_bar)
        self._inner_tabs.setDocumentMode(True)
        self._inner_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._inner_tabs.currentChanged.connect(self._on_inner_tab_changed)

        available = QWidget()
        available_lay = QVBoxLayout(available)
        available_lay.setContentsMargins(0, SPACE_SM, 0, 0)
        available_lay.setSpacing(SPACE_SM)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        self.server_lbl = QLabel(self.tr("Servidor: não configurado"))
        self.server_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet(
            f"color: palette(windowText); font-size: {SIZE_UI_SMALL}pt; opacity: 0.75;"
        )
        top.addWidget(self.server_lbl, 0)
        top.addStretch()
        top.addWidget(self.count_lbl, 0)
        top_wrap = QWidget()
        top_wrap.setLayout(top)
        available_lay.addWidget(top_wrap, 0)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Buscar impressora..."))
        self.filter_edit.textChanged.connect(self._apply_filter)
        available_lay.addWidget(self.filter_edit, 0)

        self._loading_lbl = LoadingEllipsisLabel(base_text=self.tr("Carregando"))
        self._loading_lbl.setVisible(False)
        load_row = QHBoxLayout()
        load_row.setContentsMargins(0, 6, 0, 6)
        load_row.addStretch()
        load_row.addWidget(self._loading_lbl)
        load_row.addStretch()
        self._spin_wrap = QWidget()
        self._spin_wrap.setLayout(load_row)
        self._spin_wrap.setVisible(False)
        available_lay.addWidget(self._spin_wrap, 0)

        self.table = QTableWidget()
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("Nome"),
                self.tr("Compartilhamento"),
                self.tr("Driver"),
                self.tr("Porta"),
                self.tr("Localização"),
                self.tr("Status"),
            ]
        )
        configure_standard_table(self.table, stretch_columns=(0, 2, 4))
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.table.setMinimumHeight(80)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        available_lay.addWidget(self.table, 1)

        installed = QWidget()
        installed_lay = QVBoxLayout(installed)
        installed_lay.setContentsMargins(0, SPACE_SM, 0, 0)
        installed_lay.setSpacing(SPACE_SM)

        facts = QHBoxLayout()
        facts.setContentsMargins(0, 0, 0, 0)
        facts.setSpacing(SPACE_MD + 4)
        self.installed_host_lbl = QLabel("")
        self.installed_user_lbl = QLabel("")
        self.installed_session_lbl = QLabel("")
        for lbl in (
            self.installed_host_lbl,
            self.installed_user_lbl,
            self.installed_session_lbl,
        ):
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lbl.setStyleSheet(f"color: {COLOR_TEXT}; background: transparent;")
        facts.addWidget(self.installed_host_lbl, 1)
        facts.addWidget(self.installed_user_lbl, 1)
        facts.addWidget(self.installed_session_lbl, 0)
        facts_wrap = QWidget()
        facts_wrap.setLayout(facts)
        installed_lay.addWidget(facts_wrap, 0)

        self.installed_status_lbl = QLabel("")
        self.installed_status_lbl.setWordWrap(True)
        self.installed_status_lbl.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {SIZE_UI_SMALL}pt; background: transparent;"
        )
        installed_lay.addWidget(self.installed_status_lbl, 0)

        inst_top = QHBoxLayout()
        inst_top.setContentsMargins(0, 0, 0, 0)
        inst_top.setSpacing(8)
        self.installed_filter_edit = QLineEdit()
        self.installed_filter_edit.setPlaceholderText(self.tr("Buscar impressora..."))
        self.installed_filter_edit.textChanged.connect(self._apply_installed_filter)
        self.installed_count_lbl = QLabel("")
        self.installed_count_lbl.setStyleSheet(
            f"color: palette(windowText); font-size: {SIZE_UI_SMALL}pt; opacity: 0.75;"
        )
        inst_top.addWidget(self.installed_filter_edit, 1)
        inst_top.addWidget(self.installed_count_lbl, 0)
        inst_top_wrap = QWidget()
        inst_top_wrap.setLayout(inst_top)
        installed_lay.addWidget(inst_top_wrap, 0)

        self._installed_loading_lbl = LoadingEllipsisLabel(
            base_text=self.tr("Carregando")
        )
        self._installed_loading_lbl.setVisible(False)
        inst_load_row = QHBoxLayout()
        inst_load_row.setContentsMargins(0, 6, 0, 6)
        inst_load_row.addStretch()
        inst_load_row.addWidget(self._installed_loading_lbl)
        inst_load_row.addStretch()
        self._installed_spin_wrap = QWidget()
        self._installed_spin_wrap.setLayout(inst_load_row)
        self._installed_spin_wrap.setVisible(False)
        installed_lay.addWidget(self._installed_spin_wrap, 0)

        self.installed_table = QTableWidget()
        self.installed_table.setColumnCount(6)
        self.installed_table.setHorizontalHeaderLabels(
            [
                self.tr("Nome"),
                self.tr("Driver"),
                self.tr("Porta"),
                self.tr("Tipo"),
                self.tr("Localização"),
                self.tr("Padrão"),
            ]
        )
        configure_standard_table(
            self.installed_table,
            stretch_columns=(0, 1, 4),
            fixed_columns={5: 72},
        )
        self.installed_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.installed_table.setMinimumHeight(80)
        installed_lay.addWidget(self.installed_table, 1)

        idx_available = self._inner_tabs.addTab(available, self.tr("Disponíveis"))
        inner_bar.set_tab_meta(idx_available, "\uE8F1")
        idx_installed = self._inner_tabs.addTab(installed, self.tr("Instaladas"))
        inner_bar.set_tab_meta(idx_installed, "\uE7F4")
        self._inner_tabs.setCurrentIndex(_TAB_AVAILABLE)
        card.content_layout.addWidget(self._inner_tabs, 1)
        return card

    def _build_install_card(self) -> CardWidget:
        card = CardWidget("\uE896", self.tr("Instalação"))
        card.set_collapsible(True, collapsed=False)
        card.content_layout.setSpacing(SPACE_XS)

        self.refresh_sessions_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar sessões do host")
        )
        self.refresh_sessions_btn.clicked.connect(self.refresh_sessions)
        card.add_header_button(self.refresh_sessions_btn)
        self.install_btn = card.make_header_button(
            "\uE768", self.tr("Instalar silenciosamente no host remoto")
        )
        self.install_btn.clicked.connect(self._on_install_clicked)
        card.add_header_button(self.install_btn)

        g = _column_grid(card, 3)
        printer_cell, self.printer_name_lbl = _fact_column(self.tr("Impressora"))
        share_cell, self.share_lbl = _fact_column(self.tr("Compartilhamento"))
        driver_cell, self.driver_lbl = _fact_column(self.tr("Driver"))
        g.addWidget(printer_cell, 0, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(share_cell, 0, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(driver_cell, 0, 2, Qt.AlignmentFlag.AlignTop)

        dest_cell = QWidget()
        dest_cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        dest_lay = QVBoxLayout(dest_cell)
        dest_lay.setContentsMargins(0, 0, 0, 0)
        dest_lay.setSpacing(2)
        dest_lay.addWidget(_caption_label(self.tr("Destino")), 0)
        self.radio_user = QRadioButton(self.tr("Usuário atualmente conectado"))
        self.radio_all = QRadioButton(self.tr("Todos os usuários"))
        self.radio_user.setChecked(True)
        self._scope_group = QButtonGroup(self)
        self._scope_group.addButton(self.radio_user)
        self._scope_group.addButton(self.radio_all)
        self.radio_user.toggled.connect(self._on_scope_changed)
        radios = QWidget()
        radios_lay = QHBoxLayout(radios)
        radios_lay.setContentsMargins(0, 0, 0, 0)
        radios_lay.setSpacing(SPACE_MD)
        radios_lay.addWidget(self.radio_user, 0)
        radios_lay.addWidget(self.radio_all, 0)
        radios_lay.addStretch(1)
        dest_lay.addWidget(radios, 0)
        self.default_check = QCheckBox(self.tr("Definir como padrão"))
        self.default_check.setToolTip(
            self.tr(
                "Executa /y no token do usuário conectado. "
                "Não existe equivalente global para todos os usuários."
            )
        )
        self.default_check.toggled.connect(self._update_params_card)
        dest_lay.addWidget(self.default_check, 0)
        g.addWidget(dest_cell, 1, 0, 1, 2, Qt.AlignmentFlag.AlignTop)

        user_cell = QWidget()
        user_cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        user_lay = QVBoxLayout(user_cell)
        user_lay.setContentsMargins(0, 0, 0, 0)
        user_lay.setSpacing(2)
        user_lay.addWidget(_caption_label(self.tr("Usuário")), 0)
        self.session_state_dot = StatusDot(diameter=8)
        self.session_summary_lbl = _value_label()
        self.session_combo = FluentComboBox()
        self.session_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.session_combo.currentIndexChanged.connect(self._on_session_changed)
        session_row = QWidget()
        self._session_summary_row = session_row
        session_row_lay = QHBoxLayout(session_row)
        session_row_lay.setContentsMargins(0, 0, 0, 0)
        session_row_lay.setSpacing(SPACE_XS)
        session_row_lay.addWidget(
            self.session_state_dot, 0, Qt.AlignmentFlag.AlignTop
        )
        session_row_lay.addWidget(self.session_summary_lbl, 1)
        user_lay.addWidget(session_row, 0)
        user_lay.addWidget(self.session_combo, 0)
        self.session_combo.hide()
        g.addWidget(user_cell, 1, 2, Qt.AlignmentFlag.AlignTop)
        return card

    def _build_params_card(self) -> CardWidget:
        card = CardWidget("\uE943", self.tr("Parâmetros PrintUIEntry"))
        card.set_collapsible(True, collapsed=False)
        card.set_copyable(True)
        card.copyRequested.connect(self._copy_printui_command)
        card.content_layout.setSpacing(SPACE_XS)

        g = _column_grid(card, 3)
        op_cell, self.param_op_lbl = _fact_column(self.tr("Operação"))
        flags_cell, self.param_flags_lbl = _fact_column(self.tr("Parâmetros"))
        driver_cell, self.param_driver_lbl = _fact_column(self.tr("Driver"))
        g.addWidget(op_cell, 0, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(flags_cell, 0, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(driver_cell, 0, 2, Qt.AlignmentFlag.AlignTop)

        unc_cell, self.param_unc_lbl = _fact_column(self.tr("Impressora"))
        user_cell, self.param_user_lbl = _fact_column(self.tr("Usuário"))
        sid_cell, self.param_session_lbl = _fact_column(self.tr("Sessão"))
        g.addWidget(unc_cell, 1, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(user_cell, 1, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(sid_cell, 1, 2, Qt.AlignmentFlag.AlignTop)

        note_cell, self.param_note_lbl = _fact_column(self.tr("Sessão ativa"))
        g.addWidget(note_cell, 2, 0, 1, 3, Qt.AlignmentFlag.AlignTop)
        self._param_note_cell = note_cell
        self._param_note_cell.hide()

        self.command_preview = _value_label()
        self.command_preview.setObjectName("printersCommandPreview")
        self.command_preview.setFont(QFont(FONT_MONO, SIZE_MONO))
        self.command_preview.setStyleSheet(_COMMAND_QSS)
        card.content_layout.addWidget(self.command_preview, 0)
        return card

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is None:
            return
        open_cards = []
        for w, stretch in (
            (self.catalog_card, 3),
            (self.install_card, 0),
            (self.params_card, 0),
            (self.log_output, 2),
        ):
            idx = lay.indexOf(w)
            if idx < 0:
                continue
            if w.is_collapsed:
                lay.setStretch(idx, 0)
            else:
                used = stretch if stretch > 0 else 0
                if used:
                    open_cards.append(w)
                    w.set_layout_stretch(used)
                lay.setStretch(idx, used)
        need_tail = len(open_cards) == 0
        if need_tail:
            if self._bottom_stretch_idx is None:
                lay.addStretch(1)
                self._bottom_stretch_idx = lay.count() - 1
            else:
                lay.setStretch(self._bottom_stretch_idx, 1)
        elif self._bottom_stretch_idx is not None:
            lay.setStretch(self._bottom_stretch_idx, 0)
        lay.activate()
        self.updateGeometry()

    def _abort_workers(self, _destroyed: object = None) -> None:
        if sip.isdeleted(self):
            return
        for worker in (
            self._list_worker,
            self._session_worker,
            self._install_worker,
            self._installed_worker,
        ):
            if worker is None:
                continue
            try:
                if hasattr(worker, "abort"):
                    worker.abort()
            except Exception:
                pass

    def _on_host_text_changed(self, *_args) -> None:
        if not self._ui_alive():
            return
        self.sync_from_host()

    def _share_unc(self, share_name: str) -> str:
        return printer_unc(share_name, get_print_server())

    def _server_configured(self) -> bool:
        return bool(get_print_server())

    def _refresh_server_chrome(self) -> None:
        if not self._ui_alive():
            return
        server = get_print_server()
        if server:
            unc = print_server_unc(server)
            self.server_lbl.setText(self.tr(f"Servidor: {unc}"))
        else:
            self.server_lbl.setText(self.tr("Servidor: não configurado"))
        self._update_refresh_tooltip()

    def on_print_server_changed(self) -> None:
        """Atualiza rótulos e recarrega o catálogo quando o Settings muda."""
        if not self._ui_alive():
            return
        self._refresh_server_chrome()
        self._update_install_panel()
        self._refresh_actions()
        if self._server_configured():
            self.refresh_printers()
            return
        self._printers = []
        self._apply_filter()
        self.count_lbl.setText(self.tr("Servidor não configurado"))
        self._log(PRINT_SERVER_REQUIRED_MSG.replace("\n\n", " "))

    def refresh_printers(self) -> None:
        if not self._ui_alive():
            return
        if self._list_worker is not None and self._list_worker.isRunning():
            return
        self._refresh_server_chrome()
        server = get_print_server()
        if not server:
            self._printers = []
            self._apply_filter()
            self.count_lbl.setText(self.tr("Servidor não configurado"))
            self._log(PRINT_SERVER_REQUIRED_MSG.replace("\n\n", " "))
            return
        self._set_listing(True)
        self._log(f"Consultando impressoras em {print_server_unc(server)}...")
        self._list_worker = _PrinterListWorker(parent=self)
        self._list_worker.finished_ok.connect(self._on_printers_ok)
        self._list_worker.finished_err.connect(self._on_printers_err)
        self._list_worker.finished.connect(self._on_list_worker_finished)
        self._list_worker.start()

    def _set_listing(self, loading: bool) -> None:
        self._loading_lbl.setVisible(loading)
        self._spin_wrap.setVisible(loading)
        if loading:
            self._loading_lbl.start()
            self.count_lbl.setText("")
        else:
            self._loading_lbl.stop()
        self._update_refresh_enabled()

    def _on_catalog_refresh_clicked(self) -> None:
        if not self._ui_alive():
            return
        if self._inner_tabs.currentIndex() == _TAB_INSTALLED:
            self.refresh_installed_printers(force=True)
            return
        self.refresh_printers()

    def _on_inner_tab_changed(self, _index: int) -> None:
        if not self._ui_alive():
            return
        self._update_refresh_tooltip()
        self._update_refresh_enabled()
        if self._inner_tabs.currentIndex() == _TAB_INSTALLED:
            self._update_installed_facts()
            if self._sessions_loading:
                self._set_installed_status(MSG_IDENTIFYING)
            elif not self._installed_printers and not self._installed_worker_running():
                self._maybe_refresh_installed(force=False)

    def _installed_worker_running(self) -> bool:
        worker = self._installed_worker
        return worker is not None and worker.isRunning()

    def _update_refresh_tooltip(self) -> None:
        if not self._ui_alive():
            return
        if self._inner_tabs.currentIndex() == _TAB_INSTALLED:
            host = self._get_host() or "—"
            self.refresh_btn.setToolTip(
                self.tr(f"Atualizar impressoras instaladas em {host}")
            )
            return
        server = get_print_server()
        if server:
            unc = print_server_unc(server)
            self.refresh_btn.setToolTip(self.tr(f"Atualizar impressoras de {unc}"))
            return
        self.refresh_btn.setToolTip(
            self.tr("Configure o servidor de impressão em Configurações")
        )

    def _update_refresh_enabled(self) -> None:
        if not self._ui_alive():
            return
        if self._inner_tabs.currentIndex() == _TAB_INSTALLED:
            busy = self._installed_worker_running()
        else:
            busy = self._list_worker is not None and self._list_worker.isRunning()
        self.refresh_btn.setEnabled(not busy)

    def _set_installed_listing(self, loading: bool) -> None:
        self._installed_loading_lbl.setVisible(loading)
        self._installed_spin_wrap.setVisible(loading)
        if loading:
            self._installed_loading_lbl.start()
            self.installed_count_lbl.setText("")
        else:
            self._installed_loading_lbl.stop()
        self._update_refresh_enabled()

    def _set_installed_status(self, message: str) -> None:
        self.installed_status_lbl.setText((message or "").strip())

    def _update_installed_facts(self, session: Optional[RemoteSession] = None) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        used = session if session is not None else self._selected_session()
        user = session_account(used) if used is not None else ""
        session_id = used.session_id if used is not None else None
        self.installed_host_lbl.setText(self.tr(f"Host: {host or _EMPTY_FACT}"))
        self.installed_user_lbl.setText(self.tr(f"Usuário: {user or _EMPTY_FACT}"))
        self.installed_session_lbl.setText(
            self.tr(f"Sessão: {session_id if session_id is not None else _EMPTY_FACT}")
        )

    def _invalidate_installed(self, message: str = "") -> None:
        self._installed_generation += 1
        self._installed_query_key = ""
        worker = self._installed_worker
        if worker is not None:
            try:
                worker.abort()
            except Exception:
                pass
        self._installed_printers = []
        self._apply_installed_filter()
        self._set_installed_listing(False)
        self._update_installed_facts(None)
        self._set_installed_status(message)

    def _maybe_refresh_installed(self, *, force: bool = False) -> None:
        if not self._ui_alive():
            return
        status, session = installed_query_readiness(
            host=self._get_host(),
            online=self._is_online(),
            sessions=self._sessions,
            selected_session=self._selected_session(),
            sessions_loading=self._sessions_loading,
        )
        self._update_installed_facts(session)
        if session is None:
            self._installed_query_key = ""
            if self._installed_worker_running():
                self._invalidate_installed(status)
            else:
                self._installed_printers = []
                self._apply_installed_filter()
                self._set_installed_listing(False)
                self._set_installed_status(status)
                if status and status != MSG_IDENTIFYING and force:
                    self._log(status)
            return
        key = (
            f"{self._get_host().casefold()}|{session.session_id}|"
            f"{session_account(session).casefold()}"
        )
        if not force and key == self._installed_query_key and self._installed_printers:
            self._set_installed_status("")
            return
        if not force and self._installed_worker_running() and key == self._installed_query_key:
            return
        self.refresh_installed_printers(force=True, session=session)

    def refresh_installed_printers(
        self,
        force: bool = False,
        session: Optional[RemoteSession] = None,
    ) -> None:
        if not self._ui_alive():
            return
        status, chosen = installed_query_readiness(
            host=self._get_host(),
            online=self._is_online(),
            sessions=self._sessions,
            selected_session=session or self._selected_session(),
            sessions_loading=self._sessions_loading,
        )
        self._update_installed_facts(chosen)
        if chosen is None:
            if self._installed_worker_running():
                self._invalidate_installed(status)
            else:
                self._installed_printers = []
                self._apply_installed_filter()
                self._set_installed_listing(False)
                self._set_installed_status(status)
                if status:
                    self._log(status)
            return
        if self._installed_worker_running() and not force:
            return
        if self._installed_worker_running():
            try:
                self._installed_worker.abort()
            except Exception:
                pass
        self._installed_generation += 1
        generation = self._installed_generation
        host = self._get_host()
        account = session_account(chosen)
        self._installed_query_key = (
            f"{host.casefold()}|{chosen.session_id}|{account.casefold()}"
        )
        self._set_installed_status("")
        self._set_installed_listing(True)
        self._log(
            f"Consultando impressoras instaladas em {host} "
            f"({account or '—'}, sessão {chosen.session_id})..."
        )
        user, password = self._creds()
        creds = CredentialContext(user=user, password=password)
        worker = _InstalledPrintersWorker(
            host,
            username=chosen.username,
            domain=chosen.domain,
            session_id=chosen.session_id,
            session_name=chosen.name,
            creds=creds,
            generation=generation,
            parent=self,
        )
        self._installed_worker = worker
        worker.finished_payload.connect(self._on_installed_payload)
        worker.finished.connect(self._on_installed_worker_finished)
        worker.start()

    def _on_installed_worker_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_installed_listing(False)

    def _on_installed_payload(self, payload: object) -> None:
        if not self._ui_alive():
            return
        if not isinstance(payload, InstalledPrintersPayload):
            return
        if not result_belongs_to_current(
            result_host=payload.host,
            current_host=self._get_host(),
            result_generation=payload.generation,
            current_generation=self._installed_generation,
        ):
            return
        self._installed_printers = list(payload.printers or [])
        self._apply_installed_filter()
        self._update_installed_facts(self._selected_session())
        if payload.username:
            self.installed_user_lbl.setText(self.tr(f"Usuário: {payload.username}"))
        if payload.session_id is not None:
            self.installed_session_lbl.setText(
                self.tr(f"Sessão: {payload.session_id}")
            )
        warnings = [w for w in (payload.warnings or []) if str(w).strip()]
        if not payload.ok:
            err = (payload.error or "").strip() or self.tr(
                "Falha na consulta de impressoras instaladas."
            )
            self.installed_count_lbl.setText(self.tr("Falha na consulta"))
            self._set_installed_status(err)
            self._log(err)
            for warning in warnings:
                if warning != err:
                    self._log(warning)
            return
        if payload.partial:
            note = warnings[0] if warnings else self.tr("Consulta parcial.")
            self._set_installed_status(note)
            for warning in warnings:
                self._log(warning)
            self._log(
                f"{len(self._installed_printers)} impressoras instaladas "
                f"(resultado parcial) em {payload.host}."
            )
            return
        self._set_installed_status("")
        self._log(
            f"{len(self._installed_printers)} impressoras instaladas em {payload.host}."
        )

    def _apply_installed_filter(self) -> None:
        if not self._ui_alive():
            return
        query = self.installed_filter_edit.text()
        shown = [p for p in self._installed_printers if printer_matches_query(p, query)]
        self._populate_installed_table(shown)
        self.installed_count_lbl.setText(
            self.tr(f"{len(shown)} de {len(self._installed_printers)}")
        )

    def _populate_installed_table(self, printers: List[NetworkPrinter]) -> None:
        with pause_table_sorting(self.installed_table):
            self.installed_table.setRowCount(len(printers))
            for row, printer in enumerate(printers):
                values = [
                    display_or_dash(printer.name),
                    display_or_dash(printer.driver_name),
                    display_or_dash(printer.port_name),
                    display_or_dash(printer.printer_type),
                    display_or_dash(printer.location),
                    self.tr("Sim") if printer.is_default else _EMPTY_FACT,
                ]
                for col, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, printer)
                    self.installed_table.setItem(row, col, item)

    def _on_list_worker_finished(self) -> None:
        if not self._ui_alive():
            return
        self._set_listing(False)

    def _on_printers_ok(self, printers: object) -> None:
        if not self._ui_alive():
            return
        items: List[NetworkPrinter] = []
        if isinstance(printers, list):
            items = [p for p in printers if isinstance(p, NetworkPrinter)]
        items.sort(key=lambda p: (p.name or p.share_name or "").casefold())
        self._printers = items
        self._apply_filter()
        self._log(f"{len(items)} impressoras encontradas.")

    def _on_printers_err(self, error: str) -> None:
        if not self._ui_alive():
            return
        self._printers = []
        self._apply_filter()
        self.count_lbl.setText(self.tr("Falha na consulta"))
        text = (error or "").strip()
        if text[:1] in "{[":
            text = "Falha ao consultar o servidor de impressão."
        self._log(text or "Falha ao consultar o servidor de impressão.")

    def _apply_filter(self) -> None:
        query = self.filter_edit.text()
        shown = [p for p in self._printers if printer_matches_query(p, query)]
        selected_share = (
            (self._selected.share_name or self._selected.name) if self._selected else ""
        )
        self._populate_table(shown)
        self.count_lbl.setText(self.tr(f"{len(shown)} de {len(self._printers)}"))
        if selected_share:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                printer = item.data(Qt.ItemDataRole.UserRole) if item else None
                if not isinstance(printer, NetworkPrinter):
                    continue
                key = printer.share_name or printer.name
                if key == selected_share:
                    self.table.selectRow(row)
                    break

    def _populate_table(self, printers: List[NetworkPrinter]) -> None:
        with pause_table_sorting(self.table):
            self.table.setRowCount(len(printers))
            for row, printer in enumerate(printers):
                values = [
                    printer.name,
                    printer.share_name or "—",
                    printer.driver_name,
                    printer.port_name,
                    printer.location,
                    share_status_label(printer),
                ]
                for col, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, printer)
                    self.table.setItem(row, col, item)
        if not printers:
            self._selected = None
            self._update_install_panel()

    def _on_selection_changed(self) -> None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        printer = None
        if rows:
            item = self.table.item(rows[0].row(), 0)
            data = item.data(Qt.ItemDataRole.UserRole) if item else None
            if isinstance(data, NetworkPrinter):
                printer = data
        self._selected = printer
        self._update_install_panel()

    def _current_scope(self) -> str:
        return SCOPE_ALL if self.radio_all.isChecked() else SCOPE_USER

    def _selected_session(self) -> Optional[RemoteSession]:
        if self.session_combo.isVisible():
            data = self.session_combo.currentData()
            return data if isinstance(data, RemoteSession) else None
        chosen = select_default_active_session(self._sessions)
        if chosen is not None:
            return chosen
        active = active_sessions(self._sessions)
        return active[0] if len(active) == 1 else None

    def _on_scope_changed(self, *_args) -> None:
        if self._current_scope() != SCOPE_USER:
            self.default_check.setChecked(False)
        self._update_install_panel()

    def _on_session_changed(self, *_args) -> None:
        self._update_install_panel()
        self._maybe_refresh_installed(force=False)

    def _invalidate_sessions(self) -> None:
        self._sessions_host = ""
        self._sessions = []
        self._sessions_loading = False
        self._fill_session_widgets()

    def refresh_sessions(self) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if not host or not is_valid_host(host) or not self._is_online():
            self._invalidate_sessions()
            self._invalidate_installed(
                MSG_OFFLINE if host else MSG_INVALID_HOST
            )
            self._refresh_actions()
            return
        if self._session_worker is not None and self._session_worker.isRunning():
            return
        user, password = self._creds()
        self.refresh_sessions_btn.setEnabled(False)
        self._sessions_loading = True
        if self._inner_tabs.currentIndex() == _TAB_INSTALLED:
            self._set_installed_status(MSG_IDENTIFYING)
        self._session_worker = _SessionListWorker(
            host, user=user, password=password, parent=self
        )
        self._session_worker.result.connect(self._on_sessions_result)
        self._session_worker.finished.connect(self._on_session_worker_finished)
        self._session_worker.start()

    def _on_session_worker_finished(self) -> None:
        if not self._ui_alive():
            return
        self._sessions_loading = False
        self.refresh_sessions_btn.setEnabled(True)

    def _on_sessions_result(self, host: str, sessions, error: str) -> None:
        if not self._ui_alive():
            return
        current = self._get_host()
        if host.casefold() != current.casefold():
            return
        self._sessions_loading = False
        valid: List[RemoteSession] = []
        if isinstance(sessions, list):
            valid = [s for s in sessions if isinstance(s, RemoteSession)]
        self._sessions_host = current
        self._sessions = valid
        self._fill_session_widgets()
        if error and not valid:
            self._log(error)
            self._invalidate_installed(error)
            self._refresh_actions()
            return
        if valid:
            active = active_sessions(valid)
            if len(active) == 1:
                session = active[0]
                user = session_account(session) or qualify_interactive_user(
                    session.username, self._domain_hint()
                )
                self._log(
                    f"Usuário ativo: {user} — sessão {session.session_id}."
                )
        self._maybe_refresh_installed(force=True)
        self._refresh_actions()

    def _fill_session_widgets(self) -> None:
        interactive = interactive_sessions(self._sessions)
        active = active_sessions(self._sessions)
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        show_combo = len(active) > 1 or (len(active) != 1 and bool(interactive))
        if len(active) == 1 and len(interactive) > 1:
            show_combo = True
        if show_combo:
            if len(active) != 1:
                self.session_combo.addItem(self.tr("Selecione a sessão"), None)
            for session in interactive:
                user = qualify_interactive_user(session.username, self._domain_hint())
                label = (
                    f"{user} — Sessão {session.session_id} — {session.state or ''}"
                ).strip(" —")
                self.session_combo.addItem(label, session)
            if len(active) == 1:
                for i in range(self.session_combo.count()):
                    data = self.session_combo.itemData(i)
                    if (
                        isinstance(data, RemoteSession)
                        and data.session_id == active[0].session_id
                    ):
                        self.session_combo.setCurrentIndex(i)
                        break
            else:
                self.session_combo.setCurrentIndex(0)
            self.session_combo.show()
            self._session_summary_row.hide()
        else:
            self.session_combo.hide()
            self._session_summary_row.show()
        self.session_combo.blockSignals(False)
        has_active = len(active) > 0
        self.radio_user.setEnabled(has_active and not self._installing)
        if not has_active and self.radio_user.isChecked():
            self.radio_all.blockSignals(True)
            self.radio_all.setChecked(True)
            self.radio_all.blockSignals(False)
        self._update_install_panel()

    def _apply_session_facts(self, session: Optional[RemoteSession]) -> None:
        if session is None:
            _set_fact_value(
                self.session_summary_lbl,
                self.tr("Nenhum usuário conectado"),
                muted=True,
            )
            self.session_state_dot.set_state("idle")
            return
        user = qualify_interactive_user(session.username, self._domain_hint())
        state = session.state or self.tr("Ativa")
        _set_fact_value(
            self.session_summary_lbl,
            self.tr(f"{user}  ·  sessão {session.session_id}  ·  {state}"),
        )
        self.session_state_dot.set_color(
            STATUS_COLORS["ok"] if is_session_active(session) else STATUS_COLORS["idle"]
        )

    def _update_install_panel(self) -> None:
        printer = self._selected
        if printer is None:
            _set_fact_value(self.printer_name_lbl, _EMPTY_FACT)
            _set_fact_value(self.share_lbl, _EMPTY_FACT)
            _set_fact_value(self.driver_lbl, _EMPTY_FACT)
        else:
            _set_fact_value(self.printer_name_lbl, printer.name or _EMPTY_FACT)
            if printer.is_shared:
                try:
                    _set_fact_value(self.share_lbl, self._share_unc(printer.share_name))
                except ValueError:
                    _set_fact_value(
                        self.share_lbl,
                        self.tr("Servidor não configurado"),
                        muted=True,
                    )
            else:
                _set_fact_value(
                    self.share_lbl, self.tr("Não compartilhada"), muted=True
                )
            _set_fact_value(self.driver_lbl, printer.driver_name or _EMPTY_FACT)
        self._apply_session_facts(self._selected_session())
        user_scope = self._current_scope() == SCOPE_USER
        self.default_check.setEnabled(user_scope and not self._installing)
        if not user_scope:
            self.default_check.blockSignals(True)
            self.default_check.setChecked(False)
            self.default_check.blockSignals(False)
        self._update_params_card()
        self._refresh_actions()

    def _update_params_card(self) -> None:
        printer = self._selected
        unc = ""
        if printer is not None and printer.is_shared:
            try:
                unc = self._share_unc(printer.share_name)
            except ValueError:
                unc = ""
        session = self._selected_session()
        user = ""
        session_id = None
        if session is not None:
            user = qualify_interactive_user(session.username, self._domain_hint())
            session_id = session.session_id
        kind = self._current_scope()
        flags = ["/ga", "/q"] if kind == SCOPE_ALL else ["/in", "/q"]
        if effective_set_default(kind, self.default_check.isChecked()):
            flags.append("/y")
        if kind == SCOPE_ALL:
            _set_fact_value(self.param_op_lbl, self.tr("Instalação por computador"))
        else:
            _set_fact_value(self.param_op_lbl, self.tr("Instalar conexão do usuário"))
        _set_fact_value(self.param_flags_lbl, "  ·  ".join(flags))
        _set_fact_value(self.param_unc_lbl, unc, muted=not unc)
        if kind == SCOPE_USER:
            _set_fact_value(self.param_user_lbl, user, muted=not user)
            _set_fact_value(
                self.param_session_lbl,
                "" if session_id is None else str(session_id),
                muted=session_id is None,
            )
        else:
            _set_fact_value(self.param_user_lbl, _EMPTY_FACT, muted=True)
            _set_fact_value(self.param_session_lbl, _EMPTY_FACT, muted=True)
        driver = (printer.driver_name if printer else "") or ""
        _set_fact_value(self.param_driver_lbl, driver, muted=not driver)
        if kind == SCOPE_ALL and is_session_active(session):
            _set_fact_value(
                self.param_note_lbl,
                self.tr("também aplica /in no token do usuário"),
            )
            self._param_note_cell.show()
        else:
            _set_fact_value(self.param_note_lbl, _EMPTY_FACT, muted=True)
            self._param_note_cell.hide()
        preview = logical_printui_preview(
            scope=kind,
            unc=unc,
            set_default=effective_set_default(kind, self.default_check.isChecked()),
        )
        if preview:
            self.command_preview.setText(preview)
            self.command_preview.setToolTip(preview)
            self.command_preview.setStyleSheet(_COMMAND_QSS)
        else:
            placeholder = self.tr(
                "Selecione uma impressora compartilhada para ver o comando lógico."
            )
            self.command_preview.setText(placeholder)
            self.command_preview.setToolTip("")
            self.command_preview.setStyleSheet(_COMMAND_MUTED_QSS)
        self.install_card.updateGeometry()
        self.params_card.updateGeometry()

    def _copy_printui_command(self) -> None:
        text = (self.command_preview.text() or "").strip()
        if "printuientry" not in text.casefold() and "rundll32" not in text.casefold():
            return
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)

    def _refresh_actions(self) -> None:
        online = self._is_online()
        busy = self._installing
        session = self._selected_session()
        configured = self._server_configured()
        enabled = can_install_printer(
            host_online=online,
            printer=self._selected,
            busy=busy,
            scope=self._current_scope(),
            session=session,
            server_configured=configured,
        )
        self.install_btn.setEnabled(enabled)
        reason = install_block_reason(
            host_online=online,
            printer=self._selected,
            busy=busy,
            scope=self._current_scope(),
            session=session,
            server_configured=configured,
        )
        if busy:
            self.install_btn.setToolTip(self.tr("Instalação em andamento."))
        else:
            self.install_btn.setToolTip(
                reason or self.tr("Instalar silenciosamente no host remoto.")
            )
        self.radio_all.setEnabled(not busy)
        self.radio_user.setEnabled(
            not busy and bool(active_sessions(self._sessions))
        )
        self.session_combo.setEnabled(not busy)
        self.refresh_sessions_btn.setEnabled(not busy)
        self._update_refresh_enabled()

    def _on_install_clicked(self) -> None:
        if self._installing:
            return
        host = self._get_host()
        if not self._is_online() or not host:
            self._log("Host remoto precisa estar Online.")
            return
        printer = self._selected
        if printer is None or not printer.is_shared:
            self._log("Não compartilhada")
            return
        session = self._selected_session()
        scope = self._current_scope()
        if not can_install_printer(
            host_online=True,
            printer=printer,
            busy=False,
            scope=scope,
            session=session,
            server_configured=self._server_configured(),
        ):
            self._log(
                install_block_reason(
                    host_online=True,
                    printer=printer,
                    busy=False,
                    scope=scope,
                    session=session,
                    server_configured=self._server_configured(),
                )
            )
            return
        user, password = self._creds()
        creds = CredentialContext(user=user, password=password)
        request = InstallPrinterRequest(
            host=host,
            printer=printer,
            scope=scope,
            session=session,
            set_default=self.default_check.isChecked(),
            operation_id=new_operation_id(),
            domain_hint=self._domain_hint(),
        )
        self._installing = True
        self._install_host = host
        self._install_generation += 1
        generation = self._install_generation
        self._refresh_actions()
        self._log(f"Host remoto: {host}")
        worker = _InstallWorker(request, creds, parent=self)
        self._install_worker = worker
        worker.log_line.connect(self._on_install_log)
        worker.finished_result.connect(
            lambda result, gen=generation: self._on_install_finished(result, gen)
        )
        worker.finished.connect(self._on_install_thread_finished)
        worker.start()

    def _on_install_log(self, message: str) -> None:
        if not self._ui_alive():
            return
        self._log(message)

    def _on_install_finished(self, result: object, generation: int) -> None:
        if not self._ui_alive() or generation != self._install_generation:
            return
        if isinstance(result, InstallPrinterResult):
            if result.cancelled:
                self._log("Operação cancelada.")
            elif result.ok:
                self._log(result.message or "Instalação concluída.")
                self.refresh_installed_printers(force=True)
            else:
                self._log(result.message or "Falha na instalação.")

    def _on_install_thread_finished(self) -> None:
        if not self._ui_alive():
            return
        self._installing = False
        self._install_host = ""
        self._install_worker = None
        self._refresh_actions()
        self._update_refresh_enabled()

    def _cancel_install(self) -> None:
        worker = self._install_worker
        if worker is None:
            self._installing = False
            self._install_host = ""
            return
        try:
            worker.abort()
        except Exception:
            pass
        self._install_generation += 1
