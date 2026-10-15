from __future__ import annotations

import hashlib
from typing import Optional

from PyQt6.QtCore import QEvent, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPalette, QValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.auth_identity import (
    AuthKind,
    compose_psexec_identity,
    normalize_account_name,
    normalize_domain_label,
)
from remoteops.core.psexec_options import (
    TOOLTIPS,
    PsExecOptions,
    compute_psexec_option_state,
    options_to_params,
)
from remoteops.ui.style import (
    CARD_GRID_VERTICAL_SPACING,
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_FOCUS,
    COLOR_HOVER,
    COLOR_PRESSED,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    FONT_UI,
    HEADER_BTN_SIZE,
    INPUT_HEIGHT,
    RADIUS_MEDIUM,
    RADIUS_SMALL,
    SIZE_UI,
    SIZE_UI_SMALL,
    accent_button_qss,
    composite_field_qss,
)
from remoteops.ui.widgets.card import CardWidget, bind_card_stack, make_card_stack
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.flow import FlowLayout
from remoteops.ui.widgets.spinbox import StepSpinBox
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.utils.cpu_topology import (
    CpuTopology,
    ProcessorGroup,
    affinity_limit_for,
    describe_cpu_status,
    query_remote_cpu_topology,
)
from remoteops.utils.domain import (
    HostUserDomain,
    domain_hint_from_hostname,
    local_logon_domain,
    lookup_host_userdomain,
)
from remoteops.utils.host_reachability import (
    HostReachability,
    is_stale_host_result,
    probe_host_reachability,
    probe_tcp_445,
    psexec_tcp_blocked_message,
)
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.printer_settings import get_print_server
from remoteops.utils.printers import print_server_unc
from remoteops.utils.psping import PsPingState
from remoteops.utils.sessions import (
    RemoteSession,
    classify_session_query,
    format_psexec_session,
    list_remote_sessions,
)
from remoteops.utils.validator import AffinityValidator


class _HostStatusWorker(QThread):
    """ICMP + TCP 445 em background; emite geração, host e o resultado."""

    result = pyqtSignal(int, str, object)

    def __init__(self, host: str, generation: int, *, use_cache: bool = True, parent=None):
        super().__init__(parent)
        self._host = host
        self._generation = generation
        self._use_cache = use_cache
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        from remoteops.utils.pstools import get_pstools_dir

        reach = probe_host_reachability(
            self._host,
            should_cancel=lambda: self._cancel or self.isInterruptionRequested(),
            pstools_dir=get_pstools_dir(),
            use_cache=self._use_cache,
        )
        self.result.emit(self._generation, self._host, reach)


class _TcpPrecheckWorker(QThread):
    """TCP Ping rápido na porta 445 antes do PsExec."""

    result = pyqtSignal(str, object)

    def __init__(self, host: str, parent=None):
        super().__init__(parent)
        self._host = host
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        ping = probe_tcp_445(
            self._host,
            should_cancel=lambda: self._cancel or self.isInterruptionRequested(),
            use_cache=True,
        )
        self.result.emit(self._host, ping)


class _AffinityEdit(QLineEdit):
    """Mantém o texto rejeitado visível quando a colagem não cabe no validador."""

    inputRejectedReason = pyqtSignal(str)

    def _propose(self, current: str, addition: str, pos: int) -> str:
        pos = max(0, min(pos, len(current)))
        return current[:pos] + addition + current[pos:]

    def keyPressEvent(self, event) -> None:
        before = self.text()
        pos = self.cursorPosition()
        addition = event.text() or ""
        super().keyPressEvent(event)
        if not addition or self.text() != before:
            return
        validator = self.validator()
        if validator is None:
            return
        validator.validate(self._propose(before, addition, pos), pos)
        reason = str(getattr(validator, "last_reason", "") or "")
        if reason:
            self.inputRejectedReason.emit(reason)

    def insertFromMimeData(self, source) -> None:
        before = self.text()
        pos = self.cursorPosition()
        pasted = source.text() if source is not None else ""
        super().insertFromMimeData(source)
        if not pasted or self.text() != before:
            return
        validator = self.validator()
        if validator is None:
            return
        attempted = self._propose(before, pasted, pos)
        state, _, _ = validator.validate(attempted, pos)
        if state == QValidator.State.Invalid and len(pasted) > 1:
            self.setText(attempted)
            self.setCursorPosition(len(attempted))
        reason = str(getattr(validator, "last_reason", "") or "")
        if reason:
            self.inputRejectedReason.emit(reason)


class _CpuTopologyWorker(QThread):
    """Lê a topologia do host fora da thread da interface.

    ``generation`` descarta resultado atrasado. A senha só vive até o fim de ``run``.
    """

    result = pyqtSignal(int, str, object, str)

    def __init__(
        self,
        generation: int,
        host: str,
        user: str = "",
        password: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._generation = int(generation)
        self._host = host
        self._user = user
        self._password = password
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        try:
            if self._cancel or self.isInterruptionRequested():
                return
            try:
                topology, error = query_remote_cpu_topology(
                    self._host,
                    user=self._user,
                    password=self._password,
                    should_cancel=lambda: self._cancel or self.isInterruptionRequested(),
                )
            except Exception:
                topology, error = None, "Falha ao consultar os processadores."
            if self._cancel or self.isInterruptionRequested():
                return
            self.result.emit(self._generation, self._host, topology, error or "")
        finally:
            self._password = ""
            self._user = ""


class _SessionListWorker(QThread):
    """Lista sessões WTS do host em background.

    ``generation`` identifica o contexto que pediu a consulta. A senha fica
    só até o fim de ``run`` e não entra no sinal.
    """

    result = pyqtSignal(int, str, object, str)

    def __init__(
        self,
        generation: int,
        host: str,
        user: str = "",
        password: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._generation = int(generation)
        self._host = host
        self._user = user
        self._password = password
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True
        self.requestInterruption()

    def run(self) -> None:
        try:
            if self._cancel or self.isInterruptionRequested():
                return
            try:
                sessions, error = list_remote_sessions(
                    self._host, user=self._user, password=self._password
                )
            except Exception:
                sessions, error = [], "Falha inesperada ao consultar as sessões."
            if self._cancel or self.isInterruptionRequested():
                return
            self.result.emit(self._generation, self._host, sessions, error or "")
        finally:
            self._password = ""
            self._user = ""


class _HostDomainWorker(QThread):
    """Descobre o USERDOMAIN do host em background (NetAPI + nbtstat)."""

    result = pyqtSignal(str, object)

    def __init__(self, host: str, parent=None):
        super().__init__(parent)
        self._host = host

    def run(self) -> None:
        self.result.emit(self._host, lookup_host_userdomain(self._host))


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_label(text: str, *, min_width: int = 120) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    if min_width > 0:
        lbl.setMinimumWidth(min_width)
    # opacidade reduzida via stylesheet
    lbl.setStyleSheet(f"QLabel#fieldLabel {{ color: {COLOR_TEXT_SECONDARY}; }}")
    return lbl


def _field_caption(object_name: str) -> QLabel:
    lbl = QLabel()
    lbl.setObjectName(object_name)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        f"QLabel#{object_name} {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
    )
    lbl.setContentsMargins(2, 0, 0, 0)
    return lbl


def _captioned_field(object_name: str, caption: str, body: QWidget) -> QWidget:
    column = QVBoxLayout()
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(2)
    label = _field_caption(object_name)
    label.setText(caption)
    column.addWidget(label)
    column.addWidget(body)
    wrap = QWidget()
    wrap.setLayout(column)
    wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return wrap


def _add_row(grid: QGridLayout, row: int, label_text: str, widget: QWidget):
    """Adiciona label + widget em uma linha do grid (label centralizado na vertical)."""
    lbl = _make_label(label_text)
    grid.addWidget(lbl, row, 0, Qt.AlignmentFlag.AlignVCenter)
    grid.addWidget(widget, row, 1, Qt.AlignmentFlag.AlignVCenter)


def _grid_in_card(card: CardWidget) -> QGridLayout:
    grid = QGridLayout()
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(10)
    grid.setVerticalSpacing(CARD_GRID_VERTICAL_SPACING)
    grid.setColumnStretch(1, 1)
    card.content_layout.addLayout(grid)
    return grid


def _line_edit_with_clear_icon(password: bool = False, clear_tooltip: str = "Limpar"):
    """
    Container com QLineEdit e ícone de remover à direita.
    Retorna (container, line_edit). O ícone só aparece quando há texto.
    Altura igual aos demais QLineEdit (INPUT_HEIGHT).
    """
    container = QWidget()
    container.setObjectName("AuthField")
    container.setFixedHeight(INPUT_HEIGHT)
    container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    container.setStyleSheet(composite_field_qss("AuthField"))
    layout = QHBoxLayout(container)
    layout.setContentsMargins(8, 0, 4, 0)
    layout.setSpacing(2)

    line_edit = QLineEdit()
    line_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    clear_btn = QToolButton()
    clear_btn.setText("\uE711")
    clear_btn.setFont(QFont("Segoe MDL2 Assets", 10))
    clear_btn.setFixedSize(22, 22)
    clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    clear_btn.setToolTip(clear_tooltip)
    clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    clear_btn.setStyleSheet(f"""
        QToolButton {{ border: none; background: transparent; color: {COLOR_ACCENT}; }}
        QToolButton:hover {{ background: transparent; border-radius: {RADIUS_SMALL}px; }}
        QToolButton:pressed {{ background: transparent; }}
    """)
    clear_btn.hide()

    def on_text_changed(text):
        clear_btn.setVisible(bool(text.strip()))

    def on_clear():
        line_edit.clear()
        line_edit.setFocus()

    line_edit.textChanged.connect(on_text_changed)
    clear_btn.clicked.connect(on_clear)

    layout.addWidget(line_edit)
    layout.addWidget(clear_btn, 0, Qt.AlignmentFlag.AlignVCenter)
    return container, line_edit


def _auth_eye_qss(*, active: bool) -> str:
    color = COLOR_ACCENT if active else COLOR_TEXT_SECONDARY
    return f"""
        QToolButton {{
            border: 1px solid transparent;
            background: transparent;
            color: {color};
            border-radius: {RADIUS_SMALL}px;
        }}
        QToolButton:hover {{
            color: {COLOR_ACCENT};
        }}
        QToolButton:focus {{
            border-color: {COLOR_FOCUS};
        }}
        QToolButton:disabled {{
            color: {COLOR_TEXT_MUTED};
        }}
    """


_AUTH_KIND_QSS = f"""
QWidget#authKindBar QPushButton {{
    border: 1px solid transparent;
    border-radius: {RADIUS_MEDIUM}px;
    background: transparent;
    color: {COLOR_TEXT_SECONDARY};
    padding: 2px 8px;
}}
QWidget#authKindBar QPushButton:hover {{
    background: {COLOR_HOVER};
    color: {COLOR_TEXT};
}}
QWidget#authKindBar QPushButton:checked {{
    background: {COLOR_PRESSED};
    color: {COLOR_TEXT};
    font-weight: 600;
}}
QWidget#authKindBar QPushButton:checked:hover {{
    background: {COLOR_PRESSED};
    color: {COLOR_TEXT};
}}
QWidget#authKindBar QPushButton:focus {{
    border-color: {COLOR_FOCUS};
}}
QWidget#authKindBar QPushButton:disabled {{
    color: {COLOR_TEXT_MUTED};
    background: transparent;
}}
"""

_AUTH_KIND_TIPS = {
    AuthKind.WINDOWS: "Usa o token desta sessão. O PsExec não recebe -u nem -p.",
    AuthKind.DOMAIN: r"Conta de domínio no formato DOMÍNIO\usuário. O prefixo pode ser corrigido.",
    AuthKind.LOCAL: r"Conta local do computador remoto, enviada como .\usuário.",
}


class _FitStack(QStackedWidget):
    """Altura da página visível, para o card não reservar as duas variantes."""

    def sizeHint(self):
        current = self.currentWidget()
        if current is None:
            return super().sizeHint()
        return current.sizeHint()

    def minimumSizeHint(self):
        current = self.currentWidget()
        if current is None:
            return super().minimumSizeHint()
        return current.minimumSizeHint()

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        current = self.currentWidget()
        return bool(current is not None and current.hasHeightForWidth())

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        current = self.currentWidget()
        if current is None:
            return super().heightForWidth(width)
        if current.hasHeightForWidth():
            return current.heightForWidth(width)
        return current.sizeHint().height()


class _AuthKindButton(QPushButton):
    def keyPressEvent(self, event):  # noqa: N802
        key = event.key()
        move = getattr(self.parent(), "move_kind", None)
        if not callable(move):
            move = getattr(self.parent(), "move_level", None)
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down) and callable(move):
            move(1)
            event.accept()
            return
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Up) and callable(move):
            move(-1)
            event.accept()
            return
        super().keyPressEvent(event)


class _AuthKindBar(QWidget):
    """Três opções em pílulas, no mesmo peso visual dos demais controles."""

    kindChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("authKindBar")
        self.setAccessibleName("Tipo de credencial")
        self.setFixedHeight(INPUT_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(_AUTH_KIND_QSS)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._buttons: dict[AuthKind, _AuthKindButton] = {}
        labels = (
            (AuthKind.WINDOWS, "Windows atual"),
            (AuthKind.DOMAIN, "Domínio"),
            (AuthKind.LOCAL, "Conta local"),
        )
        for kind, label in labels:
            button = _AuthKindButton(label, self)
            button.setObjectName(f"authKind_{kind.value}")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            button.setAccessibleName(label)
            button.setToolTip(button.tr(_AUTH_KIND_TIPS[kind]))
            button.toggled.connect(lambda checked, selected=kind: self._emit_kind(selected, checked))
            self._buttons[kind] = button
            layout.addWidget(button, 1)
        self._buttons[AuthKind.WINDOWS].setChecked(True)

    def button(self, kind: AuthKind) -> _AuthKindButton:
        return self._buttons[AuthKind(kind)]

    def kind(self) -> AuthKind:
        for kind, button in self._buttons.items():
            if button.isChecked():
                return kind
        return AuthKind.WINDOWS

    def set_kind(self, kind: AuthKind) -> None:
        button = self._buttons[AuthKind(kind)]
        if not button.isChecked():
            button.setChecked(True)

    def move_kind(self, step: int) -> None:
        order = (AuthKind.WINDOWS, AuthKind.DOMAIN, AuthKind.LOCAL)
        index = order.index(self.kind())
        nxt = order[max(0, min(len(order) - 1, index + step))]
        button = self._buttons[nxt]
        button.setChecked(True)
        button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _emit_kind(self, kind: AuthKind, checked: bool) -> None:
        if checked:
            self.kindChanged.emit(kind.value)


_PRIVILEGE_QSS = _AUTH_KIND_QSS.replace("authKindBar", "privilegeBar")

_PRIVILEGE_LEVELS = ("", "-h", "-s", "-l")

_PRIVILEGE_LABELS = {
    "": "Padrão",
    "-h": "Elevado (-h)",
    "-s": "SYSTEM (-s)",
    "-l": "Limitado (-l)",
}

_PRIVILEGE_CAPTIONS = {
    "": "Executar sem flags adicionais de privilégio.",
    "-h": "Solicita o token elevado da conta, quando disponível.",
    "-s": "Executar como NT AUTHORITY\\SYSTEM no computador remoto.",
    "-l": "Executar com privilégios reduzidos.",
}

_PRIVILEGE_TIPS = {
    "": _PRIVILEGE_CAPTIONS[""],
    "-h": TOOLTIPS["-h"],
    "-s": TOOLTIPS["-s"],
    "-l": TOOLTIPS["-l"],
}

_SESSION_MISSING_REASON = (
    "A sessão selecionada não está mais disponível. "
    "Escolha novamente antes de executar de forma interativa."
)


class _PrivilegeBar(QWidget):
    """Padrão, elevado, SYSTEM ou limitado — uma opção por vez."""

    levelChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("privilegeBar")
        self.setAccessibleName("Nível de privilégio")
        self.setFixedHeight(INPUT_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(_PRIVILEGE_QSS)
        self._syncing = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._buttons: dict[str, _AuthKindButton] = {}
        for level in _PRIVILEGE_LEVELS:
            label = _PRIVILEGE_LABELS[level]
            button = _AuthKindButton(label, self)
            button.setObjectName("privilegeDefault" if not level else f"privilege_{level[1:]}")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setAutoDefault(False)
            button.setDefault(False)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            button.setAccessibleName(label)
            button.setToolTip(button.tr(_PRIVILEGE_TIPS[level]))
            button.toggled.connect(
                lambda checked, selected=level: self._emit_level(selected, checked)
            )
            self._buttons[level] = button
            layout.addWidget(button, 1)
        self._buttons[""].setChecked(True)

    def button(self, level: str) -> _AuthKindButton:
        return self._buttons.get(level, self._buttons[""])

    def level(self) -> str:
        for level, button in self._buttons.items():
            if button.isChecked():
                return level
        return ""

    def set_level(self, level: str) -> None:
        button = self._buttons.get(level, self._buttons[""])
        if button.isChecked():
            return
        self._syncing = True
        try:
            button.setChecked(True)
        finally:
            self._syncing = False

    def move_level(self, step: int) -> None:
        index = _PRIVILEGE_LEVELS.index(self.level())
        nxt = _PRIVILEGE_LEVELS[max(0, min(len(_PRIVILEGE_LEVELS) - 1, index + step))]
        button = self._buttons[nxt]
        button.setChecked(True)
        button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _emit_level(self, level: str, checked: bool) -> None:
        if checked and not self._syncing:
            self.levelChanged.emit(level)


# ── tab principal ─────────────────────────────────────────────────────────────

class PsExecTab(QWidget):
    openHostAppsRequested = pyqtSignal()
    openWinGetRequested = pyqtSignal()
    openPsInfoRequested = pyqtSignal()
    openProcessosRequested = pyqtSignal()
    openServicosRequested = pyqtSignal()
    openEnergiaRequested = pyqtSignal()
    openContasLocaisRequested = pyqtSignal()
    openSessoesArquivosRequested = pyqtSignal()
    openRustDeskRequested = pyqtSignal()
    openMessageRequested = pyqtSignal()
    openPrintersRequested = pyqtSignal()
    openConnectivityRequested = pyqtSignal()
    formLayoutChanged = pyqtSignal()
    hostOnlineChanged = pyqtSignal(bool)
    tcpPrecheckFinished = pyqtSignal(bool, str)
    authChanged = pyqtSignal()

    def __init__(self, parent=None, log_output=None):
        super().__init__(parent)
        self.log_output = log_output
        self._host_online = False
        self._closing = False
        self._last_reachability: Optional[HostReachability] = None
        self._host_status_worker: Optional[_HostStatusWorker] = None
        self._host_status_wanted = ""
        self._host_status_generation = 0
        self._host_status_pending: Optional[tuple] = None
        self._live_workers: list = []
        self._tcp_precheck_worker: Optional[_TcpPrecheckWorker] = None
        self._tcp_precheck_generation = 0
        self._domain_worker: Optional[_HostDomainWorker] = None
        self._domain_wanted = ""
        self._host_userdomain = HostUserDomain()
        self._auth_kind = AuthKind.WINDOWS
        self._domain_manual = False
        self._updating_user = False
        self._updating_auth = False
        self._updating_domain = False
        self._copy_allowed = True
        self._updating_option_state = False
        self._shell_s_preselected = False
        self._session_worker: Optional[_SessionListWorker] = None
        self._session_generation = 0
        self._session_context: Optional[tuple] = None
        self._session_loaded_generation = -1
        self._session_pending = False
        self._session_querying = False
        self._session_pick: Optional[tuple] = None
        self._session_unconfirmed = False
        self._session_block_reason = ""
        self._filling_sessions = False
        self._updating_privilege = False
        self._session_refresh_timer = QTimer(self)
        self._session_refresh_timer.setSingleShot(True)
        self._session_refresh_timer.setInterval(150)
        self._session_refresh_timer.timeout.connect(self._refresh_remote_sessions)
        self._cpu_worker: Optional[_CpuTopologyWorker] = None
        self._cpu_generation = 0
        self._cpu_context: Optional[tuple] = None
        self._cpu_loaded_generation = -1
        self._cpu_pending = False
        self._cpu_querying = False
        self._cpu_state = "not_queried"
        self._cpu_topology: Optional[CpuTopology] = None
        self._cpu_failure = ""
        self._cpu_group_block_reason = ""
        self._rebuilding_cpu_groups = False
        self._noting_cpu = False
        self._cpu_refresh_timer = QTimer(self)
        self._cpu_refresh_timer.setSingleShot(True)
        self._cpu_refresh_timer.setInterval(150)
        self._cpu_refresh_timer.timeout.connect(self._refresh_cpu_topology)
        self._host_status_timer = QTimer(self)
        self._host_status_timer.setSingleShot(True)
        self._host_status_timer.setInterval(550)
        self._host_status_timer.timeout.connect(self._check_host_status)

        # Formulário: cards no topo. Sobra vertical da janela fica
        # para Pré-visualização e Log (aba com altura = conteúdo).
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        vbox = make_card_stack(self)

        # ── Card 1 — Conexão ─────────────────────────────────────────────────
        card1 = CardWidget("\uEA18", self.tr("Conexão"))
        g1 = _grid_in_card(card1)

        host_clear_container, self.host_edit = _line_edit_with_clear_icon(
            clear_tooltip=self.tr("Limpar host")
        )
        self.host_edit.setPlaceholderText("ex: 192.168.1.100 ou computador.local")
        self.host_edit.setToolTip(
            self.tr("Nome, FQDN ou endereço IP do computador remoto")
        )
        _add_row(g1, 0, self.tr("Host remoto"), host_clear_container)

        # Ver → software → interagir → impacto (reset/recolher ficam no fim).
        self.psinfo_button = card1.make_header_button(
            "\uE946", self.tr("Abrir Inventário")
        )
        self.psinfo_button.clicked.connect(self.openPsInfoRequested.emit)
        card1.add_header_button(self.psinfo_button)

        self.sessoes_arquivos_button = card1.make_header_button(
            "\uE8F1", self.tr("Sessões e arquivos remotos")
        )
        self.sessoes_arquivos_button.clicked.connect(
            self.openSessoesArquivosRequested.emit
        )
        card1.add_header_button(self.sessoes_arquivos_button)

        self.contas_locais_button = card1.make_header_button(
            "\uE77B", self.tr("Contas locais e alteração de senha (PsPasswd)")
        )
        self.contas_locais_button.clicked.connect(
            self.openContasLocaisRequested.emit
        )
        card1.add_header_button(self.contas_locais_button)

        self.processos_button = card1.make_header_button(
            "\uE9D9", self.tr("Processos remotos (PsList)")
        )
        self.processos_button.clicked.connect(self.openProcessosRequested.emit)
        card1.add_header_button(self.processos_button)

        self.servicos_button = card1.make_header_button(
            "\uE90F", self.tr("Serviços remotos (PsService)")
        )
        self.servicos_button.clicked.connect(self.openServicosRequested.emit)
        card1.add_header_button(self.servicos_button)

        self.hostapps_button = card1.make_header_button(
            "\uE71D", self.tr("Listar aplicativos do host (Remote Registry)")
        )
        self.hostapps_button.clicked.connect(self.openHostAppsRequested.emit)
        card1.add_header_button(self.hostapps_button)

        self.winget_button = card1.make_header_button(
            "\uE7B8", self.tr("WinGet — pacotes remotos (winget)")
        )
        self.winget_button.clicked.connect(self.openWinGetRequested.emit)
        card1.add_header_button(self.winget_button)

        self.printers_button = card1.make_header_button(
            "\uE749", self.tr("Impressoras de rede")
        )
        self.printers_button.clicked.connect(self.openPrintersRequested.emit)
        card1.add_header_button(self.printers_button)
        self.refresh_printers_button_tooltip()

        self.message_button = card1.make_header_button(
            "\uE8BD", self.tr("Enviar mensagem ao host")
        )
        self.message_button.clicked.connect(self.openMessageRequested.emit)
        card1.add_header_button(self.message_button)

        self.rustdesk_button = card1.make_header_button(
            "\uE774", self.tr("Conectar via RustDesk")
        )
        self.rustdesk_button.clicked.connect(self.openRustDeskRequested.emit)
        card1.add_header_button(self.rustdesk_button)

        self.energia_button = card1.make_header_button(
            "\uE7E8", self.tr("Gerenciamento de energia remoto (PsShutdown)")
        )
        self.energia_button.clicked.connect(self.openEnergiaRequested.emit)
        card1.add_header_button(self.energia_button)

        # Status (legenda com bolinha abaixo do host)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.host_status_dot = _StatusDot()
        self.host_status_label = QLabel()
        self.host_status_label.setObjectName("hostStatusCaption")
        self.host_status_label.setWordWrap(True)
        self.host_status_label.setMinimumWidth(0)
        self.host_status_label.setStyleSheet(
            f"QLabel#hostStatusCaption {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        status_row.addWidget(self.host_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.host_status_label, 1, Qt.AlignmentFlag.AlignVCenter)
        self.recheck_btn = QToolButton()
        self.recheck_btn.setObjectName("recheckHostBtn")
        self.recheck_btn.setText("\uE72C")
        self.recheck_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self.recheck_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self.recheck_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.recheck_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.recheck_btn.setAutoRaise(True)
        self.recheck_btn.setToolTip(self.tr("Verificar novamente"))
        self.recheck_btn.setAccessibleName(self.tr("Verificar novamente"))
        self.recheck_btn.setStyleSheet(
            f"QToolButton#recheckHostBtn {{ border: none; background: transparent; color: {COLOR_ACCENT}; }}"
        )
        self.recheck_btn.clicked.connect(self._recheck_host_status)
        status_row.addWidget(self.recheck_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        self.diagnose_btn = QPushButton(self.tr("Diagnosticar"))
        self.diagnose_btn.setObjectName("diagnoseHostBtn")
        self.diagnose_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.diagnose_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.diagnose_btn.setFixedHeight(HEADER_BTN_SIZE)
        self.diagnose_btn.setToolTip(
            self.tr("Abrir a aba Conectividade (ICMP Ping e TCP Ping)")
        )
        self.diagnose_btn.setStyleSheet(
            accent_button_qss(
                "QPushButton#diagnoseHostBtn",
                radius=RADIUS_SMALL,
                padding="0 10px",
            )
        )
        self.diagnose_btn.clicked.connect(self.openConnectivityRequested.emit)
        status_row.addWidget(self.diagnose_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        status_container = QWidget()
        status_container.setLayout(status_row)
        status_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        status_container.setAccessibleName(self.tr("Status da conectividade"))
        _add_row(g1, 1, self.tr("Status"), status_container)
        self._set_host_status("idle")  # desabilita ações até o host ficar Online
        self.host_edit.textChanged.connect(self._on_host_text_changed)

        # Comando remoto
        remote_cmd_container, self.remote_cmd_edit = _line_edit_with_clear_icon(
            clear_tooltip=self.tr("Limpar comando")
        )
        self.remote_cmd_edit.setPlaceholderText(
            self.tr(r"ex: cmd.exe ou C:\Windows\System32\hostname.exe")
        )
        self.remote_cmd_edit.setToolTip(
            self.tr(
                "Programa executado no computador remoto, com argumentos se necessário. "
                "O texto digitado não é substituído pela verificação de rede."
            )
        )
        self.remote_cmd_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g1, 2, self.tr("Comando remoto"), remote_cmd_container)

        vbox.addWidget(card1)

        # ── Card 2 — Autenticação ─────────────────────────────────────────────
        card2 = self._build_auth_card()
        vbox.addWidget(card2)
        self._set_auth_tab_order()

        # ── Card 3 — Privilégios e Sessão ─────────────────────────────────────
        card3 = CardWidget("\uE8D4", self.tr("Privilégios e Sessão"))
        g3 = _grid_in_card(card3)

        # Os checkboxes continuam sendo a fonte do estado (-h/-s/-l) para o
        # backend, o reset e os comandos que marcam -s. A barra é a seleção.
        self.flag_h = QCheckBox("-h  " + self.tr("Elevado"))
        self.flag_s = QCheckBox("-s  SYSTEM")
        self.flag_l = QCheckBox("-l  " + self.tr("Limitado"))
        for cb in (self.flag_h, self.flag_s, self.flag_l):
            cb.setParent(self)
            cb.hide()
            cb.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.privilege_bar = _PrivilegeBar()
        self.privilege_bar.levelChanged.connect(self._on_privilege_level)
        self.privilege_caption = _field_caption("privilegeCaption")
        self.privilege_caption.setText(self.tr(_PRIVILEGE_CAPTIONS[""]))
        _add_row(g3, 0, self.tr("Privilégio"), self.privilege_bar)
        g3.addWidget(self.privilege_caption, 1, 1)

        session_row = QHBoxLayout()
        session_row.setSpacing(8)
        session_row.setContentsMargins(0, 0, 0, 0)
        self.session_interactive = QCheckBox(self.tr("Interativo (-i)"))
        self.session_interactive.setToolTip(self.tr(TOOLTIPS["-i"]))
        self.session_combo = FluentComboBox()
        self.session_combo.setAccessibleName(self.tr("Sessão remota"))
        self.session_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.session_combo.setEnabled(False)
        self.session_combo.setToolTip(self.tr(TOOLTIPS["session_id"]))
        self.session_combo.currentIndexChanged.connect(self._on_session_combo_changed)
        self._reset_session_combo()
        self.session_refresh_btn = QToolButton()
        self.session_refresh_btn.setObjectName("sessionRefreshBtn")
        self.session_refresh_btn.setText("\uE72C")
        self.session_refresh_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self.session_refresh_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self.session_refresh_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.session_refresh_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.session_refresh_btn.setAutoRaise(True)
        self.session_refresh_btn.setToolTip(self.tr("Atualizar"))
        self.session_refresh_btn.setAccessibleName(self.tr("Atualizar"))
        self.session_refresh_btn.setStyleSheet(
            "QToolButton#sessionRefreshBtn { border: none; background: transparent; "
            f"color: {COLOR_ACCENT}; }}"
        )
        self.session_refresh_btn.clicked.connect(self._on_session_refresh_clicked)
        session_row.addWidget(self.session_interactive)
        session_row.addWidget(self.session_combo, 1)
        session_row.addWidget(self.session_refresh_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        session_container = QWidget()
        session_container.setLayout(session_row)
        session_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.session_status = _field_caption("sessionStatus")
        self.session_status.hide()
        _add_row(g3, 2, self.tr("Sessão"), session_container)
        g3.addWidget(self.session_status, 3, 1)

        vbox.addWidget(card3)

        # ── Card 4 — Desempenho ───────────────────────────────────────────────
        card4 = CardWidget("\uE950", self.tr("Desempenho"))
        g4 = _grid_in_card(card4)

        self.priority_combo = FluentComboBox()
        self.priority_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.priority_combo.addItem(self.tr("Padrão"), "")
        self.priority_combo.addItem(self.tr("Baixa"), "-low")
        self.priority_combo.addItem(self.tr("Abaixo do normal"), "-belownormal")
        self.priority_combo.addItem(self.tr("Acima do normal"), "-abovenormal")
        self.priority_combo.addItem(self.tr("Alta"), "-high")
        self.priority_combo.addItem(self.tr("Tempo real — crítico"), "-realtime")
        self.priority_combo.addItem(self.tr("Segundo plano — E/S e memória"), "-background")
        self.priority_combo.setCurrentIndex(0)
        self.priority_combo.setToolTip(self.tr(TOOLTIPS["priority"]))
        self.realtime_warning = _field_caption("cpuRealtimeWarning")
        self.realtime_warning.setText(
            self.tr(
                "Tempo real pode tornar o computador remoto instável. "
                "A execução pedirá confirmação."
            )
        )
        self.realtime_warning.hide()
        priority_column = QVBoxLayout()
        priority_column.setContentsMargins(0, 0, 0, 0)
        priority_column.setSpacing(2)
        priority_caption = _field_caption("cpuPriorityCaption")
        priority_caption.setText(self.tr("Prioridade do processo"))
        priority_column.addWidget(priority_caption)
        priority_column.addWidget(self.priority_combo)
        priority_column.addWidget(self.realtime_warning)
        priority_box = QWidget()
        priority_box.setLayout(priority_column)

        self.group_combo = FluentComboBox()
        self.group_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.group_combo.addItem(self.tr("Automático (sem -g)"), None)
        self.group_combo.setCurrentIndex(0)
        self.group_combo.setToolTip(self.tr(TOOLTIPS["-g"]))
        group_box = _captioned_field("cpuGroupCaption", self.tr("Grupo CPU"), self.group_combo)

        performance_top = QWidget()
        performance_row = QHBoxLayout(performance_top)
        performance_row.setContentsMargins(0, 0, 0, 0)
        performance_row.setSpacing(8)
        performance_row.addWidget(priority_box, 1)
        performance_row.addWidget(group_box, 1)
        g4.addWidget(performance_top, 0, 0, 1, 2)

        self.affinity_edit = _AffinityEdit()
        self.affinity_edit.setPlaceholderText(self.tr("1,2,3"))
        self.affinity_edit.setToolTip(self.tr(TOOLTIPS["-a"]))
        self.affinity_validator = AffinityValidator(None, self.affinity_edit)
        self.affinity_edit.setValidator(self.affinity_validator)
        self.affinity_edit.inputRejectedReason.connect(self._on_affinity_rejected)
        self.cpu_query_btn = QToolButton()
        self.cpu_query_btn.setObjectName("cpuQueryButton")
        self.cpu_query_btn.setText("\uE72C")
        self.cpu_query_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self.cpu_query_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self.cpu_query_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cpu_query_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.cpu_query_btn.setAutoRaise(True)
        self.cpu_query_btn.setToolTip(self.tr("Atualizar"))
        self.cpu_query_btn.setAccessibleName(self.tr("Atualizar"))
        self.cpu_query_btn.setStyleSheet(
            "QToolButton#cpuQueryButton { border: none; background: transparent; "
            f"color: {COLOR_ACCENT}; }}"
        )
        self.cpu_query_btn.clicked.connect(self._on_cpu_query_clicked)
        affinity_row = QWidget()
        affinity_layout = QHBoxLayout(affinity_row)
        affinity_layout.setContentsMargins(0, 0, 0, 0)
        affinity_layout.setSpacing(8)
        affinity_layout.addWidget(self.affinity_edit, 1)
        affinity_layout.addWidget(self.cpu_query_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        affinity_box = _captioned_field(
            "cpuAffinityCaption", self.tr("Afinidade CPU"), affinity_row
        )
        g4.addWidget(affinity_box, 1, 0, 1, 2)

        self.cpu_status = _field_caption("cpuTopologyStatus")
        self.cpu_status.setText(self.tr("Não consultado."))
        g4.addWidget(self.cpu_status, 2, 0, 1, 2)

        vbox.addWidget(card4)

        # ── Card 5 — Flags e Argumentos ──────────────────────────────────────
        card5 = CardWidget("\uE115", self.tr("Flags e Argumentos"))
        g5 = _grid_in_card(card5)

        # Timeout
        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(6)
        timeout_row.setContentsMargins(0, 0, 0, 0)
        self.timeout_spin = StepSpinBox()
        self.timeout_spin.setRange(0, 9999)
        self.timeout_spin.setValue(0)
        self.timeout_spin.setToolTip(self.tr("Timeout em segundos (0 = sem timeout)"))
        timeout_suffix = QLabel(self.tr("segundos"))
        timeout_suffix.setStyleSheet("color: palette(windowText);")
        timeout_row.addWidget(self.timeout_spin)
        timeout_row.addWidget(timeout_suffix)
        timeout_container = QWidget()
        timeout_container.setLayout(timeout_row)
        timeout_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g5, 0, self.tr("Timeout"), timeout_container)

        # Flags (podem quebrar linha; espaçamento entre Timeout/Flags/Args = 4 px no grid)
        flags_flow = FlowLayout(margin=0, h_spacing=10, v_spacing=4)
        self.flag_d = QCheckBox("-d")
        self.flag_d.setToolTip(self.tr(TOOLTIPS["-d"]))
        self.flag_e = QCheckBox("-e")
        self.flag_e.setToolTip(self.tr(TOOLTIPS["-e"]))
        self.flag_c = QCheckBox("-c")
        self.flag_c.setToolTip(self.tr(TOOLTIPS["-c"]))
        self.flag_f = QCheckBox("-f")
        self.flag_f.setToolTip(self.tr(TOOLTIPS["-f"]))
        self.flag_v = QCheckBox("-v")
        self.flag_v.setToolTip(self.tr(TOOLTIPS["-v"]))
        self.flag_arm = QCheckBox("-arm")
        self.flag_arm.setToolTip(self.tr(TOOLTIPS["-arm"]))
        self.flag_arm.setChecked(False)
        self.flag_accepteula = QCheckBox("-accepteula")
        self.flag_accepteula.setToolTip(self.tr(TOOLTIPS["-accepteula"]))
        self.flag_nobanner = QCheckBox("-nobanner")
        self.flag_nobanner.setToolTip(self.tr(TOOLTIPS["-nobanner"]))
        for cb in [
            self.flag_d, self.flag_e, self.flag_c, self.flag_f,
            self.flag_v, self.flag_arm, self.flag_accepteula, self.flag_nobanner,
        ]:
            flags_flow.addWidget(cb)
        flags_container = QWidget()
        flags_container.setLayout(flags_flow)
        flags_sp = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        flags_sp.setHeightForWidth(True)
        flags_container.setSizePolicy(flags_sp)
        _add_row(g5, 1, self.tr("Flags"), flags_container)

        # Args extras
        extra_args_container, self.extra_args = _line_edit_with_clear_icon()
        self.extra_args.setPlaceholderText(
            self.tr("Argumentos do programa remoto, ex: /S /quiet")
        )
        self.extra_args.setToolTip(self.tr(TOOLTIPS["extra_args"]))
        _add_row(g5, 2, self.tr("Args do programa"), extra_args_container)

        vbox.addWidget(card5)

        self._priority_tooltips = [
            self.tr(TOOLTIPS["priority_default"]),
            self.tr(TOOLTIPS["-low"]),
            self.tr(TOOLTIPS["-belownormal"]),
            self.tr(TOOLTIPS["-abovenormal"]),
            self.tr(TOOLTIPS["-high"]),
            self.tr(TOOLTIPS["-realtime"]),
            self.tr(TOOLTIPS["-background"]),
        ]
        self._connect_option_signals()
        self.update_psexec_option_state()

        # Collapsible só depois do conteúdo no layout — senão o padrão
        # minimizado (Autenticação / Desempenho) nasce com altura errada.
        self._form_cards = (card1, card2, card3, card4, card5)
        _collapsed_default = (False, True, False, True, False)
        _reset_handlers = (
            self._reset_card_conexao,
            self._reset_card_autenticacao,
            self._reset_card_privilegios,
            self._reset_card_desempenho,
            self._reset_card_flags,
        )
        for card, start_collapsed, on_reset in zip(
            self._form_cards, _collapsed_default, _reset_handlers
        ):
            card.set_collapsible(True, collapsed=start_collapsed)
            card.set_resettable(True, self.tr("Restaurar padrões deste card"))
            card.resetRequested.connect(on_reset)
            card.collapsedChanged.connect(self._on_form_card_collapsed)
        bind_card_stack(vbox, self._form_cards)
        self._cpu_context = self._cpu_context_key()
        self._update_cpu_query_enabled()
        self._refresh_cpu_status()

    def _reset_card_conexao(self) -> None:
        self.host_edit.clear()
        if not self.remote_cmd_edit.isReadOnly():
            self.remote_cmd_edit.clear()
        self._shell_s_preselected = False
        self.update_psexec_option_state()

    def _reset_card_autenticacao(self) -> None:
        self._domain_manual = False
        self._set_account_text("")
        self._reset_password_field()
        self._select_kind(AuthKind.WINDOWS, clear_account=False)
        self._apply_discovered_domain()
        self.update_psexec_option_state("user")
        self._notify_auth_layout()
        self.authChanged.emit()

    def _reset_card_privilegios(self) -> None:
        for cb in (self.flag_h, self.flag_s, self.flag_l, self.session_interactive):
            cb.setChecked(False)
        self._session_generation += 1
        self._session_context = self._session_context_key()
        self._session_pending = False
        self._reset_session_combo()
        self.update_psexec_option_state()

    def _reset_card_desempenho(self) -> None:
        self.priority_combo.setCurrentIndex(0)
        self.affinity_edit.clear()
        groups = None
        if self._cpu_state == "available" and self._cpu_topology is not None:
            groups = self._cpu_topology.groups
        self._cpu_group_block_reason = ""
        self._rebuild_group_combo(groups, preserve=None)
        self.update_psexec_option_state()

    def _reset_card_flags(self) -> None:
        self.timeout_spin.setValue(0)
        for cb in (
            self.flag_d,
            self.flag_e,
            self.flag_c,
            self.flag_f,
            self.flag_v,
            self.flag_arm,
            self.flag_accepteula,
            self.flag_nobanner,
        ):
            cb.setChecked(False)
        self.extra_args.clear()
        self.update_psexec_option_state()

    def reset_all(self) -> None:
        """Restaura todos os cards da aba PsExec aos valores iniciais."""
        self._reset_card_conexao()
        self._reset_card_autenticacao()
        self._reset_card_privilegios()
        self._reset_card_desempenho()
        self._reset_card_flags()
        self.restore_card_collapse_defaults()

    def restore_card_collapse_defaults(self) -> None:
        """Conexão/Privilégios/Flags abertos; Autenticação/Desempenho recolhidos."""
        defaults = (False, True, False, True, False)
        for card, collapsed in zip(self._form_cards, defaults):
            card.set_collapsed(collapsed)

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if obj is getattr(self, "_domain_slash", None):
                self.domain_edit.setFocus()
                return True
            if obj is getattr(self, "_local_prefix", None):
                self.user_edit.setFocus()
                return True
        return super().eventFilter(obj, event)

    # ── autenticação ──────────────────────────────────────────────────────────

    def _build_auth_card(self) -> CardWidget:
        card = CardWidget("\uE8D7", self.tr("Autenticação"))
        self.auth_card = card
        grid = _grid_in_card(card)
        grid.setColumnStretch(3, 1)

        self.auth_kind_bar = _AuthKindBar(self)
        self.auth_kind_bar.kindChanged.connect(self._on_auth_kind_changed)
        self.kind_windows = self.auth_kind_bar.button(AuthKind.WINDOWS)
        self.kind_domain = self.auth_kind_bar.button(AuthKind.DOMAIN)
        self.kind_local = self.auth_kind_bar.button(AuthKind.LOCAL)
        kind_lbl = _make_label(self.tr("Tipo de credencial"), min_width=0)
        kind_lbl.setFixedWidth(self._auth_label_width())
        grid.addWidget(kind_lbl, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self.auth_kind_bar, 0, 1, 1, 3, Qt.AlignmentFlag.AlignVCenter)

        self.auth_stack = _FitStack()
        self.auth_stack.setObjectName("authStack")
        self.auth_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        self.auth_notice = self._build_auth_notice()
        self.auth_fields = self._build_auth_fields()
        self.auth_stack.addWidget(self.auth_notice)
        self.auth_stack.addWidget(self.auth_fields)
        grid.addWidget(self.auth_stack, 1, 0, 1, 4)

        self._auth_kind = AuthKind.WINDOWS
        self._sync_auth_visibility()
        return card

    def _set_auth_tab_order(self) -> None:
        self.setTabOrder(self.kind_windows, self.kind_domain)
        self.setTabOrder(self.kind_domain, self.kind_local)
        self.setTabOrder(self.kind_local, self.domain_edit)
        self.setTabOrder(self.domain_edit, self.user_edit)
        self.setTabOrder(self.user_edit, self.pass_edit)
        self.setTabOrder(self.pass_edit, self.pass_reveal)

    def _auth_label_width(self) -> int:
        return max(120, self.fontMetrics().horizontalAdvance(self.tr("Tipo de credencial")) + 8)

    def _build_auth_notice(self) -> QWidget:
        notice = QWidget()
        notice.setObjectName("authNotice")
        layout = QHBoxLayout(notice)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(8)
        icon = QLabel("\uE73E")
        icon.setFont(QFont("Segoe MDL2 Assets", 14))
        icon.setStyleSheet("color: #107C10; background: transparent;")
        icon.setAccessibleName("")
        text_col = QWidget()
        text_lay = QVBoxLayout(text_col)
        text_lay.setContentsMargins(0, 0, 0, 0)
        text_lay.setSpacing(0)
        title = QLabel(self.tr("Credenciais da sessão Windows"))
        title.setObjectName("authNoticeTitle")
        title_font = QFont(FONT_UI, SIZE_UI)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {COLOR_TEXT}; background: transparent;")
        body = QLabel(
            self.tr(
                "O RemoteOps utilizará o contexto de segurança atual, "
                "sem informar usuário e senha adicionais ao PsExec."
            )
        )
        body.setObjectName("authNoticeBody")
        body.setWordWrap(True)
        body.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {SIZE_UI_SMALL}pt; background: transparent;"
        )
        text_lay.addWidget(title)
        text_lay.addWidget(body)
        layout.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(text_col, 1)
        return notice

    def _build_auth_fields(self) -> QWidget:
        fields = QWidget()
        fields.setObjectName("authFields")
        grid = QGridLayout(fields)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(CARD_GRID_VERTICAL_SPACING)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        user_container, self.user_edit = _line_edit_with_clear_icon(
            clear_tooltip=self.tr("Limpar usuário")
        )
        self.user_edit.setPlaceholderText(self.tr("usuário"))
        self.user_edit.setAccessibleName(self.tr("Usuário"))
        self.domain_edit = QLineEdit()
        self.domain_edit.setObjectName("authDomainEdit")
        self.domain_edit.setPlaceholderText(self.tr("DOMÍNIO"))
        self.domain_edit.setAccessibleName(self.tr("Domínio da conta"))
        self.domain_edit.setFont(QFont(FONT_UI, SIZE_UI))
        domain_palette = self.domain_edit.palette()
        domain_palette.setColor(QPalette.ColorRole.Text, QColor(COLOR_ACCENT))
        domain_palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(COLOR_TEXT_MUTED))
        self.domain_edit.setPalette(domain_palette)
        self.domain_edit.setStyleSheet(
            f"""
            QLineEdit#authDomainEdit {{
                border: none;
                background: transparent;
                padding: 0px;
                min-height: 0px;
                max-height: {INPUT_HEIGHT}px;
                selection-background-color: {COLOR_ACCENT_SOFT};
                selection-color: {COLOR_TEXT};
            }}
            """
        )
        self.domain_edit.setToolTip(
            self.tr(
                "Preenchido automaticamente com o domínio desta sessão. "
                "Se o computador remoto estiver em outro domínio, o prefixo é atualizado "
                "quando a consulta termina. Uma edição manual não é substituída."
            )
        )
        self.domain_edit.textChanged.connect(self._on_domain_edited)
        self._domain_slash = QLabel("\\")
        self._domain_slash.setObjectName("authDomainSlash")
        self._domain_slash.setFont(QFont(FONT_UI, SIZE_UI))
        self._domain_slash.setStyleSheet(
            f"color: {COLOR_ACCENT}; background: transparent;"
        )
        self._domain_slash.setCursor(Qt.CursorShape.IBeamCursor)
        self._domain_slash.installEventFilter(self)
        self._local_prefix = QLabel(".\\")
        self._local_prefix.setObjectName("authLocalPrefix")
        self._local_prefix.setFont(QFont(FONT_UI, SIZE_UI))
        self._local_prefix.setStyleSheet(
            f"color: {COLOR_ACCENT}; background: transparent;"
        )
        self._local_prefix.setToolTip(
            self.tr(
                "PsExec recebe .\\usuário: conta local do computador de destino, "
                "também quando o host é um IP ou um FQDN."
            )
        )
        self._local_prefix.setCursor(Qt.CursorShape.IBeamCursor)
        self._local_prefix.installEventFilter(self)
        self._local_prefix.hide()
        user_lay = user_container.layout()
        user_lay.setSpacing(0)
        user_lay.insertWidget(0, self.domain_edit, 0, Qt.AlignmentFlag.AlignVCenter)
        user_lay.insertWidget(1, self._domain_slash, 0, Qt.AlignmentFlag.AlignVCenter)
        user_lay.insertWidget(2, self._local_prefix, 0, Qt.AlignmentFlag.AlignVCenter)
        self._fit_domain_edit()
        self._user_caption = _field_caption("authUserCaption")

        self._pass_container, self.pass_edit = self._build_password_field()
        self._pass_caption = _field_caption("authPassCaption")

        user_lbl = _make_label(self.tr("Usuário"), min_width=0)
        user_lbl.setFixedWidth(self._auth_label_width())
        pass_lbl = _make_label(self.tr("Senha"), min_width=0)
        grid.addWidget(user_lbl, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(user_container, 0, 1, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(pass_lbl, 0, 2, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self._pass_container, 0, 3, Qt.AlignmentFlag.AlignVCenter)
        grid.addWidget(self._user_caption, 1, 1, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(self._pass_caption, 1, 3, Qt.AlignmentFlag.AlignTop)
        self._update_user_caption()
        self._update_pass_caption()
        return fields

    def _build_password_field(self) -> tuple[QWidget, QLineEdit]:
        container = QWidget()
        container.setObjectName("AuthField")
        container.setFixedHeight(INPUT_HEIGHT)
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        container.setStyleSheet(composite_field_qss("AuthField"))
        layout = QHBoxLayout(container)
        layout.setContentsMargins(8, 0, 4, 0)
        layout.setSpacing(2)
        line_edit = QLineEdit()
        line_edit.setEchoMode(QLineEdit.EchoMode.Password)
        line_edit.setPlaceholderText(self.tr("Senha"))
        line_edit.setAccessibleName(self.tr("Senha"))
        line_edit.setToolTip(self.tr(TOOLTIPS["-p"]))
        line_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.pass_reveal = QToolButton()
        self.pass_reveal.setObjectName("authPassReveal")
        self.pass_reveal.setText("\uE890")
        self.pass_reveal.setFont(QFont("Segoe MDL2 Assets", 10))
        self.pass_reveal.setFixedSize(22, 22)
        self.pass_reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pass_reveal.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.pass_reveal.setAutoRaise(True)
        self.pass_reveal.setToolTip(self.tr("Mostrar senha"))
        self.pass_reveal.setAccessibleName(self.tr("Mostrar senha"))
        self.pass_reveal.setStyleSheet(_auth_eye_qss(active=False))
        self.pass_reveal.clicked.connect(self._toggle_password_visible)
        layout.addWidget(line_edit)
        layout.addWidget(self.pass_reveal, 0, Qt.AlignmentFlag.AlignVCenter)
        return container, line_edit

    @property
    def auth_kind(self) -> AuthKind:
        return self._auth_kind

    def auth_username(self) -> str:
        """Valor de ``-u``. Vazio no modo Windows atual e sem nome de conta."""
        if not hasattr(self, "user_edit"):
            return ""
        return compose_psexec_identity(
            self._auth_kind,
            self.user_edit.text(),
            self.domain_edit.text() if hasattr(self, "domain_edit") else "",
        )

    def auth_password(self) -> str:
        """Senha bruta só para a execução. Vazia sem conta ou no modo Windows atual."""
        if self._auth_kind is AuthKind.WINDOWS or not self.auth_username():
            return ""
        return self.pass_edit.text() or ""

    def _on_auth_kind_changed(self, kind_value: str) -> None:
        if self._updating_auth:
            return
        try:
            kind = AuthKind(kind_value)
        except ValueError:
            return
        if kind == self._auth_kind:
            return
        self._select_kind(kind, clear_account=True)

    def _select_kind(self, kind: AuthKind, *, clear_account: bool) -> None:
        kind = AuthKind(kind)
        changed = kind != self._auth_kind
        self._updating_auth = True
        try:
            self._auth_kind = kind
            self.auth_kind_bar.set_kind(kind)
            if changed and clear_account:
                self._set_account_text("")
            if changed:
                self._reset_password_field()
            self._sync_auth_visibility()
            self._update_user_caption()
        finally:
            self._updating_auth = False
        if changed:
            if kind is AuthKind.DOMAIN:
                self._ensure_auto_domain()
            self.update_psexec_option_state("user")
            self._notify_auth_layout()
            self.authChanged.emit()

    def _sync_auth_visibility(self) -> None:
        windows = self._auth_kind is AuthKind.WINDOWS
        local = self._auth_kind is AuthKind.LOCAL
        self.auth_stack.setCurrentWidget(self.auth_notice if windows else self.auth_fields)
        self.domain_edit.setVisible(not local)
        self._domain_slash.setVisible(not local)
        self._local_prefix.setVisible(local)
        if self._auth_kind is AuthKind.DOMAIN:
            self.user_edit.setToolTip(
                self.tr("Nome da conta, sem o domínio. O prefixo ao lado forma DOMÍNIO\\usuário.")
            )
        elif local:
            self.user_edit.setToolTip(
                self.tr("Nome da conta local do computador remoto. O PsExec recebe .\\usuário.")
            )
        else:
            self.user_edit.setToolTip(self.tr(_AUTH_KIND_TIPS[AuthKind.WINDOWS]))
        self._fit_domain_edit()
        self.auth_stack.updateGeometry()

    def _notify_auth_layout(self) -> None:
        self.auth_stack.updateGeometry()
        self.updateGeometry()
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
        self.formLayoutChanged.emit()

    def _fit_domain_edit(self) -> None:
        if not hasattr(self, "domain_edit"):
            return
        sample = self.domain_edit.text() or self.domain_edit.placeholderText() or "DOMÍNIO"
        width = self.domain_edit.fontMetrics().horizontalAdvance(sample) + 14
        self.domain_edit.setFixedWidth(max(72, min(width, 200)))

    def _set_account_text(self, text: str) -> None:
        self._updating_user = True
        try:
            self.user_edit.setText(text)
        finally:
            self._updating_user = False

    def _set_domain_text(self, name: str) -> None:
        cleaned = normalize_domain_label(name)
        if self.domain_edit.text() == cleaned:
            self._update_user_caption()
            return
        self._updating_domain = True
        try:
            self.domain_edit.setText(cleaned)
            self._fit_domain_edit()
        finally:
            self._updating_domain = False
        self._update_user_caption()
        self.update_psexec_option_state("user")
        self.authChanged.emit()

    def _reset_password_field(self) -> None:
        self.pass_edit.clear()
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_reveal.setToolTip(self.tr("Mostrar senha"))
        self.pass_reveal.setAccessibleName(self.tr("Mostrar senha"))
        self.pass_reveal.setStyleSheet(_auth_eye_qss(active=False))

    def _toggle_password_visible(self) -> None:
        show = self.pass_edit.echoMode() == QLineEdit.EchoMode.Password
        self.pass_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if show else QLineEdit.EchoMode.Password
        )
        tip = self.tr("Ocultar senha") if show else self.tr("Mostrar senha")
        self.pass_reveal.setToolTip(tip)
        self.pass_reveal.setAccessibleName(tip)
        self.pass_reveal.setStyleSheet(_auth_eye_qss(active=show))

    def _update_user_caption(self) -> None:
        if not hasattr(self, "_user_caption"):
            return
        if self._auth_kind is AuthKind.LOCAL:
            self._user_caption.setText(self.tr("Conta local do computador remoto"))
            return
        if self._auth_kind is not AuthKind.DOMAIN:
            self._user_caption.setText("")
            return
        domain = normalize_domain_label(self.domain_edit.text())
        info = self._host_userdomain
        discovered = ""
        if info.name and not info.is_workgroup:
            discovered = normalize_domain_label(info.name)
        auto = bool(domain) and not self._domain_manual and domain.casefold() == discovered.casefold()
        if auto:
            self._user_caption.setText(
                self.tr("Prefixo do domínio detectado automaticamente")
            )
        elif domain and self._domain_manual:
            self._user_caption.setText(self.tr("Domínio informado manualmente"))
        elif domain:
            self._user_caption.setText(
                self.tr("Prefixo do domínio detectado automaticamente")
            )
        else:
            self._user_caption.setText(
                self.tr("Informe o domínio da conta, se necessário")
            )

    def _update_pass_caption(self) -> None:
        if not hasattr(self, "_pass_caption"):
            return
        self._pass_caption.setText(
            self.tr("Opcional; depende do método de autenticação")
        )

    def _on_domain_edited(self, text: str) -> None:
        if self._updating_domain:
            return
        cleaned = normalize_domain_label(text)
        if cleaned != text:
            self._updating_domain = True
            try:
                self.domain_edit.setText(cleaned)
            finally:
                self._updating_domain = False
        self._domain_manual = True
        self._fit_domain_edit()
        self._update_user_caption()
        self.update_psexec_option_state("user")
        self.authChanged.emit()

    def _on_user_text_changed(self, text: str = "") -> None:
        if self._updating_user:
            return
        raw = text or ""
        if raw.startswith(".\\") or raw.startswith("./"):
            account = normalize_account_name(raw)
            self._set_account_text(account)
            if self._auth_kind is not AuthKind.LOCAL:
                self._select_kind(AuthKind.LOCAL, clear_account=False)
            else:
                self._update_user_caption()
                self.update_psexec_option_state("user")
                self.authChanged.emit()
            return
        if "\\" in raw or "/" in raw:
            normalized = raw.replace("/", "\\")
            domain, _, account = normalized.partition("\\")
            self._updating_domain = True
            try:
                self._set_account_text(normalize_account_name(account))
                self.domain_edit.setText(normalize_domain_label(domain))
                self._domain_manual = True
                self._fit_domain_edit()
            finally:
                self._updating_domain = False
            self._update_user_caption()
            if self._auth_kind is not AuthKind.DOMAIN:
                self._select_kind(AuthKind.DOMAIN, clear_account=False)
            else:
                self.update_psexec_option_state("user")
                self.authChanged.emit()
            return
        self._update_user_caption()
        self.update_psexec_option_state("user")

    def _session_domain_name(self) -> str:
        info = local_logon_domain()
        if not info.name or info.is_workgroup:
            return ""
        return normalize_domain_label(info.name)

    def _ensure_auto_domain(self) -> None:
        """No modo Domínio, mostra o domínio já conhecido e atualiza a consulta do host."""
        if self._auth_kind is not AuthKind.DOMAIN or self._domain_manual:
            return
        self._apply_discovered_domain()
        host = normalize_host(self.host_edit.text())
        if host and is_valid_host(host) and self.is_host_online:
            self._start_domain_worker(host)

    def _apply_discovered_domain(self) -> None:
        """Preenche o prefixo só quando o usuário ainda não o editou.

        O domínio do host remoto tem prioridade. Sem essa resposta, usa o
        domínio da sessão. Workgroup não entra como domínio da conta.
        """
        if not hasattr(self, "domain_edit") or self._domain_manual:
            return
        info = self._host_userdomain
        if info.name and not info.is_workgroup:
            self._set_domain_text(info.name)
            return
        self._set_domain_text(self._session_domain_name())

    def _connect_option_signals(self) -> None:
        self.user_edit.textChanged.connect(self._on_user_text_changed)
        self.pass_edit.textChanged.connect(self._schedule_session_refresh)
        self.flag_h.stateChanged.connect(lambda: self.update_psexec_option_state("-h"))
        self.flag_s.stateChanged.connect(lambda: self.update_psexec_option_state("-s"))
        self.flag_e.stateChanged.connect(lambda: self.update_psexec_option_state("-e"))
        self.flag_l.stateChanged.connect(lambda: self.update_psexec_option_state("-l"))
        self.session_interactive.stateChanged.connect(
            lambda: self.update_psexec_option_state("-i")
        )
        self.flag_c.stateChanged.connect(lambda: self.update_psexec_option_state("-c"))
        self.flag_f.stateChanged.connect(lambda: self.update_psexec_option_state("-f"))
        self.flag_v.stateChanged.connect(lambda: self.update_psexec_option_state("-v"))
        self.flag_d.stateChanged.connect(lambda: self.update_psexec_option_state("-d"))
        self.flag_arm.stateChanged.connect(lambda: self.update_psexec_option_state("-arm"))
        self.group_combo.currentIndexChanged.connect(
            lambda: self.update_psexec_option_state("-g")
        )
        self.affinity_edit.textChanged.connect(
            lambda: self.update_psexec_option_state("-a")
        )
        self.priority_combo.currentIndexChanged.connect(
            lambda: self.update_psexec_option_state("priority")
        )

    def snapshot_options(self) -> PsExecOptions:
        group = self._selected_cpu_group_id()
        return PsExecOptions(
            user=self.auth_username(),
            has_password=bool(self.auth_password()),
            flag_h=self.flag_h.isChecked(),
            flag_s=self.flag_s.isChecked(),
            flag_e=self.flag_e.isChecked(),
            flag_l=self.flag_l.isChecked(),
            session_interactive=self.session_interactive.isChecked(),
            session_id=self._current_session_id(),
            session_unavailable=self._session_choice_unavailable(),
            session_unconfirmed=bool(
                self.session_interactive.isChecked() and self._session_unconfirmed
            ),
            session_block_reason=(
                self._session_block_reason if self.session_interactive.isChecked() else ""
            ),
            priority=self.priority_combo.currentData() or "",
            cpu_group=group,
            affinity=(self.affinity_edit.text() or "").strip(),
            cpu_topology_verified=self._cpu_topology_is_verified(),
            cpu_topology_groups=self._cpu_topology_pairs(),
            cpu_group_unavailable=self._cpu_group_is_unavailable(),
            cpu_group_block_reason=self._cpu_group_block_text(),
            timeout=int(self.timeout_spin.value() or 0),
            flag_d=self.flag_d.isChecked(),
            flag_c=self.flag_c.isChecked(),
            flag_f=self.flag_f.isChecked(),
            flag_v=self.flag_v.isChecked(),
            flag_accepteula=self.flag_accepteula.isChecked(),
            flag_nobanner=self.flag_nobanner.isChecked(),
            flag_arm=self.flag_arm.isChecked(),
            extra_args=self.extra_args.text() or "",
            copy_allowed=bool(self._copy_allowed),
        )

    def collect_builder_params(
        self,
        *,
        host: str,
        psexec_path: str,
        remote_cmd: str,
    ) -> dict:
        """Dict consumido pelo CommandBuilder (senha só como presença)."""
        opts = self.snapshot_options()
        params = options_to_params(opts)
        params.update(
            {
                "host": host,
                "psexec_path": psexec_path,
                "remote_cmd": remote_cmd,
                "has_password": bool(self.auth_password()),
            }
        )
        return params

    def set_copy_allowed(self, allowed: bool) -> None:
        if self._copy_allowed == bool(allowed):
            return
        self._copy_allowed = bool(allowed)
        self.update_psexec_option_state()

    def preselect_exe_options(self) -> None:
        """Marca na interface as opções iniciais ao escolher um .exe.

        ``-s`` continua no estado do card; a barra de privilégio acompanha.
        O usuário pode mudar a seleção depois.
        """
        for cb in (
            self.flag_accepteula,
            self.flag_nobanner,
            self.flag_s,
            self.flag_c,
            self.flag_f,
        ):
            cb.blockSignals(True)
            cb.setChecked(True)
            cb.blockSignals(False)
        self.update_psexec_option_state()

    def apply_shell_command_defaults(self, remote_cmd: str) -> None:
        """Ao digitar cmd/powershell, marca ``-s`` uma vez (o usuário pode desmarcar)."""
        if self.remote_cmd_edit.isReadOnly():
            self._shell_s_preselected = False
            return
        text = (remote_cmd or "").strip().lower()
        if text == "comando gerado automaticamente":
            self._shell_s_preselected = False
            return
        is_shell = text in ("cmd", "cmd.exe", "powershell", "powershell.exe")
        if not is_shell:
            self._shell_s_preselected = False
            return
        if self._shell_s_preselected:
            return
        self._shell_s_preselected = True
        if self.flag_s.isChecked():
            return
        self.flag_s.blockSignals(True)
        self.flag_s.setChecked(True)
        self.flag_s.blockSignals(False)
        self.update_psexec_option_state("-s")

    def update_psexec_option_state(self, trigger: Optional[str] = None) -> None:
        """Única rotina que recalcula habilitados, conflitos e tooltips."""
        if self._updating_option_state:
            return
        self._updating_option_state = True
        try:
            if trigger == "user":
                self._note_cpu_context()
            state = compute_psexec_option_state(self.snapshot_options(), trigger=trigger)
            self._apply_option_state(state, trigger=trigger)
        finally:
            self._updating_option_state = False

    def _apply_option_state(self, state, trigger: Optional[str] = None) -> None:
        opts = state.options
        widgets = state.widgets

        def apply_cb(cb: QCheckBox, key: str) -> None:
            ws = widgets.get(key)
            if ws is None:
                return
            cb.blockSignals(True)
            if ws.checked is not None:
                cb.setChecked(bool(ws.checked))
            cb.setEnabled(bool(ws.enabled))
            cb.setToolTip(self.tr(ws.tooltip) if ws.tooltip else "")
            cb.blockSignals(False)

        apply_cb(self.flag_h, "-h")
        apply_cb(self.flag_s, "-s")
        apply_cb(self.flag_e, "-e")
        apply_cb(self.flag_l, "-l")
        apply_cb(self.flag_c, "-c")
        apply_cb(self.flag_f, "-f")
        apply_cb(self.flag_v, "-v")
        apply_cb(self.flag_d, "-d")
        apply_cb(self.flag_arm, "-arm")
        apply_cb(self.flag_accepteula, "-accepteula")
        apply_cb(self.flag_nobanner, "-nobanner")
        apply_cb(self.session_interactive, "-i")
        self._sync_privilege_bar()

        pass_state = widgets.get("-p")
        enabled = bool(self.auth_username())
        if pass_state is not None:
            self._pass_container.setEnabled(enabled)
            self.pass_edit.setEnabled(enabled)
            self.pass_reveal.setEnabled(enabled)
            if enabled:
                self.pass_edit.setToolTip(self.tr(TOOLTIPS["-p"]))
            else:
                self.pass_edit.setToolTip(self.tr(pass_state.tooltip))
            self._update_pass_caption()
        if not enabled and (
            (self.pass_edit.text() or "")
            or self.pass_edit.echoMode() != QLineEdit.EchoMode.Password
        ):
            self._reset_password_field()

        session_state = widgets.get("session_id")
        if session_state is not None:
            self.session_combo.setEnabled(bool(session_state.enabled))
            if session_state.tooltip:
                self.session_combo.setToolTip(self.tr(session_state.tooltip))
        if not opts.session_interactive:
            self._reset_session_combo()
        elif trigger in ("-i", "user") and self.is_host_online:
            self._schedule_session_refresh()
        self._update_session_refresh_enabled()

        self._update_affinity_validator(opts.cpu_group)
        affinity_state = widgets.get("-a")
        if affinity_state is not None:
            self.affinity_edit.setEnabled(True)
            self.affinity_edit.setToolTip(self.tr(affinity_state.tooltip))
        group_state = widgets.get("-g")
        if group_state is not None:
            self.group_combo.setEnabled(True)
            self.group_combo.setToolTip(self.tr(group_state.tooltip))

        idx = self.priority_combo.currentIndex()
        if 0 <= idx < len(self._priority_tooltips):
            self.priority_combo.setToolTip(self._priority_tooltips[idx])
        else:
            self.priority_combo.setToolTip(self.tr(TOOLTIPS["priority"]))
        self._sync_realtime_warning()
        self._refresh_cpu_status()

        extra_state = widgets.get("extra_args")
        if extra_state is not None:
            self.extra_args.setToolTip(self.tr(extra_state.tooltip))

    def _password_mark(self) -> str:
        """Marca a senha sem guardá-la no identificador da consulta."""
        password = self.auth_password()
        if not password:
            return ""
        return hashlib.sha256(password.encode("utf-8")).hexdigest()[:16]

    def _session_context_key(self) -> tuple:
        return (
            normalize_host(self.host_edit.text()).casefold(),
            self.auth_kind.value,
            self.auth_username(),
            self._password_mark(),
            bool(self.session_interactive.isChecked()),
        )

    def _note_session_context(self) -> bool:
        """Invalida a consulta anterior quando host, conta ou senha mudam."""
        key = self._session_context_key()
        if key == self._session_context:
            return False
        self._session_context = key
        self._session_generation += 1
        worker = self._session_worker
        if worker is not None and worker.isRunning():
            self._session_pending = True
        if self.session_interactive.isChecked() and self._current_session_id() is not None:
            self._session_unconfirmed = True
            if self._session_query_applicable():
                self._session_block_reason = self.tr("Consultando sessões…")
            else:
                self._session_block_reason = self.tr(
                    "A lista de sessões está desatualizada. "
                    "Atualize ou escolha o console padrão antes de executar."
                )
            self._set_session_status(self._session_block_reason)
        return True

    def _current_session_data(self):
        return self.session_combo.currentData()

    def _current_session_id(self) -> Optional[int]:
        data = self._current_session_data()
        if isinstance(data, tuple) and data and data[0] == "session":
            try:
                return int(data[1])
            except (TypeError, ValueError):
                return None
        return None

    def _session_choice_unavailable(self) -> bool:
        data = self._current_session_data()
        return bool(isinstance(data, tuple) and data and data[0] == "missing")

    def _sync_privilege_bar(self) -> None:
        if self.flag_s.isChecked():
            level = "-s"
        elif self.flag_h.isChecked():
            level = "-h"
        elif self.flag_l.isChecked():
            level = "-l"
        else:
            level = ""
        self.privilege_bar.set_level(level)
        caption = _PRIVILEGE_CAPTIONS.get(level, _PRIVILEGE_CAPTIONS[""])
        self.privilege_caption.setText(self.tr(caption))
        self.privilege_bar.setAccessibleDescription(self.tr(caption))

    def _on_privilege_level(self, level: str) -> None:
        if self._updating_privilege or self._updating_option_state:
            return
        target = {"-h": self.flag_h, "-s": self.flag_s, "-l": self.flag_l}.get(level)
        order = [target] if target is not None else []
        order.extend(cb for cb in (self.flag_h, self.flag_s, self.flag_l) if cb is not target)
        self._updating_privilege = True
        try:
            for cb in order:
                want = cb is target
                if cb.isChecked() != want:
                    cb.setChecked(want)
        finally:
            self._updating_privilege = False

    def _set_session_status(self, text: str) -> None:
        if not hasattr(self, "session_status"):
            return
        self.session_status.setText(text)
        self.session_status.setVisible(bool(text))

    def _update_session_refresh_enabled(self) -> None:
        if not hasattr(self, "session_refresh_btn"):
            return
        host = normalize_host(self.host_edit.text())
        enabled = bool(
            self.session_interactive.isChecked()
            and host
            and is_valid_host(host)
            and self.is_host_online
            and not self._session_querying
            and not self._closing
        )
        self.session_refresh_btn.setEnabled(enabled)
        if self._session_querying:
            self.session_refresh_btn.setToolTip(self.tr("Consultando sessões…"))
        else:
            self.session_refresh_btn.setToolTip(self.tr("Atualizar"))

    def _emit_session_preview(self) -> None:
        self._filling_sessions = True
        try:
            self.session_combo.currentIndexChanged.emit(self.session_combo.currentIndex())
        finally:
            self._filling_sessions = False

    def _on_session_combo_changed(self) -> None:
        if self._filling_sessions:
            return
        self._remember_session_pick()

    def _remember_session_pick(self) -> None:
        data = self._current_session_data()
        host = normalize_host(self.host_edit.text()).casefold()
        if isinstance(data, tuple) and data and data[0] == "session":
            self._session_pick = (host, int(data[1]), (data[2] or "").casefold())
            self._session_unconfirmed = False
            self._session_block_reason = ""
            self._set_session_status("")
        elif data is None:
            self._session_pick = None
            self._session_unconfirmed = False
            self._session_block_reason = ""
            self._set_session_status("")

    def _reset_session_combo(self, *, keep_enabled: bool = False) -> None:
        self._session_pick = None
        self._session_unconfirmed = False
        self._session_block_reason = ""
        self._filling_sessions = True
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("Console padrão (sem ID)"), None)
        self.session_combo.setCurrentIndex(0)
        self.session_combo.blockSignals(False)
        self._filling_sessions = False
        if not keep_enabled:
            self.session_combo.setEnabled(self.session_interactive.isChecked())
        self.session_combo.setToolTip(self.tr(TOOLTIPS["session_id"]))
        if hasattr(self, "session_status"):
            self._set_session_status("")
        self._update_session_refresh_enabled()

    def _schedule_session_refresh(self) -> None:
        self._note_session_context()
        self._note_cpu_context()
        self._update_session_refresh_enabled()
        if not self.session_interactive.isChecked() or not self.is_host_online:
            return
        self._session_refresh_timer.start()

    def _session_query_applicable(self) -> bool:
        host = normalize_host(self.host_edit.text())
        return bool(
            self.session_interactive.isChecked()
            and self.is_host_online
            and host
            and is_valid_host(host)
            and not self._closing
        )

    def _on_session_refresh_clicked(self) -> None:
        self._session_refresh_timer.stop()
        self._refresh_remote_sessions(manual=True)

    def _refresh_remote_sessions(self, *, manual: bool = False) -> None:
        if self._closing:
            return
        self._note_session_context()
        if not self._session_query_applicable():
            self._update_session_refresh_enabled()
            return
        worker = self._session_worker
        running = worker is not None and worker.isRunning()
        if running:
            if manual or worker._generation != self._session_generation:
                self._session_pending = True
            return
        if (
            not manual
            and not self._session_pending
            and self._session_loaded_generation == self._session_generation
        ):
            return
        self._session_pending = False
        self._session_querying = True
        self._set_session_status(self.tr("Consultando sessões…"))
        self._update_session_refresh_enabled()
        generation = self._session_generation
        host = normalize_host(self.host_edit.text())
        self._session_worker = _SessionListWorker(
            generation,
            host,
            user=self.auth_username(),
            password=self.auth_password(),
            parent=self,
        )
        self._remember_worker(self._session_worker)
        self._session_worker.result.connect(self._on_sessions_result)
        self._session_worker.finished.connect(self._on_session_worker_finished)
        self._session_worker.start()

    def _session_result_is_current(self, generation: int, host: str) -> bool:
        if self._closing or generation != self._session_generation:
            return False
        if not self.session_interactive.isChecked():
            return False
        current = normalize_host(self.host_edit.text())
        return host.casefold() == current.casefold()

    def _on_sessions_result(self, generation: int, host: str, sessions, error: str) -> None:
        if not self._session_result_is_current(generation, host):
            return
        self._session_loaded_generation = generation
        self._session_querying = False
        items = [item for item in (sessions or []) if isinstance(item, RemoteSession)]
        if error:
            _kind, message = classify_session_query(error, count=0)
            message = self.tr(message)
            if self._session_unconfirmed:
                self._session_block_reason = message
            self._set_session_status(message)
            self.session_combo.setToolTip(message)
            self._log_session_status(message)
            self._update_session_refresh_enabled()
            self._emit_session_preview()
            return
        self._fill_session_combo(items)
        self._update_session_refresh_enabled()
        self._emit_session_preview()

    def _fill_session_combo(self, sessions: list) -> None:
        host = normalize_host(self.host_edit.text()).casefold()
        pick = self._session_pick
        matched = None
        missing = None
        if pick and pick[0] == host:
            for item in sessions:
                same_id = int(item.session_id) == int(pick[1])
                same_user = (item.username or "").casefold() == pick[2]
                if same_id and same_user:
                    matched = item
                    break
            else:
                missing = pick
        elif pick and pick[0] != host:
            self._session_pick = None

        self._filling_sessions = True
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("Console padrão (sem ID)"), None)
        seen: set[int] = set()
        for item in sessions:
            sid = int(item.session_id)
            if sid in seen:
                continue
            seen.add(sid)
            self.session_combo.addItem(
                format_psexec_session(item),
                ("session", sid, item.username or ""),
            )
        index = 0
        if missing is not None:
            self.session_combo.addItem(
                self.tr(f"Sessão {missing[1]} não está mais disponível"),
                ("missing", int(missing[1]), missing[2]),
            )
            index = self.session_combo.count() - 1
            self._session_unconfirmed = False
            self._session_block_reason = self.tr(_SESSION_MISSING_REASON)
            self._set_session_status(self._session_block_reason)
        elif matched is not None:
            for row in range(self.session_combo.count()):
                data = self.session_combo.itemData(row)
                if (
                    isinstance(data, tuple)
                    and data
                    and data[0] == "session"
                    and int(data[1]) == int(matched.session_id)
                    and (data[2] or "").casefold() == (matched.username or "").casefold()
                ):
                    index = row
                    break
            self._session_pick = (
                host,
                int(matched.session_id),
                (matched.username or "").casefold(),
            )
            self._session_unconfirmed = False
            self._session_block_reason = ""
            self._set_session_status("")
        else:
            self._session_pick = None
            self._session_unconfirmed = False
            self._session_block_reason = ""
            if sessions:
                self._set_session_status("")
            else:
                self._set_session_status(self.tr("Nenhuma sessão encontrada."))
        self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)
        self.session_combo.setToolTip(self.tr(TOOLTIPS["session_id"]))
        self._filling_sessions = False

    def _log_session_status(self, message: str) -> None:
        log = getattr(self, "log_output", None)
        if log is None or not hasattr(log, "append_log"):
            return
        log.append_log(self.tr(f"[PSEXEC] Sessões: {message}"))

    def _on_session_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, QThread):
            return
        pending = self._session_pending
        self._session_pending = False
        if worker is self._session_worker:
            self._session_worker = None
        self._session_querying = False
        self._release_worker(worker)
        self._update_session_refresh_enabled()
        if self._closing:
            return
        if pending:
            self._refresh_remote_sessions()

    def _drop_session_for_other_host(self, host: str) -> None:
        """A lista de um computador não pode ser reutilizada em outro."""
        current = (host or "").casefold()
        previous = ""
        if self._session_context is not None:
            previous = str(self._session_context[0] or "")
        pick_host = self._session_pick[0] if self._session_pick else ""
        if (previous and previous != current) or (pick_host and pick_host != current):
            self._reset_session_combo(keep_enabled=self.session_interactive.isChecked())

    def _update_affinity_validator(self, group_id: Optional[int]) -> None:
        limit, label = affinity_limit_for(
            group_id,
            self._cpu_topology.groups if self._cpu_topology is not None else (),
            verified=self._cpu_topology_is_verified() and not self._cpu_group_block_text(),
        )
        self.affinity_validator.set_max_cpu(limit)
        self.affinity_validator.set_group_label(label)
        self.affinity_edit.setPlaceholderText(self.tr("1,2,3"))

    def confirm_realtime_priority(self) -> bool:
        """Pede confirmação só para -realtime. As demais prioridades seguem direto."""
        if (self.priority_combo.currentData() or "") != "-realtime":
            return True
        answer = QMessageBox.question(
            self,
            self.tr("Prioridade de tempo real"),
            self.tr(
                "Tempo real pode tornar o computador remoto instável.\n\n"
                "Executar mesmo assim?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _sync_realtime_warning(self) -> None:
        if not hasattr(self, "realtime_warning"):
            return
        self.realtime_warning.setVisible(
            (self.priority_combo.currentData() or "") == "-realtime"
        )

    def _cpu_context_key(self) -> tuple:
        return (
            normalize_host(self.host_edit.text()).casefold(),
            self.auth_kind.value,
            self.auth_username(),
            self._password_mark(),
        )

    def _cpu_topology_is_verified(self) -> bool:
        return self._cpu_state == "available" and self._cpu_topology is not None

    def _cpu_topology_pairs(self) -> tuple[tuple[int, int], ...]:
        if not self._cpu_topology_is_verified() or self._cpu_topology is None:
            return ()
        return tuple(
            (group.group_id, group.logical_count) for group in self._cpu_topology.groups
        )

    def _cpu_group_is_unavailable(self) -> bool:
        data = self.group_combo.currentData()
        return isinstance(data, tuple) and bool(data) and data[0] == "missing"

    def _cpu_group_block_text(self) -> str:
        if not self._cpu_group_is_unavailable():
            return ""
        return (self._cpu_group_block_reason or "").strip()

    def _selected_cpu_group_id(self) -> Optional[int]:
        data = self.group_combo.currentData()
        if isinstance(data, tuple) and data and data[0] == "missing":
            try:
                return int(data[1])
            except (TypeError, ValueError):
                return None
        if isinstance(data, bool) or data is None:
            return None
        if isinstance(data, int):
            return int(data)
        return None

    def _note_cpu_context(self) -> None:
        """Invalida topologia quando o host ou a conta mudam. Não reaproveita limites."""
        if self._cpu_context is None or not hasattr(self, "group_combo"):
            return
        if self._noting_cpu:
            return
        key = self._cpu_context_key()
        if key == self._cpu_context:
            return
        previous = self._cpu_context
        host_changed = previous[0] != key[0]
        self._noting_cpu = True
        try:
            self._cpu_context = key
            self._cpu_generation += 1
            self._cpu_refresh_timer.stop()
            self._invalidate_cpu_topology(host_changed=host_changed)
            worker = self._cpu_worker
            if worker is not None and worker.isRunning():
                worker.cancel()
                self._cpu_pending = True
            elif (not host_changed) and self._cpu_query_applicable():
                self._cpu_refresh_timer.start()
        finally:
            self._noting_cpu = False
        if not self._updating_option_state:
            self.update_psexec_option_state("-g")

    def _invalidate_cpu_topology(self, *, host_changed: bool) -> None:
        had_remote = self._cpu_state in {"available", "failed", "querying", "stale"} or (
            self._cpu_topology is not None
        )
        selected = None if host_changed else self._selected_cpu_group_id()
        self._cpu_topology = None
        self._cpu_loaded_generation = -1
        self._cpu_failure = ""
        self._cpu_group_block_reason = ""
        self._cpu_querying = False
        if host_changed:
            self._cpu_state = "stale" if had_remote else "not_queried"
        else:
            self._cpu_state = "stale" if had_remote or selected is not None else "not_queried"
        self._rebuild_group_combo(None, preserve=selected)
        self._refresh_cpu_status()

    def _rebuild_group_combo(
        self,
        groups: Optional[tuple[ProcessorGroup, ...]],
        *,
        preserve: Optional[int],
    ) -> None:
        self._rebuilding_cpu_groups = True
        self.group_combo.blockSignals(True)
        self.group_combo.clear()
        self.group_combo.addItem(self.tr("Automático (sem -g)"), None)
        self._cpu_group_block_reason = ""
        index = 0
        if groups:
            for group in groups:
                count = int(group.logical_count)
                word = "processador" if count == 1 else "processadores"
                self.group_combo.addItem(
                    self.tr(f"{group.group_id} · {count} {word}"),
                    int(group.group_id),
                )
            if preserve is not None:
                found = -1
                for row in range(self.group_combo.count()):
                    if self.group_combo.itemData(row) == int(preserve):
                        found = row
                        break
                if found >= 0:
                    index = found
                else:
                    self.group_combo.addItem(
                        self.tr(f"Grupo {preserve} não está mais disponível"),
                        ("missing", int(preserve)),
                    )
                    index = self.group_combo.count() - 1
                    self._cpu_group_block_reason = self.tr(
                        f"Grupo CPU {preserve} não existe neste computador."
                    )
        elif preserve is not None:
            self.group_combo.addItem(
                self.tr(f"Grupo {preserve} (não verificado)"),
                int(preserve),
            )
            index = 1
        self.group_combo.setCurrentIndex(index)
        self.group_combo.blockSignals(False)
        self._rebuilding_cpu_groups = False

    def _refresh_cpu_status(self) -> None:
        if not hasattr(self, "cpu_status"):
            return
        text, tip = describe_cpu_status(
            state=self._cpu_state,
            topology=self._cpu_topology if self._cpu_state == "available" else None,
            affinity=self.affinity_edit.text() if hasattr(self, "affinity_edit") else "",
            cpu_group=self._selected_cpu_group_id() if hasattr(self, "group_combo") else None,
            failure=self._cpu_failure,
            group_block_reason=self._cpu_group_block_text(),
        )
        self.cpu_status.setText(self.tr(text))
        self.cpu_status.setToolTip(self.tr(tip))

    def _on_affinity_rejected(self, reason: str) -> None:
        if reason and hasattr(self, "cpu_status"):
            self.cpu_status.setText(self.tr(reason))

    def _sync_cpu_host_state(self, state: str) -> None:
        if not hasattr(self, "cpu_status"):
            return
        if state in {"offline", "invalid", "err"}:
            self._cpu_refresh_timer.stop()
            if self._cpu_state == "available" or self._cpu_topology is not None:
                self._invalidate_cpu_topology(host_changed=False)
            self._cpu_state = "unavailable"
            self._cpu_failure = ""
            self._update_cpu_query_enabled()
            self._refresh_cpu_status()
            if not self._updating_option_state:
                self.update_psexec_option_state("-g")
            return
        elif state == "idle":
            self._cpu_refresh_timer.stop()
            if self._cpu_state != "available":
                self._cpu_state = "not_queried"
                self._cpu_topology = None
        self._update_cpu_query_enabled()
        self._refresh_cpu_status()

    def _cpu_query_applicable(self) -> bool:
        host = normalize_host(self.host_edit.text())
        return bool(
            self.is_host_online
            and host
            and is_valid_host(host)
            and not self._closing
        )

    def _update_cpu_query_enabled(self) -> None:
        if not hasattr(self, "cpu_query_btn"):
            return
        enabled = self._cpu_query_applicable() and not self._cpu_querying
        self.cpu_query_btn.setEnabled(enabled)
        if self._cpu_querying:
            self.cpu_query_btn.setToolTip(self.tr("Consultando processadores…"))
        else:
            self.cpu_query_btn.setToolTip(self.tr("Atualizar"))

    def _schedule_cpu_refresh(self) -> None:
        if not hasattr(self, "_cpu_refresh_timer") or self._closing:
            return
        self._update_cpu_query_enabled()
        if not self._cpu_query_applicable():
            return
        self._cpu_refresh_timer.start()

    def _on_cpu_query_clicked(self) -> None:
        self._cpu_refresh_timer.stop()
        self._refresh_cpu_topology(manual=True)

    def _refresh_cpu_topology(self, *, manual: bool = False) -> None:
        if self._closing:
            return
        if not self._cpu_query_applicable():
            self._update_cpu_query_enabled()
            return
        worker = self._cpu_worker
        running = worker is not None and worker.isRunning()
        if running:
            if worker._generation != self._cpu_generation:
                worker.cancel()
                self._cpu_pending = True
            return
        if (
            not manual
            and not self._cpu_pending
            and self._cpu_loaded_generation == self._cpu_generation
        ):
            self._update_cpu_query_enabled()
            return
        self._cpu_pending = False
        self._cpu_querying = True
        self._cpu_state = "querying"
        self._cpu_failure = ""
        self._cpu_topology = None
        self._rebuild_group_combo(None, preserve=self._selected_cpu_group_id())
        self._refresh_cpu_status()
        self._update_cpu_query_enabled()
        if not self._updating_option_state:
            self.update_psexec_option_state("-g")
        generation = self._cpu_generation
        host = normalize_host(self.host_edit.text())
        self._cpu_worker = _CpuTopologyWorker(
            generation,
            host,
            user=self.auth_username(),
            password=self.auth_password(),
            parent=self,
        )
        self._remember_worker(self._cpu_worker)
        self._cpu_worker.result.connect(self._on_cpu_result)
        self._cpu_worker.finished.connect(self._on_cpu_worker_finished)
        self._cpu_worker.start()

    def _cpu_result_is_current(self, generation: int, host: str) -> bool:
        if self._closing or generation != self._cpu_generation or not self.is_host_online:
            return False
        current = normalize_host(self.host_edit.text())
        return host.casefold() == current.casefold()

    def _on_cpu_result(self, generation: int, host: str, topology, error: str) -> None:
        if not self._cpu_result_is_current(generation, host):
            return
        self._cpu_loaded_generation = generation
        self._cpu_querying = False
        selected = self._selected_cpu_group_id()
        message = (error or "").strip()
        if message or not isinstance(topology, CpuTopology):
            self._cpu_state = "failed"
            self._cpu_failure = message or "Falha ao consultar os processadores."
            self._cpu_topology = None
            self._rebuild_group_combo(None, preserve=selected)
        else:
            self._cpu_state = "available"
            self._cpu_failure = ""
            self._cpu_topology = topology
            self._rebuild_group_combo(topology.groups, preserve=selected)
        self._update_cpu_query_enabled()
        if not self._updating_option_state:
            self.update_psexec_option_state("-g")
        else:
            self._refresh_cpu_status()

    def _on_cpu_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, QThread):
            return
        pending = self._cpu_pending
        self._cpu_pending = False
        if worker is self._cpu_worker:
            self._cpu_worker = None
        self._release_worker(worker)
        if self._closing:
            return
        if pending:
            self._refresh_cpu_topology()
            return
        if self._cpu_querying:
            self._cpu_querying = False
            if self._cpu_state == "querying":
                self._cpu_state = "not_queried"
            self._update_cpu_query_enabled()
            self._refresh_cpu_status()

    @property
    def is_host_online(self) -> bool:
        return bool(self._host_online)

    def _apply_module_button(
        self,
        btn,
        *,
        enabled: bool,
        online: bool,
        ok_tip: str,
        missing_tip: str,
    ) -> None:
        btn.setEnabled(bool(enabled))
        if online and not enabled:
            btn.setToolTip(missing_tip)
        else:
            btn.setToolTip(ok_tip)

    def has_psexec_exe(self) -> bool:
        from remoteops.utils.pstools import probe_pstools

        tools = probe_pstools().get("tools") or []
        return bool(tools and tools[0].get("found"))

    def can_run_psexec(self) -> bool:
        """Host alcançável e PsExec.exe presente na pasta configurada."""
        return bool(self._host_online) and self.has_psexec_exe()

    def _update_host_action_buttons(self, online: bool | None = None) -> None:
        """Host online não basta: cada módulo exige o binário correspondente."""
        from remoteops.utils.handle import handle_available
        from remoteops.utils.pstools import probe_pstools, probe_rustdesk_local

        is_online = self._host_online if online is None else bool(online)
        found = {
            str(item.get("label") or ""): bool(item.get("found"))
            for item in (probe_pstools().get("tools") or [])
        }
        has_psexec = bool(found.get("PsExec"))
        missing_psexec = self.tr(
            "PsExec não encontrado na pasta PSTools configurada."
        )
        missing_in_pstools = self.tr(
            "{0} não encontrado na pasta PSTools configurada."
        )

        self._apply_module_button(
            self.psinfo_button,
            enabled=is_online and bool(found.get("PsInfo")),
            online=is_online,
            ok_tip=self.tr("Abrir Inventário"),
            missing_tip=missing_in_pstools.format("PsInfo"),
        )
        has_sessoes = bool(
            found.get("PsLoggedOn")
            or found.get("PsFile")
            or (handle_available() and has_psexec)
        )
        self._apply_module_button(
            self.sessoes_arquivos_button,
            enabled=is_online and has_sessoes,
            online=is_online,
            ok_tip=self.tr("Sessões e arquivos remotos"),
            missing_tip=self.tr(
                "PsLoggedOn e PsFile não encontrados na pasta PSTools. "
                "Handle exige Handle.exe e PsExec."
            ),
        )
        self._apply_module_button(
            self.contas_locais_button,
            enabled=is_online and has_psexec,
            online=is_online,
            ok_tip=self.tr("Contas locais e alteração de senha (PsPasswd)"),
            missing_tip=missing_psexec,
        )
        self._apply_module_button(
            self.processos_button,
            enabled=is_online and bool(found.get("PsList")),
            online=is_online,
            ok_tip=self.tr("Processos remotos (PsList)"),
            missing_tip=missing_in_pstools.format("PsList"),
        )
        self._apply_module_button(
            self.servicos_button,
            enabled=is_online and bool(found.get("PsService")),
            online=is_online,
            ok_tip=self.tr("Serviços remotos (PsService)"),
            missing_tip=missing_in_pstools.format("PsService"),
        )
        self.hostapps_button.setEnabled(is_online)
        self.hostapps_button.setToolTip(
            self.tr("Listar aplicativos do host (Remote Registry)")
        )
        self._apply_module_button(
            self.winget_button,
            enabled=is_online and has_psexec,
            online=is_online,
            ok_tip=self.tr("WinGet — pacotes remotos (winget)"),
            missing_tip=missing_psexec,
        )
        self.printers_button.setEnabled(is_online)
        self.refresh_printers_button_tooltip()
        if is_online and not has_psexec:
            base = self.printers_button.toolTip() or self.tr("Impressoras de rede")
            self.printers_button.setToolTip(
                self.tr(
                    "{0}\nInstalar, remover ou definir padrão exige "
                    "PsExec na pasta PSTools configurada."
                ).format(base)
            )
        self._apply_module_button(
            self.message_button,
            enabled=is_online and has_psexec,
            online=is_online,
            ok_tip=self.tr("Enviar mensagem ao host"),
            missing_tip=missing_psexec,
        )
        rustdesk_found = bool(probe_rustdesk_local().get("found"))
        if is_online and not has_psexec:
            rustdesk_missing = missing_psexec
        elif is_online and not rustdesk_found:
            rustdesk_missing = self.tr(
                "rustdesk.exe não encontrado na pasta RustDesk configurada."
            )
        else:
            rustdesk_missing = missing_psexec
        self._apply_module_button(
            self.rustdesk_button,
            enabled=is_online and has_psexec and rustdesk_found,
            online=is_online,
            ok_tip=self.tr("Conectar via RustDesk"),
            missing_tip=rustdesk_missing,
        )
        self._apply_module_button(
            self.energia_button,
            enabled=is_online and bool(found.get("PsShutdown")),
            online=is_online,
            ok_tip=self.tr("Gerenciamento de energia remoto (PsShutdown)"),
            missing_tip=missing_in_pstools.format("PsShutdown"),
        )

    def refresh_host_action_buttons(self, online: bool | None = None) -> None:
        self._update_host_action_buttons(online)

    def refresh_processos_button_state(self, online: bool | None = None) -> None:
        self._update_host_action_buttons(online)

    def refresh_servicos_button_state(self, online: bool | None = None) -> None:
        self._update_host_action_buttons(online)

    def refresh_contas_locais_button_state(self, online: bool | None = None) -> None:
        self._update_host_action_buttons(online)

    def refresh_energia_button_state(self, online: bool | None = None) -> None:
        self._update_host_action_buttons(online)

    def refresh_sessoes_arquivos_button_state(
        self, online: bool | None = None
    ) -> None:
        self._update_host_action_buttons(online)

    def refresh_rustdesk_button_state(self, online: bool | None = None) -> None:
        self._update_host_action_buttons(online)

    def _set_host_status(
        self,
        state: str,
        text: str | None = None,
        *,
        psexec_enabled: bool | None = None,
        tooltip: str | None = None,
    ) -> None:
        color = _STATUS_COLORS.get(state, _STATUS_COLORS["idle"])
        self.host_status_dot.set_color(color)
        labels = {
            "idle": self.tr("Aguardando host"),
            "checking": self.tr("Verificando…"),
            "online": self.tr("Rede acessível"),
            "offline": self.tr("Host inacessível"),
            "invalid": self.tr("Host inválido"),
            "warn": self.tr("Atenção"),
            "err": self.tr("Falha na verificação"),
        }
        caption = text if text is not None else labels.get(state, "")
        self.host_status_label.setText(caption)
        tip = tooltip if tooltip is not None else caption
        self.host_status_dot.setToolTip(tip)
        self.host_status_label.setToolTip(tip)
        host_now = normalize_host(self.host_edit.text())
        self.recheck_btn.setEnabled(bool(host_now) and is_valid_host(host_now))

        if psexec_enabled is None:
            online = state == "online"
        else:
            online = bool(psexec_enabled)
        self._update_host_action_buttons(online)
        if self._host_online != online:
            self._host_online = online
            self.hostOnlineChanged.emit(online)
            if online:
                self._start_domain_worker(normalize_host(self.host_edit.text()))
                if self.session_interactive.isChecked():
                    self._schedule_session_refresh()
                if self._cpu_state == "unavailable":
                    self._cpu_state = "not_queried"
                self._schedule_cpu_refresh()
        self._update_session_refresh_enabled()
        self._sync_cpu_host_state(state)

    def _apply_reachability(self, reach: HostReachability) -> None:
        if reach.cancelled:
            return
        self._last_reachability = reach
        self._set_host_status(
            reach.status_state,
            reach.caption,
            psexec_enabled=reach.psexec_enabled,
            tooltip=reach.tooltip or reach.caption,
        )

    def refresh_printers_button_tooltip(self) -> None:
        server = get_print_server()
        if not server:
            self.printers_button.setToolTip(self.tr("Impressoras de rede"))
            return
        try:
            unc = print_server_unc(server)
        except ValueError:
            self.printers_button.setToolTip(self.tr("Impressoras de rede"))
            return
        self.printers_button.setToolTip(self.tr(f"Impressoras de rede ({unc})"))

    def _on_host_text_changed(self, _text: str = "") -> None:
        if self._closing:
            return
        host = normalize_host(self.host_edit.text())
        self._drop_session_for_other_host(host)
        self._note_session_context()
        self._note_cpu_context()
        self._update_session_refresh_enabled()
        self._host_status_wanted = host
        self._last_reachability = None
        self._apply_host_userdomain_hint(host)
        if not host:
            self._host_status_timer.stop()
            self._set_host_status("idle", psexec_enabled=False)
            return
        if not is_valid_host(host):
            self._host_status_timer.stop()
            self._set_host_status("invalid", psexec_enabled=False)
            return
        self._set_host_status("checking", psexec_enabled=False)
        self._host_status_timer.start()

    def _apply_host_userdomain_hint(self, host: str) -> None:
        if not hasattr(self, "domain_edit"):
            return
        if not host or not is_valid_host(host):
            self._host_userdomain = HostUserDomain()
            if not self._domain_manual:
                self._apply_discovered_domain()
            return
        if self._domain_manual:
            return
        hint = domain_hint_from_hostname(host)
        if hint.name:
            self._host_userdomain = hint
            self._apply_discovered_domain()
            return
        self._host_userdomain = HostUserDomain()
        self._apply_discovered_domain()

    def _start_domain_worker(self, host: str) -> None:
        if self._closing:
            return
        host = normalize_host(host)
        if not host or not is_valid_host(host):
            return
        self._domain_wanted = host
        worker = self._domain_worker
        if worker is not None and worker.isRunning():
            return
        self._domain_worker = _HostDomainWorker(host, self)
        self._remember_worker(self._domain_worker)
        self._domain_worker.result.connect(self._on_host_domain_result)
        self._domain_worker.finished.connect(self._on_domain_worker_finished)
        self._domain_worker.start()

    def _on_host_domain_result(self, host: str, info: object) -> None:
        wanted = self._domain_wanted
        if host.casefold() != (wanted or "").casefold():
            return
        current = normalize_host(self.host_edit.text())
        if host.casefold() != current.casefold():
            return
        domain = info if isinstance(info, HostUserDomain) else HostUserDomain()
        if not domain.name:
            return
        self._host_userdomain = domain
        self._apply_discovered_domain()

    def _on_domain_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, QThread):
            return
        finished_host = getattr(worker, "_host", "") or ""
        if worker is self._domain_worker:
            self._domain_worker = None
        self._release_worker(worker)
        if self._closing:
            return

        current = normalize_host(self.host_edit.text())
        if (
            self.is_host_online
            and current
            and is_valid_host(current)
            and current.casefold() != (finished_host or "").casefold()
        ):
            self._start_domain_worker(current)

    def _remember_worker(self, worker: QThread) -> None:
        if worker not in self._live_workers:
            self._live_workers.append(worker)

    def _release_worker(self, worker: Optional[QThread]) -> None:
        if worker is None:
            return
        self._live_workers = [item for item in self._live_workers if item is not worker]
        delete = getattr(worker, "deleteLater", None)
        if callable(delete):
            delete()

    def shutdown(self, wait_ms: int = 2000) -> None:
        """Cancela verificações em andamento e espera o encerramento cooperativo."""
        self._closing = True
        self._host_status_timer.stop()
        self._session_refresh_timer.stop()
        self._cpu_refresh_timer.stop()
        self._host_status_generation += 1
        self._tcp_precheck_generation += 1
        self._session_generation += 1
        self._cpu_generation += 1
        self._cpu_pending = False
        self._session_pending = False
        self._host_status_pending = None
        for worker in list(self._live_workers):
            if worker.isRunning() and hasattr(worker, "cancel"):
                try:
                    worker.cancel()
                except Exception:
                    pass
        for worker in list(self._live_workers):
            if worker.isRunning():
                worker.wait(max(0, int(wait_ms)))

    def _check_host_status(self, *, force: bool = False) -> None:
        if self._closing:
            return
        host = normalize_host(self.host_edit.text())
        self._host_status_wanted = host
        if not host:
            self._supersede_host_check()
            self._last_reachability = None
            self._set_host_status("idle", psexec_enabled=False)
            return
        if not is_valid_host(host):
            self._supersede_host_check()
            self._last_reachability = None
            self._set_host_status("invalid", psexec_enabled=False)
            return

        worker = self._host_status_worker
        if (
            worker is not None
            and worker.isRunning()
            and not force
            and not getattr(worker, "_cancel", False)
            and str(getattr(worker, "_host", "") or "").casefold() == host.casefold()
        ):
            return

        self._host_status_generation += 1
        generation = self._host_status_generation
        self._set_host_status("checking", psexec_enabled=False)
        if worker is not None and worker.isRunning():
            worker.cancel()
            self._host_status_pending = (host, generation, force)
            return
        self._host_status_pending = None
        self._start_host_status_worker(host, generation, use_cache=not force)

    def _supersede_host_check(self) -> None:
        self._host_status_generation += 1
        self._host_status_pending = None
        worker = self._host_status_worker
        if worker is not None and worker.isRunning():
            worker.cancel()

    def _recheck_host_status(self) -> None:
        self._host_status_timer.stop()
        self.refresh_host_status()

    def refresh_host_status(self) -> None:
        """Reconsulta o host atual, sem reutilizar o cache."""
        host = normalize_host(self.host_edit.text())
        if not host or not is_valid_host(host):
            return
        self._last_reachability = None
        self._check_host_status(force=True)

    def current_host(self) -> str:
        return normalize_host(self.host_edit.text())

    def has_fresh_tcp445(self) -> bool:
        from remoteops.utils.host_reachability import has_fresh_tcp445

        host = self.current_host()
        if not host or not is_valid_host(host):
            return False
        return has_fresh_tcp445(host)

    def start_tcp_precheck(self) -> None:
        if self._closing:
            self.tcpPrecheckFinished.emit(False, "")
            return
        host = self.current_host()
        self._tcp_precheck_generation += 1
        gen = self._tcp_precheck_generation
        previous = self._tcp_precheck_worker
        if previous is not None and previous.isRunning():
            previous.cancel()
        worker = _TcpPrecheckWorker(host, self)
        self._tcp_precheck_worker = worker
        self._remember_worker(worker)
        worker.result.connect(
            lambda h, ping, g=gen: self._on_tcp_precheck_result(h, ping, g)
        )
        worker.finished.connect(self._on_tcp_precheck_worker_finished)
        worker.start()

    def cancel_tcp_precheck(self) -> None:
        self._tcp_precheck_generation += 1
        worker = self._tcp_precheck_worker
        if worker is not None and worker.isRunning():
            worker.cancel()

    def _on_tcp_precheck_result(self, host: str, ping: object, generation: int) -> None:
        if generation != self._tcp_precheck_generation:
            return
        current = self.current_host()
        if is_stale_host_result(host, host, current):
            self.tcpPrecheckFinished.emit(False, "")
            return
        from remoteops.utils.psping import PsPingResult

        result = ping if isinstance(ping, PsPingResult) else None
        if result is None or result.state == PsPingState.CANCELLED or result.cancelled:
            self.tcpPrecheckFinished.emit(False, "")
            return
        if result.ok:
            self.tcpPrecheckFinished.emit(True, "")
            return
        icmp_ok = None
        last = self._last_reachability
        if last is not None and last.host.casefold() == current.casefold():
            icmp_ok = last.icmp_ok
        self.tcpPrecheckFinished.emit(False, psexec_tcp_blocked_message(icmp_ok=icmp_ok))

    def _on_tcp_precheck_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, QThread):
            return
        if worker is self._tcp_precheck_worker:
            self._tcp_precheck_worker = None
        self._release_worker(worker)

    def _start_host_status_worker(
        self, host: str, generation: int, *, use_cache: bool
    ) -> None:
        if self._closing:
            return
        worker = _HostStatusWorker(
            host, generation, use_cache=use_cache, parent=self
        )
        self._host_status_worker = worker
        self._remember_worker(worker)
        worker.result.connect(self._on_host_status_result)
        worker.finished.connect(self._on_host_status_worker_finished)
        worker.start()

    def _on_host_status_result(self, generation: int, host: str, reach: object) -> None:
        if self._closing or generation != self._host_status_generation:
            return
        wanted = self._host_status_wanted
        current = normalize_host(self.host_edit.text())
        if is_stale_host_result(host, wanted, current):
            return
        info = reach if isinstance(reach, HostReachability) else None
        if info is None or info.cancelled:
            return
        self._apply_reachability(info)

    def _on_host_status_worker_finished(self) -> None:
        worker = self.sender()
        if not isinstance(worker, QThread):
            return
        if worker is self._host_status_worker:
            self._host_status_worker = None
        self._release_worker(worker)
        if self._closing:
            self._host_status_pending = None
            return
        pending = self._host_status_pending
        self._host_status_pending = None
        if pending is None:
            return
        host, generation, force = pending
        if generation != self._host_status_generation:
            return
        current = normalize_host(self.host_edit.text())
        if current.casefold() != str(host).casefold() or not is_valid_host(current):
            return
        self._start_host_status_worker(current, generation, use_cache=not force)
