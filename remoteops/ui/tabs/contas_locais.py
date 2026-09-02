"""Aba Contas Locais — listagem via PowerShell e alteração via PsPasswd."""

from __future__ import annotations

import csv
import io
from typing import Callable, List, Optional, Sequence, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from remoteops.services.local_accounts import LocalAccountsQueryResult, LocalAccountsService
from remoteops.services.passwords import PasswordChangeService, current_operator
from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_BORDER,
    COLOR_SURFACE_MUTED,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    INPUT_HEIGHT,
    RADIUS_MEDIUM,
    RADIUS_SMALL,
    SIZE_UI_SMALL,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    composite_field_qss,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    bind_card_stack,
    make_card_stack,
)
from remoteops.ui.widgets.spinner import DotsSpinner
from remoteops.ui.widgets.status_dot import StatusDot
from remoteops.ui.widgets.table import (
    SortableTableItem,
    configure_standard_table,
    pause_table_sorting,
)
from remoteops.utils.dates import format_now_datetime
from remoteops.utils.local_accounts import (
    BuiltinAccountKind,
    LocalAccount,
    is_probably_local_session_user,
    validate_admin_credential,
    validate_new_password,
)
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.pspasswd import (
    PHASE_LABELS,
    PasswordChangePhase,
    PasswordChangeRequest,
    PasswordChangeResult,
    build_pspasswd_argv,
    preview_pspasswd_argv,
    pspasswd_available,
    resolve_pspasswd_exe,
)
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.redaction import REDACTED

_ACCOUNT_ROLE = Qt.ItemDataRole.UserRole
_EMPTY = "—"
_EYE_GLYPH = "\uE890"


def _eye_button_qss(*, active: bool) -> str:
    color = COLOR_ACCENT if active else COLOR_TEXT_SECONDARY
    hover = COLOR_ACCENT if active else COLOR_TEXT
    return f"""
        QToolButton {{
            border: none;
            background: transparent;
            color: {color};
        }}
        QToolButton:hover {{
            background: transparent;
            color: {hover};
            border-radius: {RADIUS_SMALL}px;
        }}
        """

_CAPTION_QSS = f"""
QLabel#contasFactCaption {{
    color: {COLOR_TEXT_SECONDARY};
    font-size: {SIZE_UI_SMALL}pt;
    background: transparent;
    border: none;
}}
"""

_SELECTION_PANEL_QSS = f"""
QFrame#contasSelectionPanel {{
    background: {COLOR_SURFACE_MUTED};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MEDIUM}px;
}}
"""

_ACTION_PANEL_QSS = f"""
QFrame#contasActionPanel {{
    background: transparent;
    border-top: 1px solid {COLOR_BORDER};
}}
"""

_REVIEW_CMD_QSS = (
    "font-family: Consolas, monospace; font-size: 9pt; "
    f"color: {COLOR_TEXT}; background: transparent;"
)

_REVIEW_PANEL_QSS = f"""
QFrame#contasReviewPanel {{
    background: {COLOR_SURFACE_MUTED};
    border: 1px solid {COLOR_BORDER};
    border-radius: {RADIUS_MEDIUM}px;
}}
"""


def _caption_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("contasFactCaption")
    lbl.setStyleSheet(_CAPTION_QSS)
    return lbl


def _value_label() -> QLabel:
    lbl = QLabel(_EMPTY)
    lbl.setWordWrap(True)
    lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    lbl.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
    return lbl


def _set_fact_value(label: QLabel, text: str, *, muted: bool | None = None) -> None:
    value = (text or "").strip() or _EMPTY
    label.setText(value)
    label.setToolTip(value if value != _EMPTY else "")
    if muted is None:
        muted = value == _EMPTY
    label.setStyleSheet(
        f"color: {COLOR_TEXT_MUTED if muted else COLOR_TEXT}; background: transparent;"
    )


def _fact_column(caption: str) -> tuple[QWidget, QLabel]:
    cell = QWidget()
    cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = QVBoxLayout(cell)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(_caption_label(caption), 0)
    value = _value_label()
    lay.addWidget(value, 0)
    return cell, value


def _status_fact(caption: str) -> tuple[QWidget, StatusDot, QLabel]:
    cell = QWidget()
    cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = QVBoxLayout(cell)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(_caption_label(caption), 0)
    row = QWidget()
    row_lay = QHBoxLayout(row)
    row_lay.setContentsMargins(0, 0, 0, 0)
    row_lay.setSpacing(SPACE_XS)
    dot = StatusDot(diameter=8)
    value = _value_label()
    row_lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
    row_lay.addWidget(value, 1)
    lay.addWidget(row, 0)
    return cell, dot, value


def _muted_label(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color: {COLOR_TEXT_SECONDARY}; font-size: {SIZE_UI_SMALL}pt;"
    )
    lbl.setWordWrap(True)
    return lbl


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


def _pair_row(*cells: QWidget, stretches: tuple[int, ...] | None = None) -> QWidget:
    row = QWidget()
    row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(SPACE_MD + 4)
    weights = stretches or tuple(1 for _ in cells)
    for cell, stretch in zip(cells, weights):
        lay.addWidget(cell, max(1, int(stretch)))
    return row


def _password_field(placeholder: str) -> tuple[QWidget, QLineEdit]:
    """Campo de senha com botão olho dentro da borda (padrão PsExec)."""
    container = QWidget()
    container.setObjectName("contasPasswordField")
    container.setFixedHeight(INPUT_HEIGHT)
    container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    container.setStyleSheet(composite_field_qss("contasPasswordField"))
    layout = QHBoxLayout(container)
    layout.setContentsMargins(8, 0, 4, 0)
    layout.setSpacing(2)

    line_edit = QLineEdit()
    line_edit.setEchoMode(QLineEdit.EchoMode.Password)
    line_edit.setPlaceholderText(placeholder)
    line_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

    eye_btn = QToolButton()
    eye_btn.setText(_EYE_GLYPH)
    eye_btn.setFont(QFont("Segoe MDL2 Assets", 10))
    eye_btn.setFixedSize(22, 22)
    eye_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    eye_btn.setToolTip("Mostrar senha")
    eye_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    eye_btn.setStyleSheet(_eye_button_qss(active=False))

    def _toggle_visibility() -> None:
        show_text = line_edit.echoMode() == QLineEdit.EchoMode.Password
        line_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if show_text else QLineEdit.EchoMode.Password
        )
        eye_btn.setToolTip("Ocultar senha" if show_text else "Mostrar senha")
        eye_btn.setStyleSheet(_eye_button_qss(active=show_text))

    eye_btn.clicked.connect(_toggle_visibility)

    layout.addWidget(line_edit)
    layout.addWidget(eye_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    return container, line_edit


class _QueryWorker(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        host: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        current_host: str = "",
    ):
        super().__init__()
        self.host = host
        self.user = user
        self.password = password
        self.pstools_dir = pstools_dir
        self.current_host = current_host
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        if self._cancel:
            return
        result = LocalAccountsService().query(
            self.host,
            user=self.user,
            password=self.password,
            pstools_dir=self.pstools_dir,
            current_host=self.current_host,
        )
        if self._cancel:
            return
        if result.error and not result.accounts:
            self.finished_err.emit(result.error)
        else:
            self.finished_ok.emit(result)


class _PasswordWorker(QThread):
    progress = pyqtSignal(str)
    item_done = pyqtSignal(object)
    finished_all = pyqtSignal()

    def __init__(
        self,
        requests: Sequence[tuple[PasswordChangeRequest, Optional[LocalAccount]]],
        *,
        new_password: str,
        admin_user: str = "",
        admin_password: str = "",
        pstools_dir: str = "",
        listed_accounts: Sequence[LocalAccount] = (),
        current_host: str = "",
        operator: str = "",
    ):
        super().__init__()
        self.requests = list(requests)
        self.new_password = new_password
        self.admin_user = admin_user
        self.admin_password = admin_password
        self.pstools_dir = pstools_dir
        self.listed_accounts = list(listed_accounts)
        self.current_host = current_host
        self.operator = operator
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        service = PasswordChangeService()
        for request, account in self.requests:
            if self._cancel:
                break
            self.progress.emit(normalize_host(request.host))
            result = service.run(
                request,
                new_password=self.new_password,
                confirmation=self.new_password,
                admin_user=self.admin_user,
                admin_password=self.admin_password,
                pstools_dir=self.pstools_dir,
                listed_accounts=self.listed_accounts,
                account=account,
                should_cancel=lambda: self._cancel,
                current_host=self.current_host,
                operator=self.operator,
            )
            self.item_done.emit(result)
        self.finished_all.emit()


class ContasLocaisTab(QWidget):
    """Contas locais e alteração de senha (PsPasswd)."""

    openConnectivityRequested = pyqtSignal(str)
    openSessoesRequested = pyqtSignal()

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
        self._closing = False
        self._busy = False
        self._accounts: List[LocalAccount] = []
        self._selected_single: Optional[LocalAccount] = None
        self._last_query_at = ""
        self._query_worker: Optional[_QueryWorker] = None
        self._password_worker: Optional[_PasswordWorker] = None
        self._results: List[PasswordChangeResult] = []
        self._pending_hosts: List[str] = []
        self._query_host_snapshot = ""

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)

        self.dest_card = self._build_dest_card()
        self.accounts_card = self._build_accounts_card()
        self.results_card = self._build_results_card()

        root.addWidget(self.dest_card, 0)
        root.addWidget(self.accounts_card, 5)
        root.addWidget(self.results_card, 2)

        self.dest_card.set_layout_stretch(0)
        self.accounts_card.set_layout_stretch(5)
        self.results_card.set_layout_stretch(2)

        bind_card_stack(
            root,
            (self.accounts_card, self.results_card),
        )

        if self._host_source is not None:
            self._host_source.textChanged.connect(self.sync_from_host)
        self.destroyed.connect(self._on_destroyed)
        self.sync_from_host()
        self._refresh_all()

    def _maybe_load_accounts(self) -> None:
        if not self._ui_alive() or self._busy:
            return
        host = self._get_host()
        if not host or not is_valid_host(host):
            return
        if not self._is_online():
            return
        self.load_accounts()

    def _ui_alive(self) -> bool:
        return not self._closing and not sip.isdeleted(self)

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
        if self._online_provider is None:
            return False
        try:
            return bool(self._online_provider())
        except Exception:
            return False

    def set_host_online(self, online: bool) -> None:
        if not self._ui_alive():
            return
        self._apply_host_status(online=bool(online))
        self._refresh_actions()
        if online and not self._busy and not self._accounts:
            self._maybe_load_accounts()

    def sync_from_host(self, *_args) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        prev_host = self._query_host_snapshot
        if self._busy and host.casefold() != (prev_host or "").casefold():
            self._invalidate_busy_host()
        self._apply_host_status(online=self._is_online())
        self._refresh_dest_labels()
        self._refresh_actions()
        if (
            host
            and is_valid_host(host)
            and self._is_online()
            and not self._busy
            and (
                not self._accounts
                or host.casefold() != (prev_host or "").casefold()
            )
        ):
            self.load_accounts()
        elif not self._busy and not self._accounts:
            if not host or not is_valid_host(host):
                self.accounts_status_lbl.setText(
                    self.tr("Informe um host válido na aba PsExec.")
                )
            elif not self._is_online():
                self.accounts_status_lbl.setText(
                    self.tr(
                        "Host offline — as contas serão carregadas automaticamente."
                    )
                )

    def refresh_tool_capabilities(self) -> None:
        self._refresh_dest_labels()
        self._refresh_actions()
        self._refresh_review()

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._closing = True
        self._clear_password_fields()
        for attr in ("_query_worker", "_password_worker"):
            worker = getattr(self, attr, None)
            if worker is None:
                continue
            try:
                if hasattr(worker, "cancel"):
                    worker.cancel()
            except Exception:
                pass
            if worker.isRunning():
                worker.wait(max(0, int(wait_ms)))
            setattr(self, attr, None)

    def _on_destroyed(self, _obj: object = None) -> None:
        self._closing = True
        self.shutdown(wait_ms=2000)

    def focus_account(
        self,
        *,
        username: str = "",
        sid: str = "",
        load: bool = True,
    ) -> None:
        if load and not self._accounts:
            self.load_accounts()
        name = (username or "").strip()
        sid_key = (sid or "").strip().casefold()
        for row in range(self.accounts_table.rowCount()):
            account = self._find_account_in_row(row)
            if account is None:
                continue
            if sid_key and (account.sid or "").casefold() == sid_key:
                self.accounts_table.selectRow(row)
                self._on_account_selection_changed()
                return
            if name and account.name.casefold() == name.casefold():
                self.accounts_table.selectRow(row)
                self._on_account_selection_changed()
                return

    # ── cards ─────────────────────────────────────────────────────────────

    def _build_dest_card(self) -> CardWidget:
        card = CardWidget("\uEA18", self.tr("Destino"))
        card.set_collapsible(True, collapsed=False)
        card.set_layout_stretch(0)
        card.content_layout.setSpacing(SPACE_XS)

        g = _column_grid(card, 4)
        host_cell, self.host_label = _fact_column(self.tr("Host"))
        estado_cell, self.host_status_dot, self.host_status_label = _status_fact(
            self.tr("Estado")
        )
        consulta_cell, self.last_query_label = _fact_column(self.tr("Última consulta"))
        cred_cell, self.cred_label = _fact_column(self.tr("Credencial administrativa"))
        g.addWidget(host_cell, 0, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(estado_cell, 0, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(consulta_cell, 0, 2, Qt.AlignmentFlag.AlignTop)
        g.addWidget(cred_cell, 0, 3, Qt.AlignmentFlag.AlignTop)
        return card

    def _build_accounts_card(self) -> CardWidget:
        card = CardWidget("\uE716", self.tr("Contas locais"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(5)

        self.refresh_accounts_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar contas locais")
        )
        self.refresh_accounts_btn.clicked.connect(self.load_accounts)
        card.add_header_button(self.refresh_accounts_btn)

        self.apply_btn = card.make_header_button(
            "\uE768", self.tr("Aplicar nova senha (PsPasswd)")
        )
        self.apply_btn.clicked.connect(self._on_change_clicked)
        card.add_header_button(self.apply_btn)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(SPACE_SM)
        self.accounts_status_lbl = _muted_label(
            self.tr("Consultando contas locais…")
        )
        self.accounts_status_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        toolbar.addWidget(self.accounts_status_lbl, 1)
        self._accounts_spinner = DotsSpinner()
        self._accounts_spinner.setVisible(False)
        self._accounts_spin_wrap = QWidget()
        spin_lay = QHBoxLayout(self._accounts_spin_wrap)
        spin_lay.setContentsMargins(0, 0, 0, 0)
        spin_lay.addWidget(self._accounts_spinner)
        self._accounts_spin_wrap.setVisible(False)
        toolbar.addWidget(self._accounts_spin_wrap, 0, Qt.AlignmentFlag.AlignVCenter)
        filter_caption = _caption_label(self.tr("Filtrar"))
        filter_caption.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred
        )
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Nome, SID ou estado…"))
        self.filter_edit.setMinimumWidth(180)
        self.filter_edit.textChanged.connect(self._apply_filter)
        toolbar.addWidget(filter_caption, 0, Qt.AlignmentFlag.AlignVCenter)
        toolbar.addWidget(self.filter_edit, 0)
        card.content_layout.addLayout(toolbar)

        self.accounts_table = QTableWidget()
        self._configure_accounts_table()
        self.accounts_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.accounts_table.itemSelectionChanged.connect(
            self._on_account_selection_changed
        )
        self.accounts_table.customContextMenuRequested.connect(
            self._accounts_context_menu
        )
        card.content_layout.addWidget(self.accounts_table, 1)

        self.action_panel = QFrame()
        self.action_panel.setObjectName("contasActionPanel")
        self.action_panel.setStyleSheet(_ACTION_PANEL_QSS)
        action_lay = QVBoxLayout(self.action_panel)
        action_lay.setContentsMargins(0, SPACE_SM, 0, 0)
        action_lay.setSpacing(SPACE_SM)

        self.selection_panel = QFrame()
        self.selection_panel.setObjectName("contasSelectionPanel")
        self.selection_panel.setStyleSheet(_SELECTION_PANEL_QSS)
        panel_lay = QVBoxLayout(self.selection_panel)
        panel_lay.setContentsMargins(10, 8, 10, 8)
        panel_lay.setSpacing(4)
        self.selection_title = QLabel(self.tr("Nenhuma conta selecionada"))
        self.selection_title.setStyleSheet(
            f"color: {COLOR_TEXT}; font-weight: 600; background: transparent;"
        )
        self.selection_detail = _muted_label(
            self.tr("Selecione uma linha na tabela para alterar a senha.")
        )
        panel_lay.addWidget(self.selection_title)
        panel_lay.addWidget(self.selection_detail)
        action_lay.addWidget(self.selection_panel)

        pass_new_cell = QWidget()
        pass_new_lay = QVBoxLayout(pass_new_cell)
        pass_new_lay.setContentsMargins(0, 0, 0, 0)
        pass_new_lay.setSpacing(2)
        pass_new_lay.addWidget(_caption_label(self.tr("Nova senha")), 0)
        new_field, self.new_pass_edit = _password_field(
            self.tr("Nova senha da conta local")
        )
        self.new_pass_field = new_field
        pass_new_lay.addWidget(new_field)

        pass_confirm_cell = QWidget()
        pass_confirm_lay = QVBoxLayout(pass_confirm_cell)
        pass_confirm_lay.setContentsMargins(0, 0, 0, 0)
        pass_confirm_lay.setSpacing(2)
        pass_confirm_lay.addWidget(_caption_label(self.tr("Confirmar nova senha")), 0)
        confirm_field, self.confirm_pass_edit = _password_field(
            self.tr("Repita a nova senha")
        )
        self.confirm_pass_field = confirm_field
        pass_confirm_lay.addWidget(confirm_field)

        action_lay.addWidget(_pair_row(pass_new_cell, pass_confirm_cell), 0)

        self.review_panel = QFrame()
        self.review_panel.setObjectName("contasReviewPanel")
        self.review_panel.setStyleSheet(_REVIEW_PANEL_QSS)
        review_lay = QVBoxLayout(self.review_panel)
        review_lay.setContentsMargins(10, 8, 10, 8)
        review_lay.setSpacing(4)

        review_lay.addWidget(_caption_label(self.tr("Comando")))
        self.review_command_label = QLabel(_EMPTY)
        self.review_command_label.setWordWrap(True)
        self.review_command_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.review_command_label.setStyleSheet(_REVIEW_CMD_QSS)
        review_lay.addWidget(self.review_command_label)

        self.review_panel.hide()
        action_lay.addWidget(self.review_panel, 0)

        card.content_layout.addWidget(self.action_panel, 0)
        return card

    def _build_results_card(self) -> CardWidget:
        card = CardWidget("\uE9D9", self.tr("Resultados"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(2)

        self.stop_pending_btn = card.make_header_button(
            "\uE711", self.tr("Interromper operações pendentes")
        )
        self.stop_pending_btn.clicked.connect(self._stop_pending)
        card.add_header_button(self.stop_pending_btn)
        self.export_csv_btn = card.make_header_button(
            "\uE8A5", self.tr("Exportar CSV sanitizado")
        )
        self.export_csv_btn.clicked.connect(self._export_csv)
        card.add_header_button(self.export_csv_btn)
        self.clear_results_btn = card.make_header_button(
            "\uE74D", self.tr("Limpar resultados")
        )
        self.clear_results_btn.clicked.connect(self._clear_results)
        card.add_header_button(self.clear_results_btn)

        self.results_status_lbl = _muted_label(
            self.tr("Nenhuma alteração executada nesta sessão.")
        )
        card.content_layout.addWidget(self.results_status_lbl, 0)

        self.results_table = QTableWidget()
        self.results_table.setColumnCount(7)
        self.results_table.setHorizontalHeaderLabels(
            [
                self.tr("Host"),
                self.tr("Conta"),
                self.tr("SID"),
                self.tr("Status"),
                self.tr("Detalhe"),
                self.tr("Código"),
                self.tr("Duração"),
            ]
        )
        configure_standard_table(
            self.results_table,
            stretch_columns=(4,),
            fixed_columns={
                0: 112,
                1: 88,
                2: 168,
                3: 82,
                5: 58,
                6: 76,
            },
        )
        self.results_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.results_table.customContextMenuRequested.connect(
            self._results_context_menu
        )
        card.content_layout.addWidget(self.results_table, 1)
        return card


    def _configure_accounts_table(self) -> None:
        headers = [
            self.tr("Conta"),
            self.tr("Nome completo"),
            self.tr("Tipo"),
            self.tr("SID"),
            self.tr("Estado"),
            self.tr("Bloqueio"),
            self.tr("Senha exigida"),
            self.tr("Senha expira"),
            self.tr("Alteração permitida"),
        ]
        self.accounts_table.setColumnCount(len(headers))
        self.accounts_table.setHorizontalHeaderLabels(headers)
        configure_standard_table(
            self.accounts_table,
            stretch_columns=(1,),
            fixed_columns={
                0: 96,
                2: 108,
                3: 168,
                4: 84,
                5: 72,
                6: 96,
                7: 96,
                8: 118,
            },
        )

    # ── estado / UI ───────────────────────────────────────────────────────

    def _apply_host_status(self, *, online: bool) -> None:
        self.host_status_dot.set_color("#2ecc71" if online else "#e74c3c")
        _set_fact_value(
            self.host_status_label,
            self.tr("Online") if online else self.tr("Offline"),
        )

    def _refresh_dest_labels(self) -> None:
        _set_fact_value(self.host_label, self._get_host() or _EMPTY)
        user, _pwd = self._creds()
        _set_fact_value(
            self.cred_label,
            user or self.tr("Contexto Windows atual"),
        )
        _set_fact_value(self.last_query_label, self._last_query_at or _EMPTY, muted=True)

    def _refresh_actions(self) -> None:
        online = self._is_online()
        host_ok = bool(self._get_host()) and is_valid_host(self._get_host())
        can_query = online and host_ok and not self._busy
        self.refresh_accounts_btn.setEnabled(can_query)
        has_pspasswd = pspasswd_available()
        has_selection = self._has_valid_selection()
        can_change = (
            has_pspasswd
            and has_selection
            and not self._busy
            and online
        )
        self.apply_btn.setEnabled(can_change)
        for field in (self.new_pass_field, self.confirm_pass_field):
            field.setEnabled(has_selection and not self._busy)
        if not has_pspasswd:
            self.apply_btn.setToolTip(
                self.tr("PsPasswd não encontrado na pasta PSTools configurada.")
            )
        elif not has_selection:
            self.apply_btn.setToolTip(
                self.tr("Selecione uma conta na tabela para habilitar a alteração.")
            )
        else:
            self.apply_btn.setToolTip("")
        self.stop_pending_btn.setEnabled(self._busy)

    def _has_valid_selection(self) -> bool:
        return self._selected_single is not None

    def _refresh_selection_labels(self) -> None:
        if self._selected_single is None:
            self.selection_title.setText(self.tr("Nenhuma conta selecionada"))
            self.selection_detail.setText(
                self.tr("Selecione uma linha na tabela para alterar a senha.")
            )
            return
        acc = self._selected_single
        self.selection_title.setText(f"{acc.name} — {acc.type_label}")
        detail_parts = [
            self.tr(f"Host: {acc.host}"),
            self.tr(f"SID: {acc.sid or '—'}"),
            self.tr(f"Estado: {acc.state_label}"),
        ]
        self.selection_detail.setText(" · ".join(detail_parts))

    def _update_accounts_status(self) -> None:
        if self._busy:
            return
        count = len(self._accounts)
        if count:
            self.accounts_status_lbl.setText(
                self.tr(f"{count} conta(s) local(is) carregada(s). Selecione uma linha.")
            )
        else:
            self.accounts_status_lbl.setText(
                self.tr("Nenhuma conta local encontrada neste host.")
            )

    def _set_accounts_loading(self, loading: bool) -> None:
        self._accounts_spinner.setVisible(loading)
        self._accounts_spin_wrap.setVisible(loading)
        if loading:
            self.accounts_status_lbl.setText(self.tr("Consultando contas locais…"))

    def _refresh_review(self) -> None:
        if not self._has_valid_selection():
            self.review_panel.hide()
            return
        self.review_panel.show()
        user, _pwd = self._creds()
        acc = self._selected_single
        if acc is None:
            self.review_command_label.setText(_EMPTY)
            self.review_command_label.setToolTip("")
            return
        argv = build_pspasswd_argv(
            resolve_pspasswd_exe(),
            PasswordChangeRequest(acc.host, acc.name, acc.sid or ""),
            REDACTED,
            user=user,
            admin_password=REDACTED,
            include_secrets=False,
        )
        preview = preview_pspasswd_argv(argv)
        self.review_command_label.setText(preview)
        self.review_command_label.setToolTip(preview)

    def _refresh_all(self) -> None:
        self._refresh_dest_labels()
        self._refresh_selection_labels()
        self._refresh_review()
        self._refresh_actions()

    # ── consulta ──────────────────────────────────────────────────────────

    def load_accounts(self) -> None:
        if self._busy:
            return
        user, password = self._creds()
        admin_errors = validate_admin_credential(user, password)
        if admin_errors:
            QMessageBox.warning(
                self,
                self.tr("Credencial incompleta"),
                admin_errors[0],
            )
            return
        host = self._get_host()
        if not host or not is_valid_host(host):
            QMessageBox.warning(
                self,
                self.tr("Destino inválido"),
                self.tr("Informe um host válido na aba PsExec."),
            )
            return
        self._query_host_snapshot = host
        self._set_busy(True)
        self._set_accounts_loading(True)
        self._query_worker = _QueryWorker(
            host,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
            current_host=host,
        )
        self._query_worker.finished_ok.connect(self._on_single_query_ok)
        self._query_worker.finished_err.connect(self._on_query_err)
        self._query_worker.start()

    def _on_single_query_ok(self, result: object) -> None:
        if not isinstance(result, LocalAccountsQueryResult):
            self._set_busy(False)
            self._set_accounts_loading(False)
            return
        if result.stale:
            QMessageBox.warning(
                self,
                self.tr("Host alterado"),
                result.error or self.tr("O host mudou durante a consulta."),
            )
            self._set_busy(False)
            self._set_accounts_loading(False)
            return
        self._accounts = list(result.accounts)
        self._populate_single_table()
        self._last_query_at = format_now_datetime()
        self._set_busy(False)
        self._set_accounts_loading(False)
        self._refresh_dest_labels()

    def _on_query_err(self, message: str) -> None:
        QMessageBox.warning(self, self.tr("Consulta falhou"), message or self.tr("Erro."))
        self._set_busy(False)
        self._set_accounts_loading(False)
        self._update_accounts_status()

    def _populate_single_table(self) -> None:
        table = self.accounts_table
        with pause_table_sorting(table):
            table.setRowCount(0)
            for account in self._accounts:
                row = table.rowCount()
                table.insertRow(row)
                values = [
                    account.name,
                    account.full_name or _EMPTY,
                    account.type_label,
                    account.sid or _EMPTY,
                    account.state_label,
                    self.tr("Sim") if account.locked else self.tr("Não"),
                    self._bool_label(account.password_required),
                    self._bool_label(account.password_expires),
                    self.tr("Sim")
                    if account.password_change_allowed and pspasswd_available()
                    else self.tr("Não"),
                ]
                for col, text in enumerate(values):
                    item = SortableTableItem(text)
                    if col == 0:
                        item.setData(_ACCOUNT_ROLE, account)
                        if account.builtin_kind == BuiltinAccountKind.ADMINISTRATOR:
                            item.setToolTip(
                                self.tr("Conta Administrador interna (RID 500)")
                            )
                    table.setItem(row, col, item)
        self._selected_single = None
        self._update_accounts_status()
        self._refresh_selection_labels()
        self._refresh_review()
        self._refresh_actions()

    @staticmethod
    def _bool_label(value: Optional[bool]) -> str:
        if value is None:
            return _EMPTY
        return "Sim" if value else "Não"

    def _apply_filter(self) -> None:
        needle = (self.filter_edit.text() or "").strip().casefold()
        table = self.accounts_table
        for row in range(table.rowCount()):
            if not needle:
                table.setRowHidden(row, False)
                continue
            parts: List[str] = []
            for col in range(table.columnCount()):
                item = table.item(row, col)
                if item is not None:
                    parts.append(item.text())
            hidden = needle not in " ".join(parts).casefold()
            table.setRowHidden(row, hidden)

    def _on_account_selection_changed(self) -> None:
        items = self.accounts_table.selectedItems()
        if not items:
            self._selected_single = None
        else:
            self._selected_single = self._find_account_in_row(items[0].row())
        self._refresh_selection_labels()
        self._refresh_review()
        self._refresh_actions()

    def _find_account_in_row(self, row: int) -> Optional[LocalAccount]:
        table = self.accounts_table
        for col in range(table.columnCount()):
            item = table.item(row, col)
            if item is None:
                continue
            account = item.data(_ACCOUNT_ROLE)
            if isinstance(account, LocalAccount):
                return account
        return None

    def _accounts_context_menu(self, pos) -> None:
        menu = QMenu(self)
        act_copy = menu.addAction(self.tr("Copiar informações"))
        chosen = menu.exec(self.accounts_table.viewport().mapToGlobal(pos))
        if chosen is act_copy:
            row = self.accounts_table.currentRow()
            if row < 0:
                return
            account = self._find_account_in_row(row)
            if account is not None:
                text = (
                    f"{account.host}\t{account.name}\t{account.sid}\t"
                    f"{account.type_label}\t{account.state_label}"
                )
                QApplication.clipboard().setText(text)

    # ── alteração de senha ────────────────────────────────────────────────

    def _reset_password_field(self, container: QWidget, line_edit: QLineEdit) -> None:
        line_edit.clear()
        line_edit.setEchoMode(QLineEdit.EchoMode.Password)
        eye = container.findChild(QToolButton)
        if eye is not None:
            eye.setStyleSheet(_eye_button_qss(active=False))
            eye.setToolTip("Mostrar senha")

    def _clear_password_fields(self) -> None:
        self._reset_password_field(self.new_pass_field, self.new_pass_edit)
        self._reset_password_field(self.confirm_pass_field, self.confirm_pass_edit)

    def _collect_requests(
        self,
    ) -> List[tuple[PasswordChangeRequest, Optional[LocalAccount]]]:
        if self._selected_single is None:
            return []
        acc = self._selected_single
        return [
            (
                PasswordChangeRequest(acc.host, acc.name, acc.sid or ""),
                acc,
            )
        ]

    def _on_change_clicked(self) -> None:
        new_password = self.new_pass_edit.text()
        confirm = self.confirm_pass_edit.text()
        errors = validate_new_password(new_password, confirm)
        user, admin_password = self._creds()
        errors.extend(validate_admin_credential(user, admin_password))
        requests = self._collect_requests()
        if not requests:
            errors.append(self.tr("Selecione uma conta local."))
        if errors:
            QMessageBox.warning(self, self.tr("Validação"), "\n".join(errors))
            return

        acc = requests[0][1]
        if acc and (acc.disabled or acc.locked):
            warn = QMessageBox(self)
            warn.setIcon(QMessageBox.Icon.Warning)
            warn.setWindowTitle(self.tr("Conta em estado especial"))
            warn.setText(
                self.tr(
                    f"A conta {acc.name} está {acc.state_label}. "
                    "Deseja continuar mesmo assim?"
                )
            )
            warn.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if warn.exec() != QMessageBox.StandardButton.Yes:
                return

        if not self._confirm_operation(requests, user):
            return

        self._set_busy(True)
        self._password_worker = _PasswordWorker(
            requests,
            new_password=new_password,
            admin_user=user,
            admin_password=admin_password,
            pstools_dir=get_pstools_dir(),
            listed_accounts=list(self._accounts),
            current_host=self._get_host(),
            operator=user or current_operator(),
        )
        self._password_worker.item_done.connect(self._on_password_result)
        self._password_worker.finished_all.connect(self._on_password_finished)
        self._password_worker.start()

    def _confirm_operation(
        self,
        requests: Sequence[tuple[PasswordChangeRequest, Optional[LocalAccount]]],
        operator: str,
    ) -> bool:
        acc = requests[0][1]
        if acc is None:
            return False
        summary = [
            self.tr(f"Host: {acc.host}"),
            self.tr(f"Conta: {acc.name}"),
            self.tr(f"SID: {acc.sid or '—'}"),
            self.tr(f"Tipo: {acc.type_label}"),
            self.tr(f"Estado: {acc.state_label}"),
            self.tr(f"Operador: {operator or 'Contexto Windows atual'}"),
            self.tr("Nova senha: ********"),
            self.tr("Não existe desfazer automático desta operação."),
        ]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Confirmar alteração de senha"))
        box.setText("\n".join(summary))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        return box.exec() == QMessageBox.StandardButton.Yes

    def _on_password_result(self, result: object) -> None:
        if not isinstance(result, PasswordChangeResult):
            return
        self._results.append(result)
        self._append_result_row(result)

    def _on_password_finished(self) -> None:
        self._clear_password_fields()
        self._set_busy(False)

    def _append_result_row(self, result: PasswordChangeResult) -> None:
        if self.results_card.is_collapsed:
            self.results_card.set_collapsed(False)
        ok_count = sum(1 for r in self._results if r.ok)
        fail_count = len(self._results) - ok_count
        self.results_status_lbl.setText(
            self.tr(
                f"{len(self._results)} operação(ões): "
                f"{ok_count} com sucesso, {fail_count} com falha."
            )
        )
        table = self.results_table
        row = table.rowCount()
        table.insertRow(row)
        values = [
            result.host,
            result.account_name,
            result.account_sid or _EMPTY,
            PHASE_LABELS.get(result.phase, result.phase.value),
            result.output_sanitized or _EMPTY,
            str(result.exit_code) if result.exit_code is not None else _EMPTY,
            f"{result.duration:.1f}s",
        ]
        for col, text in enumerate(values):
            item = SortableTableItem(text)
            item.setData(_ACCOUNT_ROLE, result)
            table.setItem(row, col, item)

    def _results_context_menu(self, pos) -> None:
        menu = QMenu(self)
        act_copy_row = menu.addAction(self.tr("Copiar linha"))
        act_copy_detail = menu.addAction(self.tr("Copiar detalhe"))
        menu.addSeparator()
        act_retry = menu.addAction(self.tr("Repetir somente falhas"))
        act_connectivity = menu.addAction(self.tr("Abrir Conectividade"))
        act_sessoes = menu.addAction(self.tr("Abrir Sessões e Arquivos"))
        act_clear = menu.addAction(self.tr("Limpar resultados"))
        chosen = menu.exec(self.results_table.viewport().mapToGlobal(pos))
        row = self.results_table.currentRow()
        result_item = (
            self.results_table.item(row, 0) if row >= 0 else None
        )
        result = (
            result_item.data(_ACCOUNT_ROLE)
            if result_item is not None
            else None
        )
        if chosen is act_copy_row and isinstance(result, PasswordChangeResult):
            cols = [
                self.results_table.item(row, c).text()
                for c in range(self.results_table.columnCount())
                if self.results_table.item(row, c)
            ]
            QApplication.clipboard().setText("\t".join(cols))
        elif chosen is act_copy_detail and isinstance(result, PasswordChangeResult):
            QApplication.clipboard().setText(result.output_sanitized or "")
        elif chosen is act_retry:
            self._retry_failures()
        elif chosen is act_connectivity and isinstance(result, PasswordChangeResult):
            self.openConnectivityRequested.emit(result.host)
        elif chosen is act_sessoes:
            self.openSessoesRequested.emit()
        elif chosen is act_clear:
            self._clear_results()

    def _retry_failures(self) -> None:
        failed = [
            r
            for r in self._results
            if r.phase != PasswordChangePhase.SUCCESS
        ]
        if not failed:
            return
        # Reexecuta somente falhas com a mesma senha exige novo preenchimento.
        QMessageBox.information(
            self,
            self.tr("Repetir falhas"),
            self.tr(
                "Preencha novamente a nova senha e confirme para repetir "
                f"{len(failed)} operação(ões) com falha."
            ),
        )

    def _clear_results(self) -> None:
        self._results = []
        self.results_table.setRowCount(0)
        self.results_status_lbl.setText(
            self.tr("Nenhuma alteração executada nesta sessão.")
        )

    def _export_csv(self) -> None:
        from PyQt6.QtWidgets import QFileDialog

        path, _flt = QFileDialog.getSaveFileName(
            self,
            self.tr("Exportar CSV sanitizado"),
            "contas_locais_resultados.csv",
            "CSV (*.csv)",
        )
        if not path:
            return
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        headers = [
            self.results_table.horizontalHeaderItem(c).text()
            for c in range(self.results_table.columnCount())
            if self.results_table.horizontalHeaderItem(c)
        ]
        writer.writerow(headers)
        for row in range(self.results_table.rowCount()):
            writer.writerow(
                [
                    self.results_table.item(row, c).text()
                    for c in range(self.results_table.columnCount())
                    if self.results_table.item(row, c)
                ]
            )
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())

    def _stop_pending(self) -> None:
        if self._password_worker is not None:
            self._password_worker.cancel()

    def _set_busy(self, busy: bool) -> None:
        self._busy = bool(busy)
        self._refresh_actions()

    def _invalidate_busy_host(self) -> None:
        self._stop_pending()
        QMessageBox.warning(
            self,
            self.tr("Host alterado"),
            self.tr(
                "O host da aba PsExec mudou durante a operação. "
                "Os resultados pendentes podem estar incompletos."
            ),
        )

    @staticmethod
    def is_local_user_for_host(
        display_user: str,
        domain: str,
        *,
        host: str,
    ) -> bool:
        return is_probably_local_session_user(
            display_user,
            domain,
            computer_name=host,
        )
