"""Aba Energia — PsShutdown em um único host remoto."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional, Tuple

from PyQt6 import sip
from PyQt6.QtCore import QTime, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QTabWidget,
    QTableWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from remoteops.services.power import (
    PowerResult,
    PowerService,
    current_operator,
    list_power_sessions,
    sessions_warning,
)
from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    COLOR_TEXT_SECONDARY,
    RADIUS_MEDIUM,
    SIZE_UI_SMALL,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    accent_button_qss,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    bind_card_stack,
    make_card_stack,
)
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.flow import FlowLayout
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.mdl2_tab_bar import Mdl2TabBar
from remoteops.ui.widgets.spinbox import StepSpinBox
from remoteops.ui.widgets.status_dot import StatusDot
from remoteops.ui.widgets.table import SortableTableItem, configure_standard_table
from remoteops.utils.installed_printers import session_account
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.printers import active_sessions
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.psshutdown import (
    ADVANCED_ACTIONS,
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_COUNTDOWN_SECONDS,
    MAX_CONNECT_TIMEOUT_SECONDS,
    MAX_COUNTDOWN_SECONDS,
    MAX_MESSAGE_LENGTH,
    PRIMARY_ACTIONS,
    PowerAction,
    PowerPhase,
    PowerRequest,
    ShutdownReasonKind,
    TimingMode,
    action_profile,
    apply_action_defaults,
    build_psshutdown_argv,
    confirmation_summary,
    find_reason_option,
    preview_psshutdown_argv,
    psshutdown_available,
    reason_display,
    reason_option_label,
    reasons_for_kind,
    resolve_psshutdown_exe,
    suggested_reason_message,
    validate_power_request,
)
from remoteops.utils.redaction import redact_command_text
from remoteops.utils.sessions import RemoteSession

_EMPTY_FACT = "—"
_CAPTION_QSS = f"""
QLabel#energiaFactCaption {{
    color: {COLOR_TEXT_SECONDARY};
    font-size: {SIZE_UI_SMALL}pt;
    background: transparent;
    border: none;
}}
"""

_ACTION_BTN_QSS = (
    accent_button_qss("QPushButton", radius=RADIUS_MEDIUM, padding="4px 10px")
    + f"""
    QPushButton:checked {{
        background: {COLOR_ACCENT_SOFT};
        border-color: {COLOR_ACCENT};
        color: {COLOR_ACCENT};
    }}
    """
)


def _caption_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("energiaFactCaption")
    lbl.setStyleSheet(_CAPTION_QSS)
    return lbl


def _value_label() -> QLabel:
    lbl = QLabel(_EMPTY_FACT)
    lbl.setObjectName("energiaFactValue")
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


def _control_column(caption: str, widget: QWidget) -> QWidget:
    """Campo de formulário compacto: legenda em cima, controle embaixo."""
    cell = QWidget()
    cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = QVBoxLayout(cell)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(2)
    lay.addWidget(_caption_label(caption), 0)
    widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    lay.addWidget(widget, 0)
    return cell


def _pair_row(*cells: QWidget, stretches: tuple[int, ...] | None = None) -> QWidget:
    """Empilha controles lado a lado para ocupar a largura do card."""
    row = QWidget()
    row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(SPACE_MD + 4)
    weights = stretches or tuple(1 for _ in cells)
    for cell, stretch in zip(cells, weights):
        lay.addWidget(cell, max(1, int(stretch)))
    return row


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


class _SessionWorker(QThread):
    result = pyqtSignal(str, object, str)

    def __init__(self, host: str, user: str = "", password: str = "", parent=None):
        super().__init__(parent)
        self._host = host
        self._user = user
        self._password = password

    def run(self) -> None:
        try:
            sessions, error = list_power_sessions(
                self._host, user=self._user, password=self._password
            )
            self.result.emit(self._host, sessions, error)
        finally:
            self._password = ""


class _PowerWorker(QThread):
    progress = pyqtSignal(str)
    finished_result = pyqtSignal(object)
    finished_err = pyqtSignal(str)

    def __init__(
        self,
        request: PowerRequest,
        *,
        user: str = "",
        password: str = "",
        current_host: str = "",
        follow_up: bool = False,
        parent=None,
    ):
        super().__init__(parent)
        self.request = request
        self.user = user
        self.password = password
        self.current_host = current_host
        self.follow_up = follow_up
        self._abort = False

    def cancel(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            result = PowerService().run(
                self.request,
                user=self.user,
                password=self.password,
                pstools_dir=get_pstools_dir(),
                should_cancel=lambda: self._abort,
                current_host=self.current_host,
                follow_up=self.follow_up,
                on_progress=self.progress.emit,
                operator=current_operator(),
            )
            self.finished_result.emit(result)
        except Exception as exc:
            self.finished_err.emit(str(exc) or "Falha ao executar o PsShutdown.")
        finally:
            self.password = ""


class EnergiaTab(QWidget):
    """Gerenciamento de energia remoto via PsShutdown (host único)."""

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
        self._session_worker: Optional[_SessionWorker] = None
        self._power_worker: Optional[_PowerWorker] = None
        self._sessions: list[RemoteSession] = []
        self._sessions_host = ""
        self._history: list[dict] = []
        self._closing = False
        self._busy = False
        self._abort_after_stop = False
        self._selected = PowerAction.RESTART
        self._action_buttons: dict[PowerAction, QPushButton] = {}
        self._auto_message = ""

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)
        dest = self._build_destination_card()
        action = self._build_action_card()
        schedule = self._build_schedule_card()
        output = self._build_output_card()
        root.addWidget(dest)
        root.addWidget(action)
        root.addWidget(schedule)
        root.addWidget(output, 3)
        bind_card_stack(root, (dest, action, schedule, output))
        if self._host_source is not None:
            self._host_source.textChanged.connect(self.sync_from_host)
        self.destroyed.connect(self._on_destroyed)
        self._apply_action_profile(self._selected, reset_timing=True)
        self.sync_from_host()
        self.refresh_sessions()

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
        if not online:
            self._invalidate_sessions()
        self._refresh_actions()

    def sync_from_host(self, *_args) -> None:
        if not self._ui_alive():
            return
        host = self._get_host()
        if host.casefold() != (self._sessions_host or "").casefold():
            self._invalidate_sessions()
        self._apply_host_status(online=self._is_online())
        self._refresh_preview()
        self._refresh_actions()
        if host and is_valid_host(host) and self._is_online() and not self._sessions:
            self.refresh_sessions()

    def refresh_tool_capabilities(self) -> None:
        self._refresh_actions()
        self._refresh_preview()

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._closing = True
        self._abort_after_stop = False
        for attr in ("_session_worker", "_power_worker"):
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

    def _on_destroyed(self, _destroyed: object = None) -> None:
        self._closing = True
        self.shutdown(wait_ms=2000)

    def _build_destination_card(self) -> CardWidget:
        card = CardWidget("\uEA18", self.tr("Destino"))
        card.set_collapsible(True, collapsed=False)
        card.content_layout.setSpacing(SPACE_XS)
        self.refresh_sessions_btn = card.make_header_button(
            "\uE72C", self.tr("Atualizar o usuário ativo do host")
        )
        self.refresh_sessions_btn.clicked.connect(self.refresh_sessions)
        card.add_header_button(self.refresh_sessions_btn)

        g = _column_grid(card, 3)
        host_cell, self.host_label = _fact_column(self.tr("Host"))
        estado_cell, self.host_status_dot, self.host_status_label = _status_fact(
            self.tr("Estado")
        )
        metodo_cell, self.method_label = _fact_column(self.tr("Método"))
        g.addWidget(host_cell, 0, 0, Qt.AlignmentFlag.AlignTop)
        g.addWidget(estado_cell, 0, 1, Qt.AlignmentFlag.AlignTop)
        g.addWidget(metodo_cell, 0, 2, Qt.AlignmentFlag.AlignTop)

        user_cell, self.user_status_dot, self.user_label = _status_fact(
            self.tr("Usuário ativo")
        )
        g.addWidget(user_cell, 1, 0, 1, 2, Qt.AlignmentFlag.AlignTop)
        self.method_label.setToolTip(
            self.tr(
                "Executa PsShutdown no host da aba PsExec. "
                "Sem destino o utilitário atuaria neste computador."
            )
        )
        return card

    def _build_action_card(self) -> CardWidget:
        card = CardWidget("\uE7E8", self.tr("Ação"))
        card.set_collapsible(True, collapsed=False)
        self.run_btn = card.make_header_button(
            "\uE768", self.tr("Executar ação de energia")
        )
        self.run_btn.clicked.connect(self._on_run_clicked)
        card.add_header_button(self.run_btn)
        self.stop_btn = card.make_header_button(
            "\uE71A",
            self.tr(
                "Parar só o processo local. Não cancela uma ação já aceita pelo host."
            ),
        )
        self.stop_btn.clicked.connect(self.stop_local)
        card.add_header_button(self.stop_btn)
        self.abort_btn = card.make_header_button(
            "\uE711",
            self.tr(
                "Cancelar ação agendada no host remoto (PsShutdown -a). "
                "Só funciona enquanto houver contagem regressiva no host."
            ),
        )
        self.abort_btn.clicked.connect(self.request_remote_abort)
        card.add_header_button(self.abort_btn)

        self.action_hint = QLabel()
        self.action_hint.setWordWrap(True)
        self.action_hint.setStyleSheet(
            f"color: {COLOR_TEXT_SECONDARY}; font-size: {SIZE_UI_SMALL}pt;"
        )
        card.content_layout.addWidget(self.action_hint)

        primary = QWidget()
        primary_flow = FlowLayout(primary, h_spacing=8, v_spacing=6)
        self._action_group = QButtonGroup(self)
        self._action_group.setExclusive(True)
        for action in PRIMARY_ACTIONS:
            btn = self._make_action_button(action)
            self._action_group.addButton(btn)
            primary_flow.addWidget(btn)
            self._action_buttons[action] = btn
        card.content_layout.addWidget(primary)

        advanced = CardWidget("\uE713", self.tr("Opções avançadas"))
        advanced.set_collapsible(True, collapsed=True)
        adv_wrap = QWidget()
        adv_flow = FlowLayout(adv_wrap, h_spacing=8, v_spacing=6)
        for action in ADVANCED_ACTIONS:
            btn = self._make_action_button(action)
            self._action_group.addButton(btn)
            adv_flow.addWidget(btn)
            self._action_buttons[action] = btn
        advanced.content_layout.addWidget(adv_wrap)
        card.content_layout.addWidget(advanced)
        self._action_buttons[PowerAction.RESTART].setChecked(True)
        return card

    def _make_action_button(self, action: PowerAction) -> QPushButton:
        profile = action_profile(action)
        btn = QPushButton(profile.label)
        btn.setCheckable(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setToolTip(self.tr(profile.hint))
        btn.setStyleSheet(_ACTION_BTN_QSS)
        btn.clicked.connect(lambda _checked=False, a=action: self._on_action_selected(a))
        return btn

    def _build_schedule_card(self) -> CardWidget:
        card = CardWidget("\uE823", self.tr("Agendamento e aviso"))
        card.set_collapsible(True, collapsed=False)
        card.content_layout.setSpacing(SPACE_SM)

        timing_row = QHBoxLayout()
        timing_row.setContentsMargins(0, 0, 0, 0)
        timing_row.setSpacing(SPACE_SM + 2)
        self.radio_immediate = QRadioButton(self.tr("Imediato"))
        self.radio_countdown = QRadioButton(self.tr("Contagem"))
        self.radio_scheduled = QRadioButton(self.tr("Horário"))
        self.radio_countdown.setChecked(True)
        self._timing_group = QButtonGroup(self)
        for radio in (self.radio_immediate, self.radio_countdown, self.radio_scheduled):
            self._timing_group.addButton(radio)

        self.countdown_spin = StepSpinBox()
        self.countdown_spin.setRange(0, MAX_COUNTDOWN_SECONDS)
        self.countdown_spin.setValue(DEFAULT_COUNTDOWN_SECONDS)
        self.countdown_spin.setSuffix(" s")
        self.countdown_spin.setFixedWidth(108)
        self.countdown_spin.setToolTip(self.tr("Segundos até a ação (-t)"))
        self.countdown_spin.valueChanged.connect(self._refresh_preview)

        self.time_edit = QTimeEdit()
        self.time_edit.setDisplayFormat("HH:mm")
        self.time_edit.setTime(QTime(18, 0))
        self.time_edit.setFixedWidth(96)
        self.time_edit.setToolTip(self.tr("Horário no relógio do host remoto (-t HH:mm)"))
        self.time_edit.timeChanged.connect(lambda *_: self._refresh_preview())

        timing_row.addWidget(self.radio_immediate, 0)
        timing_row.addWidget(self.radio_countdown, 0)
        timing_row.addWidget(self.countdown_spin, 0)
        timing_row.addWidget(self.radio_scheduled, 0)
        timing_row.addWidget(self.time_edit, 0)
        timing_row.addStretch(1)
        timing_wrap = QWidget()
        timing_wrap.setLayout(timing_row)
        self.radio_immediate.toggled.connect(self._on_timing_changed)
        self.radio_countdown.toggled.connect(self._on_timing_changed)
        self.radio_scheduled.toggled.connect(self._on_timing_changed)

        self.message_edit = QLineEdit()
        self.message_edit.setMaxLength(MAX_MESSAGE_LENGTH)
        self.message_edit.setPlaceholderText(
            self.tr("Obrigatória quando houver contagem. Ex.: manutenção planejada.")
        )
        self.message_edit.textChanged.connect(self._refresh_preview)

        self.notice_spin = StepSpinBox()
        self.notice_spin.setRange(0, MAX_COUNTDOWN_SECONDS)
        self.notice_spin.setValue(0)
        self.notice_spin.setSpecialValueText(self.tr("Até a ação"))
        self.notice_spin.setSuffix(" s")
        self.notice_spin.setMinimumWidth(120)
        self.notice_spin.valueChanged.connect(self._refresh_preview)

        self.connect_spin = StepSpinBox()
        self.connect_spin.setRange(1, MAX_CONNECT_TIMEOUT_SECONDS)
        self.connect_spin.setValue(DEFAULT_CONNECT_TIMEOUT_SECONDS)
        self.connect_spin.setSuffix(" s")
        self.connect_spin.setMinimumWidth(120)
        self.connect_spin.valueChanged.connect(self._refresh_preview)

        self.reason_combo = FluentComboBox()
        self.reason_combo.addItem(self.tr("Planejado"), ShutdownReasonKind.PLANNED.value)
        self.reason_combo.addItem(
            self.tr("Não planejado"), ShutdownReasonKind.UNPLANNED.value
        )
        self.reason_combo.setItemData(
            0,
            self.tr(
                "Rotina, manutenção ou atualização. "
                "Registra o evento como planejado (p:xx:yy)."
            ),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.reason_combo.setItemData(
            1,
            self.tr(
                "Incidente, falha ou emergência. "
                "Registra o evento como não planejado (u:xx:yy)."
            ),
            Qt.ItemDataRole.ToolTipRole,
        )
        self.reason_combo.setCurrentIndex(0)
        self.reason_combo.setMinimumWidth(140)
        self.reason_combo.currentIndexChanged.connect(self._on_reason_kind_changed)

        self.reason_detail_combo = FluentComboBox()
        self.reason_detail_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.reason_detail_combo.currentIndexChanged.connect(
            self._on_reason_detail_changed
        )

        self.reason_code_lbl = QLabel("")
        self.reason_code_lbl.setObjectName("energiaReasonCode")
        self.reason_code_lbl.setWordWrap(True)
        self.reason_code_lbl.setStyleSheet(
            f"QLabel#energiaReasonCode {{ color: {COLOR_TEXT_MUTED}; "
            f"font-size: {SIZE_UI_SMALL}pt; background: transparent; }}"
        )

        self.allow_abort_check = QCheckBox(
            self.tr("Permitir que o usuário remoto cancele a contagem (-c)")
        )
        self.allow_abort_check.setChecked(True)
        self.allow_abort_check.stateChanged.connect(self._refresh_preview)
        self.force_check = QCheckBox(
            self.tr("Forçar encerramento dos aplicativos (-f)")
        )
        self.force_check.setChecked(False)
        self.force_check.stateChanged.connect(self._refresh_preview)

        self.preview_label = QLabel()
        self.preview_label.setObjectName("energiaPreview")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.preview_label.setStyleSheet(
            f"QLabel#energiaPreview {{ color: {COLOR_TEXT_MUTED}; "
            f"font-size: {SIZE_UI_SMALL}pt; }}"
        )

        flags_row = QWidget()
        flags_lay = QHBoxLayout(flags_row)
        flags_lay.setContentsMargins(0, 0, 0, 0)
        flags_lay.setSpacing(SPACE_MD + 4)
        flags_lay.addWidget(self.allow_abort_check, 0)
        flags_lay.addWidget(self.force_check, 0)
        flags_lay.addStretch(1)

        card.content_layout.addWidget(
            _control_column(self.tr("Quando"), timing_wrap), 0
        )
        card.content_layout.addWidget(
            _control_column(self.tr("Mensagem"), self.message_edit), 0
        )
        card.content_layout.addWidget(
            _pair_row(
                _control_column(self.tr("Aviso (-v)"), self.notice_spin),
                _control_column(self.tr("Timeout (-n)"), self.connect_spin),
            ),
            0,
        )
        reason_block = QWidget()
        reason_lay = QVBoxLayout(reason_block)
        reason_lay.setContentsMargins(0, 0, 0, 0)
        reason_lay.setSpacing(2)
        reason_lay.addWidget(
            _pair_row(
                _control_column(self.tr("Motivo"), self.reason_combo),
                _control_column(self.tr("Detalhe"), self.reason_detail_combo),
                stretches=(2, 5),
            ),
            0,
        )
        reason_lay.addWidget(self.reason_code_lbl, 0)
        card.content_layout.addWidget(reason_block, 0)
        card.content_layout.addWidget(flags_row, 0)
        card.content_layout.addWidget(
            _control_column(self.tr("Comando"), self.preview_label), 0
        )
        self._fill_reason_detail(ShutdownReasonKind.PLANNED)
        self._sync_timing_value_widgets()
        self._apply_suggested_message(force=True)
        return card

    def _sync_timing_value_widgets(self) -> None:
        """Mostra só o valor compacto da opção ativa (contagem ou horário)."""
        if not hasattr(self, "countdown_spin"):
            return
        profile = action_profile(self._selected)
        supports = profile.supports_countdown
        busy = self._busy_now()
        mode = self._timing_mode()
        show_countdown = supports and mode == TimingMode.COUNTDOWN
        show_schedule = supports and mode == TimingMode.SCHEDULED
        self.countdown_spin.setVisible(show_countdown)
        self.time_edit.setVisible(show_schedule)
        self.countdown_spin.setEnabled(show_countdown and not busy)
        self.time_edit.setEnabled(show_schedule and not busy)

    def _reason_kind(self) -> ShutdownReasonKind:
        raw = self.reason_combo.currentData()
        try:
            return ShutdownReasonKind(str(raw))
        except ValueError:
            return ShutdownReasonKind.PLANNED

    def _fill_reason_detail(
        self,
        kind: ShutdownReasonKind,
        *,
        prefer_key: str = "",
    ) -> None:
        if not hasattr(self, "reason_detail_combo"):
            return
        options = reasons_for_kind(kind)
        current = prefer_key or (self.reason_detail_combo.currentData() or "")
        self.reason_detail_combo.blockSignals(True)
        self.reason_detail_combo.clear()
        for option in options:
            label = reason_option_label(option)
            self.reason_detail_combo.addItem(label, option.key)
            idx = self.reason_detail_combo.count() - 1
            self.reason_detail_combo.setItemData(
                idx,
                self.tr(f"Event Viewer: {option.token}"),
                Qt.ItemDataRole.ToolTipRole,
            )
        select = 0
        if current:
            found = self.reason_detail_combo.findData(current)
            if found >= 0:
                select = found
            else:
                # Mantém major:minor se existir no novo tipo (ex.: Outro 0:0).
                parts = str(current).split(":")
                if len(parts) == 3:
                    alt = f"{kind.value}:{parts[1]}:{parts[2]}"
                    found = self.reason_detail_combo.findData(alt)
                    if found >= 0:
                        select = found
        self.reason_detail_combo.setCurrentIndex(max(0, select))
        self.reason_detail_combo.blockSignals(False)
        self._update_reason_code_caption()

    def _on_reason_kind_changed(self, *_args) -> None:
        self._fill_reason_detail(self._reason_kind())
        self._apply_suggested_message()
        self._refresh_preview()

    def _on_reason_detail_changed(self, *_args) -> None:
        self._update_reason_code_caption()
        self._apply_suggested_message()
        self._refresh_preview()

    def _apply_suggested_message(self, *, force: bool = False) -> None:
        """Preenche a mensagem pronta do Detalhe sem apagar edição manual."""
        if not hasattr(self, "message_edit"):
            return
        option = self._selected_reason_option()
        suggested = suggested_reason_message(option)
        if not suggested:
            return
        current = (self.message_edit.text() or "").strip()
        previous = (self._auto_message or "").strip()
        if force or not current or current == previous:
            self.message_edit.blockSignals(True)
            self.message_edit.setText(suggested)
            self.message_edit.blockSignals(False)
            self._auto_message = suggested
            self._refresh_preview()

    def _selected_reason_option(self):
        key = self.reason_detail_combo.currentData() if hasattr(self, "reason_detail_combo") else None
        if not key:
            return None
        parts = str(key).split(":")
        if len(parts) != 3:
            return None
        try:
            kind = ShutdownReasonKind(parts[0])
            return find_reason_option(kind, int(parts[1]), int(parts[2]))
        except ValueError:
            return None

    def _update_reason_code_caption(self) -> None:
        if not hasattr(self, "reason_code_lbl"):
            return
        option = self._selected_reason_option()
        if option is None:
            self.reason_code_lbl.setText("")
            self.reason_code_lbl.setToolTip("")
            return
        kind_label = (
            self.tr("planejado")
            if option.kind == ShutdownReasonKind.PLANNED
            else self.tr("não planejado")
        )
        self.reason_code_lbl.setText(
            self.tr(f"Código Windows: {option.token}  ·  {kind_label}")
        )
        self.reason_code_lbl.setToolTip(
            self.tr(
                "Enviado ao PsShutdown como -e. "
                "Aparece no Visualizador de Eventos do host."
            )
        )

    def _build_output_card(self) -> CardWidget:
        card = CardWidget("\uE9F9", self.tr("Saída"))
        card.set_collapsible(True, collapsed=False)
        card.set_expanding(True)
        card.set_layout_stretch(3)

        self._output_tabs = QTabWidget()
        inner_bar = Mdl2TabBar(self._output_tabs)
        inner_bar.setExpanding(False)
        self._output_tabs.setTabBar(inner_bar)
        self._output_tabs.setDocumentMode(True)
        self._output_tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

        self.log_output = LogOutputWidget()
        self.log_output.set_collapsible(False)
        self.log_output.set_expanding(True)
        self.log_output.set_interactive(False)
        self._embed_inner_card(self.log_output)

        self._console_copy_btn = card.make_header_button(
            "\uE8C8",
            self.tr("Copiar seleção (ou tudo, se nada estiver selecionado)"),
        )
        self._console_copy_btn.clicked.connect(self.log_output.copyRequested.emit)
        card.add_header_button(self._console_copy_btn)
        self._console_clear_btn = card.make_header_button(
            "\uE74D", self.tr("Limpar este console (apenas na tela)")
        )
        self._console_clear_btn.clicked.connect(self.log_output.clear_log)
        card.add_header_button(self._console_clear_btn)

        history_page = QWidget()
        history_lay = QVBoxLayout(history_page)
        history_lay.setContentsMargins(0, SPACE_SM, 0, 0)
        history_lay.setSpacing(0)
        self.history_table = QTableWidget()
        self.history_table.setColumnCount(8)
        self.history_table.setHorizontalHeaderLabels(
            [
                self.tr("Data"),
                self.tr("Operador"),
                self.tr("Host"),
                self.tr("Ação"),
                self.tr("Motivo"),
                self.tr("Contagem"),
                self.tr("Envio"),
                self.tr("Verificação"),
            ]
        )
        configure_standard_table(self.history_table, stretch_columns=(3, 6, 7))
        self.history_table.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.history_table.setMinimumHeight(80)
        history_lay.addWidget(self.history_table, 1)

        idx_console = self._output_tabs.addTab(
            self.log_output, self.tr("Console de Saída")
        )
        inner_bar.set_tab_meta(idx_console, "\uE9F9")
        idx_history = self._output_tabs.addTab(
            history_page, self.tr("Histórico de energia")
        )
        inner_bar.set_tab_meta(idx_history, "\uE81C")
        self._output_tabs.setCurrentIndex(0)
        self._output_tabs.currentChanged.connect(self._on_output_tab_changed)
        self._on_output_tab_changed(0)
        card.content_layout.addWidget(self._output_tabs, 1)
        return card

    def _on_output_tab_changed(self, index: int) -> None:
        on_console = int(index) == 0
        if hasattr(self, "_console_copy_btn"):
            self._console_copy_btn.setVisible(on_console)
            self._console_clear_btn.setVisible(on_console)

    @staticmethod
    def _embed_inner_card(card: CardWidget) -> None:
        """Remove borda/cabeçalho do card embutido numa aba interna."""
        card.set_collapsible(False)
        card._header_widget.hide()
        card._divider.hide()
        card._set_divider_spacing_visible(False)
        card._container.setStyleSheet(
            "QWidget#cardContainer { background: transparent; border: none; }"
        )
        card._container_layout.setContentsMargins(0, 0, 0, 0)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        card._container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def _apply_host_status(self, *, online: bool) -> None:
        host = self._get_host()
        _set_fact_value(self.host_label, host or _EMPTY_FACT, muted=not host)
        if not host:
            state, text = "idle", self.tr("Aguardando host")
        elif not is_valid_host(host):
            state, text = "invalid", self.tr("Host inválido")
        elif online:
            state, text = "online", self.tr("Online")
        else:
            state, text = "offline", self.tr("Offline")
        self.host_status_dot.set_state(state)
        _set_fact_value(self.host_status_label, text, muted=state in ("idle", "invalid"))
        tool_ok = psshutdown_available()
        _set_fact_value(
            self.method_label,
            self.tr("PsShutdown") if tool_ok else self.tr("PsShutdown ausente"),
            muted=not tool_ok,
        )
        if not host:
            self._apply_user_fact()

    def _on_action_selected(self, action: PowerAction) -> None:
        self._selected = action
        self._apply_action_profile(action, reset_timing=True)
        self._refresh_preview()
        self._refresh_actions()

    def _apply_action_profile(self, action: PowerAction, *, reset_timing: bool) -> None:
        profile = action_profile(action)
        self.action_hint.setText(self.tr(profile.hint))
        if reset_timing:
            if profile.default_timing == TimingMode.IMMEDIATE:
                self.radio_immediate.setChecked(True)
            elif profile.default_timing == TimingMode.SCHEDULED:
                self.radio_scheduled.setChecked(True)
            else:
                self.radio_countdown.setChecked(True)
            self.countdown_spin.setValue(int(profile.default_countdown or 0))
            self.allow_abort_check.setChecked(bool(profile.default_allow_abort))
            self.force_check.setChecked(False)
        self._on_timing_changed()

    def _timing_mode(self) -> TimingMode:
        if self.radio_scheduled.isChecked():
            return TimingMode.SCHEDULED
        if self.radio_immediate.isChecked():
            return TimingMode.IMMEDIATE
        return TimingMode.COUNTDOWN

    def _on_timing_changed(self, *_args) -> None:
        profile = action_profile(self._selected)
        supports = profile.supports_countdown
        busy = self._busy_now()
        self.radio_immediate.setEnabled(supports and not busy)
        self.radio_countdown.setEnabled(supports and not busy)
        self.radio_scheduled.setEnabled(supports and not busy)
        if not supports and not self.radio_immediate.isChecked():
            self.radio_immediate.blockSignals(True)
            self.radio_immediate.setChecked(True)
            self.radio_immediate.blockSignals(False)
        mode = self._timing_mode()
        self._sync_timing_value_widgets()
        self.allow_abort_check.setEnabled(
            profile.supports_user_abort and mode != TimingMode.IMMEDIATE and not busy
        )
        if mode == TimingMode.IMMEDIATE:
            self.allow_abort_check.blockSignals(True)
            self.allow_abort_check.setChecked(False)
            self.allow_abort_check.blockSignals(False)
        self._refresh_preview()

    def _busy_now(self) -> bool:
        return bool(
            self._busy
            or (self._power_worker is not None and self._power_worker.isRunning())
        )

    def _draft_request(self, *, action: Optional[PowerAction] = None) -> PowerRequest:
        chosen = action or self._selected
        profile = action_profile(chosen)
        notice = int(self.notice_spin.value())
        reason = self._reason_kind()
        major = 0
        minor = 0
        option = self._selected_reason_option()
        if option is not None:
            reason = option.kind
            major = option.major
            minor = option.minor
        if not profile.supports_reason:
            reason = ShutdownReasonKind.NONE
            major = 0
            minor = 0
        time_val = self.time_edit.time()
        return apply_action_defaults(
            PowerRequest(
                host=self._get_host(),
                action=chosen,
                timing=self._timing_mode(),
                countdown_seconds=int(self.countdown_spin.value()),
                scheduled_hour=int(time_val.hour()),
                scheduled_minute=int(time_val.minute()),
                message=self.message_edit.text(),
                notice_seconds=None if notice <= 0 else notice,
                allow_user_abort=self.allow_abort_check.isChecked(),
                force=self.force_check.isChecked(),
                connect_timeout=int(self.connect_spin.value()),
                reason=reason,
                reason_major=major,
                reason_minor=minor,
            )
        )

    def _refresh_preview(self, *_args) -> None:
        if not hasattr(self, "preview_label"):
            return
        request = self._draft_request()
        exe = resolve_psshutdown_exe(get_pstools_dir())
        user, password = self._creds()
        argv = build_psshutdown_argv(
            exe, request, user=user, password=password, include_password=True
        )
        if not argv:
            errors = validate_power_request(request)
            self.preview_label.setText(
                errors[0] if errors else self.tr("Comando indisponível.")
            )
            return
        self.preview_label.setText(preview_psshutdown_argv(argv))

    def _refresh_actions(self) -> None:
        if not hasattr(self, "run_btn"):
            return
        busy = self._busy_now()
        online = self._is_online()
        host = self._get_host()
        tool_ok = psshutdown_available()
        can_run = (
            (not busy)
            and online
            and bool(host)
            and is_valid_host(host)
            and tool_ok
        )
        self.run_btn.setEnabled(can_run)
        self.stop_btn.setEnabled(busy)
        self.abort_btn.setEnabled(
            tool_ok and bool(host) and is_valid_host(host) and online
        )
        self.refresh_sessions_btn.setEnabled(not busy and online and bool(host))
        for btn in self._action_buttons.values():
            btn.setEnabled(not busy)
        self.message_edit.setEnabled(not busy)
        self.notice_spin.setEnabled(not busy)
        self.connect_spin.setEnabled(not busy)
        profile = action_profile(self._selected)
        supports_reason = not busy and profile.supports_reason
        self.reason_combo.setEnabled(supports_reason)
        self.reason_detail_combo.setEnabled(supports_reason)
        self.reason_code_lbl.setEnabled(supports_reason)
        if not supports_reason:
            self.reason_code_lbl.setText("")
        else:
            self._update_reason_code_caption()
        self.force_check.setEnabled(not busy and profile.supports_force)
        supports = profile.supports_countdown
        self.radio_immediate.setEnabled(supports and not busy)
        self.radio_countdown.setEnabled(supports and not busy)
        self.radio_scheduled.setEnabled(supports and not busy)
        self._sync_timing_value_widgets()
        self.allow_abort_check.setEnabled(
            profile.supports_user_abort
            and self._timing_mode() != TimingMode.IMMEDIATE
            and not busy
        )
        if not tool_ok:
            self.run_btn.setToolTip(
                self.tr("PsShutdown não encontrado na pasta PSTools configurada.")
            )
        else:
            self.run_btn.setToolTip(self.tr("Executar ação de energia"))

    def refresh_sessions(self) -> None:
        host = self._get_host()
        if self._closing or not host or not is_valid_host(host) or not self._is_online():
            return
        worker = self._session_worker
        if worker is not None and worker.isRunning():
            return
        user, password = self._creds()
        self._apply_user_fact(loading=True)
        self.refresh_sessions_btn.setEnabled(False)
        self._session_worker = _SessionWorker(
            host, user=user, password=password, parent=self
        )
        self._session_worker.result.connect(self._on_sessions)
        self._session_worker.finished.connect(self._on_sessions_finished)
        self._session_worker.start()

    def _invalidate_sessions(self) -> None:
        self._sessions = []
        self._sessions_host = ""
        self._apply_user_fact()

    def _active_user_names(self) -> list[str]:
        users: list[str] = []
        seen: set[str] = set()
        for session in active_sessions(self._sessions):
            name = (session_account(session) or session.username or "").strip()
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            users.append(name)
        return users

    def _apply_user_fact(self, *, loading: bool = False, error: str = "") -> None:
        if not hasattr(self, "user_label"):
            return
        if loading:
            _set_fact_value(
                self.user_label, self.tr("Consultando sessões…"), muted=True
            )
            self.user_status_dot.set_state("checking")
            return
        if not self._get_host():
            _set_fact_value(self.user_label, _EMPTY_FACT, muted=True)
            self.user_status_dot.set_state("idle")
            return
        if error and not self._sessions:
            _set_fact_value(self.user_label, error, muted=True)
            self.user_status_dot.set_state("err")
            return
        users = self._active_user_names()
        if not users:
            _set_fact_value(
                self.user_label, self.tr("Nenhum usuário conectado"), muted=True
            )
            self.user_status_dot.set_state("idle")
            return
        _set_fact_value(self.user_label, "  ·  ".join(users))
        self.user_status_dot.set_state("ok")

    def _on_sessions(self, host: str, sessions: object, error: str) -> None:
        if not self._ui_alive():
            return
        if host.casefold() != self._get_host().casefold():
            return
        rows = [item for item in list(sessions or []) if isinstance(item, RemoteSession)]
        self._sessions = rows
        self._sessions_host = host
        self._apply_user_fact(error=error or "")
        if error and not rows:
            self.log_output.append_log(self.tr(f"[ENERGIA] {error}"))
            return
        users = self._active_user_names()
        if users:
            self.log_output.append_log(
                self.tr(f"[ENERGIA] Usuário ativo: {'  ·  '.join(users)}.")
            )

    def _on_sessions_finished(self) -> None:
        worker = self._session_worker
        self._session_worker = None
        if worker is not None:
            worker.deleteLater()
        if self._ui_alive():
            self._refresh_actions()

    def _on_run_clicked(self) -> None:
        if self._busy_now():
            return
        request = self._draft_request()
        errors = validate_power_request(request)
        if errors:
            QMessageBox.warning(self, self.tr("Energia"), errors[0])
            return
        if not self._confirm_request(request):
            return
        self._start_power(
            request, follow_up=action_profile(request.action).expects_offline
        )

    def _confirm_request(self, request: PowerRequest) -> bool:
        note = sessions_warning(self._sessions)
        if request.action == PowerAction.LOGOFF:
            extra = action_profile(request.action).hint
            note = f"{note}\n{extra}".strip() if note else extra
        body = confirmation_summary(request, sessions_note=note)
        reply = QMessageBox.question(
            self,
            self.tr("Confirmar ação de energia"),
            body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        if request.force:
            second = QMessageBox.question(
                self,
                self.tr("Forçar encerramento"),
                self.tr(
                    "Forçar encerramento está marcado (-f).\n"
                    "Aplicativos remotos serão fechados sem salvar.\n\n"
                    "Confirmar mesmo assim?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if second != QMessageBox.StandardButton.Yes:
                return False
        return True

    def _start_power(self, request: PowerRequest, *, follow_up: bool) -> None:
        if self._busy_now():
            return
        host = self._get_host()
        user, password = self._creds()
        self._busy = True
        self._refresh_actions()
        self.log_output.append_log(
            self.tr(f"[ENERGIA] {action_profile(request.action).label} em {host}…")
        )
        worker = _PowerWorker(
            request,
            user=user,
            password=password,
            current_host=host,
            follow_up=follow_up,
            parent=self,
        )
        self._power_worker = worker
        worker.progress.connect(self._on_progress)
        worker.finished_result.connect(self._on_power_result)
        worker.finished_err.connect(self._on_power_err)
        worker.finished.connect(self._on_power_finished)
        worker.start()

    def stop_local(self) -> None:
        self._abort_after_stop = False
        worker = self._power_worker
        if worker is None or not worker.isRunning():
            return
        worker.cancel()
        self.log_output.append_log(
            self.tr(
                "[ENERGIA] Parando o processo local. "
                "Se o host já aceitou a ação, use Cancelar ação agendada (-a)."
            )
        )

    def request_remote_abort(self) -> None:
        if not psshutdown_available():
            QMessageBox.warning(
                self,
                self.tr("Energia"),
                self.tr("PsShutdown não encontrado na pasta PSTools configurada."),
            )
            return
        host = self._get_host()
        if not host or not is_valid_host(host):
            QMessageBox.warning(
                self,
                self.tr("Energia"),
                self.tr("Informe um host remoto na aba PsExec."),
            )
            return
        if self._busy_now():
            self._abort_after_stop = True
            worker = self._power_worker
            if worker is not None and worker.isRunning():
                worker.cancel()
            self.log_output.append_log(
                self.tr(
                    "[ENERGIA] Interrompendo o acompanhamento local para enviar -a…"
                )
            )
            return
        abort = apply_action_defaults(
            PowerRequest(host=host, action=PowerAction.ABORT, message="")
        )
        self._start_power(abort, follow_up=False)

    def _on_progress(self, message: str) -> None:
        if not self._ui_alive() or not message:
            return
        _set_fact_value(self.host_status_label, message, muted=False)
        self.log_output.append_log(self.tr(f"[ENERGIA] {message}"))

    def _on_power_result(self, result: object) -> None:
        if not self._ui_alive() or not isinstance(result, PowerResult):
            return
        if result.stale or result.phase == PowerPhase.STALE_HOST:
            self.log_output.append_log(
                self.tr(
                    "[ENERGIA] Resultado ignorado: o host mudou durante a operação."
                )
            )
            return
        safe_out = redact_command_text(result.output or "")
        self.log_output.append_log(
            self.tr(
                f"[ENERGIA] {result.classification.title}: {result.classification.detail}"
            )
        )
        if result.argv_preview:
            self.log_output.append_log(self.tr(f"[ENERGIA] {result.argv_preview}"))
        if safe_out:
            self.log_output.append_log(safe_out)
        self._append_history(result)
        if result.action == PowerAction.LOGOFF and result.ok:
            self.refresh_sessions()
        state = "ok" if result.ok else "err"
        if result.phase in (PowerPhase.ACTION_PENDING, PowerPhase.UNCONFIRMED):
            state = "warn"
        self.host_status_dot.set_state(state)
        _set_fact_value(self.host_status_label, result.classification.title, muted=False)

    def _on_power_err(self, message: str) -> None:
        if not self._ui_alive():
            return
        safe = redact_command_text(message or "")
        self.log_output.append_log(self.tr(f"[ENERGIA] {safe}"))
        self.host_status_dot.set_state("err")
        _set_fact_value(self.host_status_label, self.tr("Falha"), muted=False)

    def _on_power_finished(self) -> None:
        worker = self._power_worker
        self._power_worker = None
        self._busy = False
        if worker is not None:
            worker.deleteLater()
        if not self._ui_alive():
            return
        pending_abort = self._abort_after_stop
        self._abort_after_stop = False
        self._refresh_actions()
        if pending_abort:
            abort = apply_action_defaults(
                PowerRequest(
                    host=self._get_host(), action=PowerAction.ABORT, message=""
                )
            )
            self._start_power(abort, follow_up=False)

    def _append_history(self, result: PowerResult) -> None:
        drafted = self._draft_request(action=result.action)
        if drafted.timing == TimingMode.COUNTDOWN:
            countdown = f"{drafted.countdown_seconds} s"
        elif drafted.timing == TimingMode.SCHEDULED:
            countdown = f"{drafted.scheduled_hour}:{drafted.scheduled_minute:02d}"
        else:
            countdown = "imediato"
        row = {
            "when": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            "operator": current_operator() or "—",
            "host": result.host,
            "action": action_profile(result.action).label,
            "reason": reason_display(drafted),
            "countdown": countdown,
            "send": result.classification.title,
            "followup": (
                result.followup_phase.value if result.followup_phase else "—"
            ),
        }
        self._history.insert(0, row)
        self.history_table.setRowCount(len(self._history))
        for i, item in enumerate(self._history):
            values = [
                item["when"],
                item["operator"],
                item["host"],
                item["action"],
                item["reason"],
                item["countdown"],
                item["send"],
                item["followup"],
            ]
            for col, text in enumerate(values):
                self.history_table.setItem(i, col, SortableTableItem(text))

