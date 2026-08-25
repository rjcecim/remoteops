"""Aba Mensagem — envio via ``msg.exe`` no host remoto (PsExec).

Pré-visualização compartilhada permanece na aba PsExec.
O console desta aba é exclusivo (não reutiliza o ConPTY do PsExec).
"""

from __future__ import annotations

from typing import Callable, Optional, Tuple

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QFont, QKeySequence, QTextCursor
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QRadioButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from remoteops.core.executor import Executor
from remoteops.core.models import ExecutionResult
from remoteops.services.messaging import (
    DEFAULT_DISPLAY_SECONDS,
    MAX_DISPLAY_SECONDS,
    MIN_DISPLAY_SECONDS,
    MSG_MAX_LENGTH,
    RECIPIENT_ALL,
    MessageRequest,
    MessageResultKind,
    MessageService,
    history_detail,
    normalize_message,
    psexec_executable_available,
)
from remoteops.services.msg_style import (
    STYLE_BOLD,
    STYLE_ITALIC,
    STYLE_NONE,
    STYLE_UNDERLINE,
    apply_message_style,
    clip_message_text,
    inspect_message_style,
    msg_unit_count,
)
from remoteops.services.ops import CredentialContext
from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_ACCENT_SOFT,
    COLOR_BORDER,
    COLOR_HOVER,
    COLOR_PRESSED,
    COLOR_SURFACE_MUTED,
    COLOR_TEXT,
    COLOR_TEXT_MUTED,
    HEADER_BTN_SIZE,
    RADIUS_MEDIUM,
    SIZE_UI_SMALL,
)
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    grid_in_card,
    make_card_stack,
)
from remoteops.ui.widgets.combobox import FluentComboBox
from remoteops.ui.widgets.log import LogOutputWidget
from remoteops.ui.widgets.spinbox import StepSpinBox
from remoteops.ui.widgets.status_dot import STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot
from remoteops.utils.app_logging import log_operation
from remoteops.utils.ping import is_valid_host, normalize_host
from remoteops.utils.pstools import get_pstools_dir
from remoteops.utils.sessions import RemoteSession, list_remote_sessions


class _SessionListWorker(QThread):
    """Lista sessões WTS em background; o callback descarta hosts antigos."""

    result = pyqtSignal(str, object, str)

    def __init__(self, host: str, user: str = "", password: str = "", parent=None):
        super().__init__(parent)
        self._host = host
        self._user = user
        self._password = password

    def run(self) -> None:
        try:
            sessions, error = list_remote_sessions(
                self._host, user=self._user, password=self._password
            )
            self.result.emit(self._host, sessions, error)
        finally:
            self._password = ""


class MessageTab(QWidget):
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
        self._service = MessageService()
        # Executor dedicado e preguiçoso: não interrompe o ConPTY do PsExec.
        self._executor_inst: Optional[Executor] = None
        self._session_worker: Optional[_SessionListWorker] = None
        self._sessions_host = ""
        self._sessions: list[RemoteSession] = []
        self._sending = False
        self._send_generation = 0
        self._active_generation = 0
        self._send_host = ""
        self._last_request: Optional[MessageRequest] = None
        self._send_passwords: list[str] = []
        self._closing = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root = make_card_stack(self)

        dest = self._build_destination_card()
        recip = self._build_recipient_card()
        self.message_card = self._build_message_card()
        root.addWidget(dest)
        root.addWidget(recip)
        root.addWidget(self.message_card, 5)
        self.log_output = LogOutputWidget()
        self.log_output.set_collapsible(True, collapsed=False)
        self.log_output.set_expanding(True)
        self.log_output.set_layout_stretch(3)
        self.log_output.set_interactive(False)
        root.addWidget(self.log_output, 3)
        self.message_card.collapsedChanged.connect(self._redistribute_expandable_space)
        self.log_output.collapsedChanged.connect(self._redistribute_expandable_space)
        self._redistribute_expandable_space()

        if self._host_source is not None:
            self._host_source.textChanged.connect(self.sync_from_host)
        self.sync_from_host()
        self._refresh_actions()

    def _build_destination_card(self) -> CardWidget:
        card = CardWidget("\uEA18", self.tr("Destino"))
        card.set_collapsible(True, collapsed=False)
        g = grid_in_card(card)

        self.host_label = QLabel("—")
        self.host_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.host_label.setWordWrap(True)

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.host_status_dot = StatusDot()
        self.host_status_label = QLabel(self.tr("Aguardando host"))
        self.host_status_label.setObjectName("messageHostStatus")
        self.host_status_label.setStyleSheet(
            f"QLabel#messageHostStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        status_row.addWidget(self.host_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.host_status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch()
        status_wrap = QWidget()
        status_wrap.setLayout(status_row)

        self.method_label = QLabel(self.tr("PsExec"))
        self.method_label.setToolTip(
            self.tr(
                "Executa msg.exe no host remoto via PsExec.\n"
                "Aproveita a pasta PSTools e as credenciais da aba PsExec."
            )
        )
        self.method_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        add_row(g, 0, self.tr("Host"), self.host_label)
        add_row(g, 1, self.tr("Estado"), status_wrap)
        add_row(g, 2, self.tr("Método"), self.method_label)
        return card

    def _build_recipient_card(self) -> CardWidget:
        card = CardWidget("\uE716", self.tr("Destinatário"))
        card.set_collapsible(True, collapsed=False)
        self.refresh_sessions_btn = card.make_header_button(
            "\uE72C",
            self.tr("Atualizar sessões do host atual"),
        )
        self.refresh_sessions_btn.clicked.connect(self.refresh_sessions)
        card.add_header_button(self.refresh_sessions_btn)

        g = grid_in_card(card)
        self.radio_all = QRadioButton(self.tr("Todos os usuários conectados"))
        self.radio_all.setToolTip(self.tr("Envia para * — todas as sessões do host."))
        self.radio_specific = QRadioButton(self.tr("Usuário ou sessão específica"))
        self.radio_specific.setToolTip(
            self.tr("Envia para o ID numérico da sessão selecionada.")
        )
        self.radio_all.setChecked(True)
        self._recipient_group = QButtonGroup(self)
        self._recipient_group.addButton(self.radio_all)
        self._recipient_group.addButton(self.radio_specific)
        self.radio_all.toggled.connect(self._on_recipient_mode_changed)

        mode_col = QWidget()
        mode_lay = QHBoxLayout(mode_col)
        mode_lay.setContentsMargins(0, 0, 0, 0)
        mode_lay.setSpacing(16)
        mode_lay.addWidget(self.radio_all)
        mode_lay.addWidget(self.radio_specific)
        mode_lay.addStretch()

        self.session_combo = FluentComboBox()
        self.session_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.session_combo.setEnabled(False)
        self.session_combo.setToolTip(
            self.tr("Usuário, nome da sessão, ID e estado. O envio usa o ID numérico.")
        )
        self.session_combo.currentIndexChanged.connect(self._refresh_actions)
        self._reset_session_combo()

        add_row(g, 0, self.tr("Modalidade"), mode_col)
        add_row(g, 1, self.tr("Sessão"), self.session_combo)
        return card

    def _build_message_card(self) -> CardWidget:
        card = CardWidget("\uE8BD", self.tr("Mensagem"))
        card.set_collapsible(True, collapsed=False)
        self.send_btn = card.make_header_button(
            "\uE724",
            self.tr("Enviar mensagem"),
        )
        self.send_btn.clicked.connect(self.send_message)
        card.add_header_button(self.send_btn)
        self.clear_btn = card.make_header_button(
            "\uE75C",
            self.tr("Limpar o texto da mensagem"),
        )
        self.clear_btn.clicked.connect(self.clear_message)
        card.add_header_button(self.clear_btn)

        composer = QWidget()
        composer.setObjectName("messageComposer")
        composer.setStyleSheet(
            f"""
            QWidget#messageComposer {{
                border: 1px solid {COLOR_BORDER};
                border-radius: {RADIUS_MEDIUM}px;
                background: {COLOR_SURFACE_MUTED};
            }}
            QWidget#messageComposer:focus-within {{
                border-color: {COLOR_ACCENT};
            }}
            QWidget#messageComposer QPlainTextEdit {{
                border: none;
                background: transparent;
                border-radius: 0px;
                padding: 6px 8px;
                color: {COLOR_TEXT};
            }}
            QWidget#messageComposer QPlainTextEdit:focus {{
                border: none;
            }}
            QWidget#messageFormatBar {{
                background: transparent;
                border: none;
                border-bottom: 1px solid {COLOR_BORDER};
            }}
            QToolButton#messageFormatBtn {{
                border: none;
                background: transparent;
                color: {COLOR_ACCENT};
            }}
            QToolButton#messageFormatBtn:hover {{
                background: {COLOR_HOVER};
                border-radius: 4px;
            }}
            QToolButton#messageFormatBtn:pressed {{
                background: {COLOR_PRESSED};
            }}
            QToolButton#messageFormatBtn:checked {{
                background: {COLOR_ACCENT_SOFT};
                border-radius: 4px;
            }}
            QToolButton#messageFormatBtn:disabled {{
                color: {COLOR_TEXT_MUTED};
                background: transparent;
            }}
            """
        )
        composer_lay = QVBoxLayout(composer)
        composer_lay.setContentsMargins(0, 0, 0, 0)
        composer_lay.setSpacing(0)
        composer_lay.addWidget(self._build_format_toolbar(), 0)

        self.message_edit = QPlainTextEdit()
        self.message_edit.setObjectName("messageEdit")
        self.message_edit.setPlaceholderText(
            self.tr("Texto exibido no host remoto (até 255 caracteres).")
        )
        self.message_edit.setToolTip(
            self.tr(
                f"Limite do msg.exe: {MSG_MAX_LENGTH} caracteres (UTF-16). "
                "Negrito e itálico usam Unicode visível no diálogo remoto. "
                "Quebras de linha da interface são normalizadas."
            )
        )
        self.message_edit.setMinimumHeight(90)
        self.message_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.message_edit.setTabChangesFocus(True)
        self.message_edit.textChanged.connect(self._on_message_changed)
        self.message_edit.selectionChanged.connect(self._sync_format_buttons)
        self.message_edit.cursorPositionChanged.connect(self._sync_format_buttons)
        self._install_format_shortcuts()
        composer_lay.addWidget(self.message_edit, 1)
        composer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        card.content_layout.addWidget(composer, 1)

        counter_row = QHBoxLayout()
        counter_row.setContentsMargins(0, 0, 0, 0)
        self.counter_label = QLabel(f"0 / {MSG_MAX_LENGTH}")
        self.counter_label.setObjectName("messageCounter")
        self.counter_label.setStyleSheet(
            f"QLabel#messageCounter {{ color: {COLOR_TEXT_MUTED}; font-size: {SIZE_UI_SMALL}pt; }}"
        )
        counter_row.addStretch()
        counter_row.addWidget(self.counter_label)
        counter_wrap = QWidget()
        counter_wrap.setLayout(counter_row)
        card.content_layout.addWidget(counter_wrap)

        g = grid_in_card(card)
        time_row = QHBoxLayout()
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(8)
        self.time_check = QCheckBox(self.tr("Definir tempo de exibição"))
        self.time_check.setToolTip(
            self.tr("Inclui /TIME:segundos. Desmarcado omite /TIME. Não usa /W.")
        )
        self.time_spin = StepSpinBox()
        self.time_spin.setRange(MIN_DISPLAY_SECONDS, MAX_DISPLAY_SECONDS)
        self.time_spin.setValue(DEFAULT_DISPLAY_SECONDS)
        self.time_spin.setEnabled(False)
        self.time_spin.setSuffix(self.tr(" s"))
        self.time_spin.setToolTip(
            self.tr(f"{MIN_DISPLAY_SECONDS}–{MAX_DISPLAY_SECONDS} segundos (padrão 30).")
        )
        self.time_check.toggled.connect(self.time_spin.setEnabled)
        time_row.addWidget(self.time_check)
        time_row.addWidget(self.time_spin)
        time_row.addStretch()
        time_wrap = QWidget()
        time_wrap.setLayout(time_row)

        self.verbose_check = QCheckBox(self.tr("Detalhado (/V)"))
        self.verbose_check.setChecked(False)
        self.verbose_check.setToolTip(
            self.tr("Inclui /V no msg.exe. Desmarcado por padrão.")
        )

        add_row(g, 0, self.tr("Exibição"), time_wrap)
        add_row(g, 1, self.tr("Avançado"), self.verbose_check)
        card.set_expanding(True)
        card.set_layout_stretch(5)
        return card

    def _redistribute_expandable_space(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is None:
            return
        open_cards = []
        for w, stretch in ((self.message_card, 5), (self.log_output, 3)):
            idx = lay.indexOf(w)
            if idx < 0:
                continue
            if w.is_collapsed:
                lay.setStretch(idx, 0)
            else:
                open_cards.append(w)
                w.set_layout_stretch(stretch)
                lay.setStretch(idx, stretch)
        need_tail = len(open_cards) == 0
        if need_tail:
            if getattr(self, "_bottom_stretch_idx", None) is None:
                lay.addStretch(1)
                self._bottom_stretch_idx = lay.count() - 1
            else:
                lay.setStretch(self._bottom_stretch_idx, 1)
        elif getattr(self, "_bottom_stretch_idx", None) is not None:
            lay.setStretch(self._bottom_stretch_idx, 0)
        lay.activate()
        self.updateGeometry()

    # ── host / estado ────────────────────────────────────────────────────────

    @property
    def _executor(self) -> Executor:
        if self._executor_inst is None:
            self._executor_inst = Executor(self)
            self._executor_inst.resultReady.connect(self._on_send_result)
        return self._executor_inst

    def _get_host(self) -> str:
        if self._host_source is None:
            return ""
        try:
            return normalize_host(self._host_source.text())
        except RuntimeError:
            return ""

    def _is_online(self) -> bool:
        if self._online_provider is None:
            return False
        try:
            return bool(self._online_provider())
        except Exception:
            return False

    def current_recipient(self) -> Optional[str]:
        if self.radio_all.isChecked():
            return RECIPIENT_ALL
        data = self.session_combo.currentData()
        if isinstance(data, int):
            return str(data)
        return None

    def set_host_online(self, online: bool) -> None:
        self._apply_host_status(online=bool(online))
        if not online:
            self._invalidate_sessions()
        self._refresh_actions()

    def sync_from_host(self, *_args) -> None:
        host = self._get_host()
        self.host_label.setText(host or "—")
        if host.casefold() != (self._sessions_host or "").casefold():
            self._invalidate_sessions()
        self._apply_host_status(online=self._is_online())
        self._refresh_actions()

    def _apply_host_status(self, *, online: bool) -> None:
        host = self._get_host()
        if not host:
            state, text = "idle", self.tr("Aguardando host")
        elif not is_valid_host(host):
            state, text = "invalid", self.tr("Host inválido")
        elif online:
            state, text = "online", self.tr("Online")
        else:
            state, text = "offline", self.tr("Offline")
        self.host_status_dot.set_color(STATUS_COLORS.get(state, STATUS_COLORS["idle"]))
        self.host_status_label.setText(text)

    def _invalidate_sessions(self) -> None:
        self._sessions_host = ""
        self._sessions = []
        self._reset_session_combo()

    def _reset_session_combo(self) -> None:
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("Nenhuma sessão selecionada"), None)
        self.session_combo.setCurrentIndex(0)
        self.session_combo.blockSignals(False)

    # ── sessões ──────────────────────────────────────────────────────────────

    def _on_recipient_mode_changed(self, *_args) -> None:
        specific = self.radio_specific.isChecked()
        self.session_combo.setEnabled(specific and not self._sending)
        if specific and not self._sessions:
            self.refresh_sessions()
        self._refresh_actions()

    def refresh_sessions(self) -> None:
        host = self._get_host()
        if self._closing or not host or not is_valid_host(host) or not self._is_online():
            return
        worker = self._session_worker
        if worker is not None and worker.isRunning():
            return
        user, password = self._creds()
        self._set_op_status("checking", self.tr("Carregando sessões…"))
        self.refresh_sessions_btn.setEnabled(False)
        self._session_worker = _SessionListWorker(
            host, user=user, password=password, parent=self
        )
        self._session_worker.result.connect(self._on_sessions_result)
        self._session_worker.finished.connect(self._on_session_worker_finished)
        self._session_worker.start()

    def _on_sessions_result(self, host: str, sessions, error: str) -> None:
        if self._closing:
            return
        current = self._get_host()
        if host.casefold() != current.casefold():
            return
        selected = self.current_recipient()
        valid: list[RemoteSession] = []
        for item in sessions or []:
            if isinstance(item, RemoteSession):
                valid.append(item)
        self._sessions = valid
        self._sessions_host = host
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("Nenhuma sessão selecionada"), None)
        for item in valid:
            label = (
                f"{item.username or '—'} — {item.name or '—'} — "
                f"ID {item.session_id} — {item.state or '—'}"
            )
            self.session_combo.addItem(label, int(item.session_id))
        index = 0
        if selected and selected != RECIPIENT_ALL:
            try:
                found = self.session_combo.findData(int(selected))
            except ValueError:
                found = -1
            if found >= 0:
                index = found
        self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)
        if error and not valid:
            self._set_op_status("err", error)
        elif not valid:
            self._set_op_status(
                "warn", self.tr("Nenhuma sessão encontrada neste host.")
            )
        else:
            self._set_op_status(
                "ok", self.tr(f"{len(valid)} sessão(ões) carregada(s).")
            )
        self._refresh_actions()

    def _on_session_worker_finished(self) -> None:
        worker = self._session_worker
        finished_host = getattr(worker, "_host", "") if worker is not None else ""
        self._session_worker = None
        if worker is not None:
            worker.deleteLater()
        self.refresh_sessions_btn.setEnabled(not self._sending)
        current = self._get_host()
        if (
            not self._closing
            and current
            and is_valid_host(current)
            and current.casefold() != (finished_host or "").casefold()
            and self.radio_specific.isChecked()
        ):
            self.refresh_sessions()

    # ── mensagem / ações ─────────────────────────────────────────────────────

    def _build_format_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("messageFormatBar")
        bar.setToolTip(
            self.tr(
                "O msg.exe não aceita RTF. Negrito e itálico viram Unicode "
                "no diálogo remoto; sublinhado usa caractere combinante."
            )
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(4, 2, 4, 2)
        lay.setSpacing(2)
        self.bold_btn = self._make_format_button(
            "\uE8DD",
            self.tr("Negrito (Ctrl+B)"),
            checkable=True,
        )
        self.italic_btn = self._make_format_button(
            "\uE8DB",
            self.tr("Itálico (Ctrl+I)"),
            checkable=True,
        )
        self.underline_btn = self._make_format_button(
            "\uE8DC",
            self.tr("Sublinhado (Ctrl+U)"),
            checkable=True,
        )
        self.clear_format_btn = self._make_format_button(
            "\uE894",
            self.tr("Remover formatação"),
            checkable=False,
        )
        self.bold_btn.clicked.connect(lambda: self._apply_message_style(STYLE_BOLD))
        self.italic_btn.clicked.connect(lambda: self._apply_message_style(STYLE_ITALIC))
        self.underline_btn.clicked.connect(
            lambda: self._apply_message_style(STYLE_UNDERLINE)
        )
        self.clear_format_btn.clicked.connect(
            lambda: self._apply_message_style(STYLE_NONE)
        )
        lay.addWidget(self.bold_btn)
        lay.addWidget(self.italic_btn)
        lay.addWidget(self.underline_btn)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setStyleSheet(f"color: {COLOR_BORDER};")
        sep.setFixedHeight(HEADER_BTN_SIZE)
        lay.addWidget(sep)
        lay.addWidget(self.clear_format_btn)
        lay.addStretch()
        return bar

    def _make_format_button(
        self, icon_char: str, tooltip: str, *, checkable: bool
    ) -> QToolButton:
        btn = QToolButton()
        btn.setObjectName("messageFormatBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.setAutoRaise(True)
        btn.setCheckable(checkable)
        btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        btn.setFont(QFont("Segoe MDL2 Assets", 10))
        btn.setText(icon_char)
        btn.setToolTip(tooltip)
        return btn

    def _install_format_shortcuts(self) -> None:
        for sequence, style in (
            ("Ctrl+B", STYLE_BOLD),
            ("Ctrl+I", STYLE_ITALIC),
            ("Ctrl+U", STYLE_UNDERLINE),
        ):
            action = QAction(self.message_edit)
            action.setShortcut(QKeySequence(sequence))
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda *_args, s=style: self._apply_message_style(s))
            self.message_edit.addAction(action)

    def _style_target_text(self) -> tuple[QTextCursor, str, int]:
        cursor = self.message_edit.textCursor()
        selected = (
            cursor.selectedText()
            .replace("\u2029", "\n")
            .replace("\u2028", "\n")
        )
        if selected:
            start = min(cursor.anchor(), cursor.position())
            return cursor, selected, start
        text = self.message_edit.toPlainText()
        cursor.setPosition(0)
        cursor.setPosition(msg_unit_count(text), QTextCursor.MoveMode.KeepAnchor)
        return cursor, text, 0

    def _apply_message_style(self, style: str) -> None:
        if self._sending or self.message_edit.isReadOnly():
            return
        cursor, source, start = self._style_target_text()
        if not source:
            self._sync_format_buttons()
            return
        styled = apply_message_style(source, style)
        if styled == source:
            self._sync_format_buttons()
            return
        self.message_edit.blockSignals(True)
        cursor.insertText(styled)
        end = start + msg_unit_count(styled)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.message_edit.setTextCursor(cursor)
        self.message_edit.blockSignals(False)
        self._on_message_changed()
        self._sync_format_buttons()

    def _sync_format_buttons(self) -> None:
        cursor = self.message_edit.textCursor()
        selected = (
            cursor.selectedText()
            .replace("\u2029", "\n")
            .replace("\u2028", "\n")
        )
        sample = selected if selected else self.message_edit.toPlainText()
        state = inspect_message_style(sample)
        for btn, key in (
            (self.bold_btn, "bold"),
            (self.italic_btn, "italic"),
            (self.underline_btn, "underline"),
        ):
            btn.blockSignals(True)
            btn.setChecked(state.get(key) is True)
            btn.blockSignals(False)

    def _on_message_changed(self) -> None:
        text = self.message_edit.toPlainText()
        clipped = clip_message_text(text, MSG_MAX_LENGTH)
        if clipped != text:
            cursor = self.message_edit.textCursor()
            pos = cursor.position()
            self.message_edit.blockSignals(True)
            self.message_edit.setPlainText(clipped)
            self.message_edit.blockSignals(False)
            cursor.setPosition(min(pos, msg_unit_count(clipped)))
            self.message_edit.setTextCursor(cursor)
            text = clipped
        shown = msg_unit_count(text)
        self.counter_label.setText(f"{shown} / {MSG_MAX_LENGTH}")
        at_limit = shown >= MSG_MAX_LENGTH
        color = "#EA4335" if at_limit else COLOR_TEXT_MUTED
        self.counter_label.setStyleSheet(
            f"QLabel#messageCounter {{ color: {color}; font-size: {SIZE_UI_SMALL}pt; }}"
        )
        self._sync_format_buttons()
        self._refresh_actions()

    def clear_message(self) -> None:
        self.message_edit.clear()

    def can_send(self) -> bool:
        if self._sending:
            return False
        host = self._get_host()
        if not host or not is_valid_host(host) or not self._is_online():
            return False
        if not normalize_message(self.message_edit.toPlainText()):
            return False
        if self.radio_specific.isChecked() and self.current_recipient() is None:
            return False
        if not psexec_executable_available(get_pstools_dir()):
            return False
        request = self._draft_request()
        if request is None:
            return False
        return not self._service.validate_request(request)

    def _draft_request(self) -> Optional[MessageRequest]:
        recipient = self.current_recipient()
        if recipient is None:
            return None
        timeout = int(self.time_spin.value()) if self.time_check.isChecked() else None
        return MessageRequest(
            host=self._get_host(),
            recipient=recipient,
            message=self.message_edit.toPlainText(),
            timeout_seconds=timeout,
            verbose=self.verbose_check.isChecked(),
        )

    def _refresh_actions(self) -> None:
        sending = self._sending
        specific = self.radio_specific.isChecked()
        self.radio_all.setEnabled(not sending)
        self.radio_specific.setEnabled(not sending)
        self.message_edit.setReadOnly(sending)
        self.bold_btn.setEnabled(not sending)
        self.italic_btn.setEnabled(not sending)
        self.underline_btn.setEnabled(not sending)
        self.clear_format_btn.setEnabled(not sending)
        self.time_check.setEnabled(not sending)
        self.time_spin.setEnabled(self.time_check.isChecked() and not sending)
        self.verbose_check.setEnabled(not sending)
        self.session_combo.setEnabled(specific and not sending)
        self.refresh_sessions_btn.setEnabled(
            not sending
            and self._is_online()
            and bool(self._get_host())
            and is_valid_host(self._get_host())
        )
        self.send_btn.setEnabled(self.can_send())
        self.clear_btn.setEnabled(not sending)

    def send_message(self) -> None:
        if self._sending or not self.can_send():
            return
        request = self._draft_request()
        if request is None:
            return
        errors = self._service.validate_request(request)
        if errors:
            self._set_op_status("err", errors[0])
            return

        self._sending = True
        self._send_generation += 1
        generation = self._send_generation
        self._send_host = normalize_host(request.host)
        self._last_request = request
        self._refresh_actions()
        self.log_output.clear_log()
        self._set_op_status("checking", self.tr("Enviando…"))

        creds = CredentialContext()
        passwords: list[str] = []
        try:
            user, password = self._creds()
            creds = CredentialContext(user=user, password=password)
            spec = self._service.build_psexec_spec(
                request,
                pstools_path=get_pstools_dir(),
                creds=creds,
                include_password=True,
            )
            passwords = list(creds.passwords)
            self._send_passwords = passwords
            display = spec.sanitized_display(passwords=passwords)
            if display:
                self._append_console(display)
            self._active_generation = generation
            self._executor.run(
                spec,
                passwords=passwords,
                timeout=60.0,
                use_conpty=False,
            )
        except Exception as exc:
            self._sending = False
            self._send_passwords = []
            self._refresh_actions()
            self._set_op_status("err", str(exc))
        finally:
            creds.clear()

    def _on_send_result(self, result: object) -> None:
        if not isinstance(result, ExecutionResult):
            return
        if getattr(self, "_active_generation", -1) != self._send_generation:
            return
        request = self._last_request
        if request is None:
            return
        current = self._get_host()
        if current.casefold() != (self._send_host or "").casefold():
            self._sending = False
            self._send_passwords = []
            self._refresh_actions()
            return
        classified = self._service.classify_result(
            request, result, passwords=self._send_passwords
        )
        log_operation(
            "message",
            detail=history_detail(request, ok=classified.ok),
            exit_code=classified.exit_code,
            passwords=self._send_passwords,
        )
        passwords = list(self._send_passwords)
        self._sending = False
        self._send_passwords = []
        self._refresh_actions()
        if classified.ok:
            self.message_edit.clear()
            state = "ok"
        elif classified.kind is MessageResultKind.CANCELLED:
            state = "idle"
        else:
            state = "err"
        extra = classified.detail if classified.detail != classified.title else ""
        text = classified.title if not extra else f"{classified.title} {extra}"
        self._set_op_status(state, text)
        output = "\n".join(
            part
            for part in (result.stdout or "", result.stderr or "")
            if (part or "").strip()
        )
        if output.strip() and output.strip() not in text:
            from remoteops.utils.redaction import redact_command_text

            self._append_console(redact_command_text(output.strip(), passwords=passwords))

    def _creds(self) -> Tuple[str, str]:
        if self._creds_provider is None:
            return "", ""
        try:
            user, password = self._creds_provider()
            return (user or "").strip(), password or ""
        except Exception:
            return "", ""

    def _append_console(self, text: str) -> None:
        line = (text or "").strip()
        if not line:
            return
        for part in line.splitlines():
            piece = part.rstrip()
            if piece:
                self.log_output.append_log(f"[MSG] {piece}")

    def _set_op_status(self, state: str, text: str) -> None:
        session = {
            "idle": "idle",
            "checking": "running",
            "ok": "exited",
            "warn": "exited",
            "err": "error",
        }.get(state, "idle")
        self.log_output.set_session_status(session)
        if state != "idle":
            self._append_console(text)

    def shutdown(self, wait_ms: int = 3000) -> None:
        self._closing = True
        self._send_generation += 1
        if self._host_source is not None:
            try:
                self._host_source.textChanged.disconnect(self.sync_from_host)
            except TypeError:
                pass
        worker = self._session_worker
        if worker is not None:
            try:
                worker.result.disconnect(self._on_sessions_result)
            except TypeError:
                pass
            if worker.isRunning():
                worker.wait(min(2000, wait_ms))
            self._session_worker = None
        exe = self._executor_inst
        if exe is not None:
            try:
                exe.resultReady.disconnect(self._on_send_result)
            except TypeError:
                pass
            exe.stop()
            try:
                exe.shutdown(wait=wait_ms > 0)
            except Exception:
                pass
            self._executor_inst = None
        self._sending = False
        self._send_passwords = []
