from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QWidget,
)

from remoteops.core.psexec_options import (
    TOOLTIPS,
    PsExecOptions,
    compute_psexec_option_state,
    options_to_params,
)
from remoteops.ui.style import (
    CARD_GRID_VERTICAL_SPACING,
    INPUT_HEIGHT,
    SIZE_UI_SMALL,
    make_icon_button,
)
from remoteops.ui.widgets.card import CardWidget, make_card_stack
from remoteops.ui.widgets.flow import FlowLayout
from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot
from remoteops.utils.api import get_processor_count, get_processor_groups
from remoteops.utils.ping import is_valid_host, normalize_host, ping_host
from remoteops.utils.sessions import RemoteSession, list_remote_sessions
from remoteops.utils.validator import AffinityValidator


class _HostStatusWorker(QThread):
    """Ping em background; emite o host consultado e se está online."""

    result = pyqtSignal(str, bool)

    def __init__(self, host: str, parent=None):
        super().__init__(parent)
        self._host = host

    def run(self) -> None:
        online, _ = ping_host(self._host)
        self.result.emit(self._host, online)


class _SessionListWorker(QThread):
    """Lista sessões WTS do host em background."""

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


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    # Mantém alinhado com as outras abas (padrão antigo ~120)
    lbl.setMinimumWidth(120)
    # opacidade reduzida via stylesheet
    lbl.setStyleSheet("QLabel#fieldLabel { color: palette(windowText); opacity: 0.75; }")
    return lbl


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


def _line_edit_with_clear_icon(password: bool = False):
    """
    Container com QLineEdit e ícone de remover à direita.
    Retorna (container, line_edit). O ícone só aparece quando há texto.
    Altura igual aos demais QLineEdit (INPUT_HEIGHT).
    """
    container = QWidget()
    container.setObjectName("AuthField")
    container.setFixedHeight(INPUT_HEIGHT)
    container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    container.setStyleSheet(f"""
        QWidget#AuthField {{
            border: 1px solid palette(mid);
            border-radius: 4px;
            background: palette(base);
        }}
        QWidget#AuthField:focus-within {{ border-color: palette(highlight); }}
        QWidget#AuthField QLineEdit {{
            border: none;
            background: transparent;
            padding: 0;
            min-height: 0px;
            max-height: {INPUT_HEIGHT}px;
        }}
    """)
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
    clear_btn.setToolTip("Limpar")
    clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    clear_btn.setStyleSheet("""
        QToolButton { border: none; background: transparent; color: palette(highlight); }
        QToolButton:hover { background: palette(light); border-radius: 11px; }
        QToolButton:pressed { background: palette(dark); }
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


# ── tab principal ─────────────────────────────────────────────────────────────

class PsExecTab(QWidget):
    openHostAppsRequested = pyqtSignal()
    openWinGetRequested = pyqtSignal()
    openPsInfoRequested = pyqtSignal()
    openRustDeskRequested = pyqtSignal()
    formLayoutChanged = pyqtSignal()
    hostOnlineChanged = pyqtSignal(bool)

    def __init__(self, parent=None, log_output=None):
        super().__init__(parent)
        self.log_output = log_output
        self._host_online = False
        self._host_status_worker: Optional[_HostStatusWorker] = None
        self._host_status_wanted = ""
        self._copy_allowed = True
        self._updating_option_state = False
        self._session_worker: Optional[_SessionListWorker] = None
        self._session_wanted = ""
        self._session_refresh_timer = QTimer(self)
        self._session_refresh_timer.setSingleShot(True)
        self._session_refresh_timer.setInterval(150)
        self._session_refresh_timer.timeout.connect(self._refresh_remote_sessions)
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

        # Host remoto
        host_row = QHBoxLayout()
        host_row.setSpacing(4)
        host_row.setContentsMargins(0, 0, 0, 0)
        host_clear_container, self.host_edit = _line_edit_with_clear_icon()
        self.host_edit.setPlaceholderText("ex: 192.168.1.100 ou computador.local")
        self.host_edit.setToolTip(self.tr("Nome ou IP do computador remoto"))
        # \uE71D = List — aplicativos do host (Remote Registry)
        self.hostapps_button = make_icon_button(
            "\uE71D", self.tr("Listar aplicativos do host (Remote Registry)")
        )
        self.hostapps_button.clicked.connect(self.openHostAppsRequested.emit)
        # \uE7B8 = Package — WinGet (gerenciador de pacotes)
        self.winget_button = make_icon_button(
            "\uE7B8", self.tr("WinGet — pacotes remotos (winget)")
        )
        self.winget_button.clicked.connect(self.openWinGetRequested.emit)
        self.psinfo_button = make_icon_button("\uE946", self.tr("Abrir PsInfo (inventário)"))
        self.psinfo_button.clicked.connect(self.openPsInfoRequested.emit)
        # \uE8B7 (Copy) já usado em Robocopy; aqui usamos \uE774 (Link) como ação de conexão
        self.rustdesk_button = make_icon_button("\uE774", self.tr("Conectar via RustDesk"))
        self.rustdesk_button.clicked.connect(self.openRustDeskRequested.emit)
        host_row.addWidget(host_clear_container)
        host_row.addWidget(self.hostapps_button)
        host_row.addWidget(self.winget_button)
        host_row.addWidget(self.psinfo_button)
        host_row.addWidget(self.rustdesk_button)
        host_container = QWidget()
        host_container.setLayout(host_row)
        host_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g1, 0, self.tr("Host remoto"), host_container)

        # Status (legenda com bolinha abaixo do host)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        status_row.setContentsMargins(2, 0, 0, 0)
        self.host_status_dot = _StatusDot()
        self.host_status_label = QLabel()
        self.host_status_label.setObjectName("hostStatusCaption")
        self.host_status_label.setStyleSheet(
            f"QLabel#hostStatusCaption {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        status_row.addWidget(self.host_status_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addWidget(self.host_status_label, 0, Qt.AlignmentFlag.AlignVCenter)
        status_row.addStretch()
        status_container = QWidget()
        status_container.setLayout(status_row)
        status_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g1, 1, self.tr("Status"), status_container)
        self._set_host_status("idle")  # desabilita ações até o host ficar Online
        self.host_edit.textChanged.connect(self._on_host_text_changed)

        # Comando remoto
        remote_cmd_container, self.remote_cmd_edit = _line_edit_with_clear_icon()
        self.remote_cmd_edit.setPlaceholderText(
            self.tr(r"Programa remoto a executar, ex: \\SERVIDOR\cmd.exe")
        )
        self.remote_cmd_edit.setToolTip(
            self.tr("Comando completo a ser executado remotamente")
        )
        self.remote_cmd_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g1, 2, self.tr("Comando remoto"), remote_cmd_container)

        vbox.addWidget(card1)

        # ── Card 2 — Autenticação ─────────────────────────────────────────────
        card2 = CardWidget("\uE8D7", self.tr("Autenticação"))
        g2 = _grid_in_card(card2)

        user_container, self.user_edit = _line_edit_with_clear_icon(password=False)
        self.user_edit.setPlaceholderText(r"DOMAIN\user")
        self.user_edit.setToolTip(self.tr(TOOLTIPS["-u"]))
        _add_row(g2, 0, self.tr("Usuário"), user_container)

        self._pass_container, self.pass_edit = _line_edit_with_clear_icon(password=True)
        self.pass_edit.setPlaceholderText(self.tr("Senha"))
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setToolTip(self.tr(TOOLTIPS["-p"]))
        _add_row(g2, 1, self.tr("Senha"), self._pass_container)

        vbox.addWidget(card2)

        # ── Card 3 — Privilégios e Sessão ─────────────────────────────────────
        card3 = CardWidget("\uE8D4", self.tr("Privilégios e Sessão"))
        g3 = _grid_in_card(card3)

        # Elevação
        elev_row = QHBoxLayout()
        elev_row.setSpacing(10)
        elev_row.setContentsMargins(0, 0, 0, 0)
        self.flag_h = QCheckBox("-h  " + self.tr("Elevado"))
        self.flag_h.setToolTip(self.tr(TOOLTIPS["-h"]))
        self.flag_s = QCheckBox("-s  SYSTEM")
        self.flag_s.setToolTip(self.tr(TOOLTIPS["-s"]))
        self.flag_l = QCheckBox("-l  " + self.tr("Limitado"))
        self.flag_l.setToolTip(self.tr(TOOLTIPS["-l"]))
        elev_row.addWidget(self.flag_h)
        elev_row.addWidget(self.flag_s)
        elev_row.addWidget(self.flag_l)
        elev_row.addStretch()
        elev_container = QWidget()
        elev_container.setLayout(elev_row)
        elev_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g3, 0, self.tr("Elevação"), elev_container)

        # Sessão
        session_row = QHBoxLayout()
        session_row.setSpacing(8)
        session_row.setContentsMargins(0, 0, 0, 0)
        self.session_interactive = QCheckBox(self.tr("Interativo (-i)"))
        self.session_interactive.setToolTip(self.tr(TOOLTIPS["-i"]))
        self.session_combo = QComboBox()
        self.session_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.session_combo.setEnabled(False)
        self.session_combo.setToolTip(self.tr(TOOLTIPS["session_id"]))
        self._reset_session_combo()
        self.session_id_label = QLabel(self.tr("ID da sessão"))
        self.session_id_label.setStyleSheet("color: palette(windowText);")
        session_row.addWidget(self.session_interactive)
        session_row.addWidget(self.session_id_label)
        session_row.addWidget(self.session_combo)
        session_container = QWidget()
        session_container.setLayout(session_row)
        session_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        _add_row(g3, 1, self.tr("Sessão"), session_container)

        vbox.addWidget(card3)

        # ── Card 4 — Desempenho ───────────────────────────────────────────────
        card4 = CardWidget("\uE950", self.tr("Desempenho"))
        g4 = _grid_in_card(card4)

        # Prioridade
        self.priority_combo = QComboBox()
        self.priority_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.priority_combo.addItem(self.tr("Padrão (sem alteração)"), "")
        self.priority_combo.addItem(self.tr("-low  Baixa"), "-low")
        self.priority_combo.addItem(self.tr("-belownormal  Abaixo do normal"), "-belownormal")
        self.priority_combo.addItem(self.tr("-abovenormal  Acima do normal"), "-abovenormal")
        self.priority_combo.addItem(self.tr("-high  Alta"), "-high")
        self.priority_combo.addItem(self.tr("-realtime  Tempo real"), "-realtime")
        self.priority_combo.addItem(self.tr("-background  Segundo plano"), "-background")
        self.priority_combo.setCurrentIndex(0)
        self.priority_combo.setToolTip(self.tr(TOOLTIPS["priority"]))
        _add_row(g4, 0, self.tr("Prioridade"), self.priority_combo)

        # Grupo CPU — opcional; não é pré-requisito de -a
        self.group_combo = QComboBox()
        self.group_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.group_combo.addItem(self.tr("Nenhum"), None)
        self.processor_groups = get_processor_groups()
        for group_id in self.processor_groups:
            self.group_combo.addItem(str(group_id), group_id)
        self.group_combo.setCurrentIndex(0)
        self.group_combo.setToolTip(self.tr(TOOLTIPS["-g"]))
        _add_row(g4, 1, self.tr("Grupo CPU"), self.group_combo)

        # Afinidade CPU — válida sem -g
        self.affinity_edit = QLineEdit()
        self.affinity_edit.setPlaceholderText(self.tr("ex: 1,2,3"))
        self.affinity_edit.setToolTip(self.tr(TOOLTIPS["-a"]))
        self.current_max_cpu = get_processor_count(0)
        self.affinity_validator = AffinityValidator(self.current_max_cpu, self.affinity_edit)
        self.affinity_edit.setValidator(self.affinity_validator)
        _add_row(g4, 2, self.tr("Afinidade CPU"), self.affinity_edit)

        vbox.addWidget(card4)

        # ── Card 5 — Flags e Argumentos ──────────────────────────────────────
        card5 = CardWidget("\uE115", self.tr("Flags e Argumentos"))
        g5 = _grid_in_card(card5)

        # Timeout
        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(6)
        timeout_row.setContentsMargins(0, 0, 0, 0)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 9999)
        self.timeout_spin.setValue(0)
        self.timeout_spin.setToolTip(self.tr("Timeout em segundos (0 = sem timeout)"))
        self.timeout_spin.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
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

    def _reset_card_conexao(self) -> None:
        self.host_edit.clear()
        if not self.remote_cmd_edit.isReadOnly():
            self.remote_cmd_edit.clear()
        self.update_psexec_option_state()

    def _reset_card_autenticacao(self) -> None:
        self.user_edit.clear()
        self.pass_edit.clear()
        self.update_psexec_option_state("user")

    def _reset_card_privilegios(self) -> None:
        for cb in (self.flag_h, self.flag_s, self.flag_l, self.session_interactive):
            cb.setChecked(False)
        self._reset_session_combo()
        self.update_psexec_option_state()

    def _reset_card_desempenho(self) -> None:
        self.priority_combo.setCurrentIndex(0)
        self.group_combo.setCurrentIndex(0)
        self.affinity_edit.clear()
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

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    # ── estado / compatibilidade ──────────────────────────────────────────────

    def _connect_option_signals(self) -> None:
        self.user_edit.textChanged.connect(lambda: self.update_psexec_option_state("user"))
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
        group = self.group_combo.currentData()
        if group is not None:
            try:
                group = int(group)
            except (TypeError, ValueError):
                group = None
        return PsExecOptions(
            user=(self.user_edit.text() or "").strip(),
            has_password=bool((self.pass_edit.text() or "").strip()),
            flag_h=self.flag_h.isChecked(),
            flag_s=self.flag_s.isChecked(),
            flag_e=self.flag_e.isChecked(),
            flag_l=self.flag_l.isChecked(),
            session_interactive=self.session_interactive.isChecked(),
            session_id=self._current_session_id(),
            priority=self.priority_combo.currentData() or "",
            cpu_group=group,
            affinity=(self.affinity_edit.text() or "").strip(),
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
        user = opts.user
        params = options_to_params(opts)
        params.update(
            {
                "host": host,
                "psexec_path": psexec_path,
                "remote_cmd": remote_cmd,
                "has_password": bool((self.pass_edit.text() or "").strip()) and bool(user),
            }
        )
        return params

    def set_copy_allowed(self, allowed: bool) -> None:
        if self._copy_allowed == bool(allowed):
            return
        self._copy_allowed = bool(allowed)
        self.update_psexec_option_state()

    def update_psexec_option_state(self, trigger: Optional[str] = None) -> None:
        """Única rotina que recalcula habilitados, conflitos e tooltips."""
        if self._updating_option_state:
            return
        self._updating_option_state = True
        try:
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

        pass_state = widgets.get("-p")
        user_filled = bool(opts.user.strip())
        if pass_state is not None:
            self._pass_container.setEnabled(bool(pass_state.enabled))
            self.pass_edit.setEnabled(bool(pass_state.enabled))
            self.pass_edit.setToolTip(self.tr(pass_state.tooltip))
        if not user_filled and (self.pass_edit.text() or "").strip():
            self.pass_edit.blockSignals(True)
            self.pass_edit.clear()
            self.pass_edit.blockSignals(False)

        session_state = widgets.get("session_id")
        if session_state is not None:
            self.session_combo.setEnabled(bool(session_state.enabled))
            self.session_id_label.setEnabled(bool(session_state.enabled))
            if session_state.tooltip:
                self.session_combo.setToolTip(self.tr(session_state.tooltip))
        if not opts.session_interactive:
            self._reset_session_combo()
        elif trigger in ("-i", "user") and self.is_host_online:
            self._schedule_session_refresh()

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

        extra_state = widgets.get("extra_args")
        if extra_state is not None:
            self.extra_args.setToolTip(self.tr(extra_state.tooltip))

    def _current_session_id(self) -> Optional[int]:
        data = self.session_combo.currentData()
        if data is None:
            return None
        try:
            return int(data)
        except (TypeError, ValueError):
            return None

    def _reset_session_combo(self, *, keep_enabled: bool = False) -> None:
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("não especificar"), None)
        self.session_combo.setCurrentIndex(0)
        self.session_combo.blockSignals(False)
        if not keep_enabled:
            self.session_combo.setEnabled(self.session_interactive.isChecked())

    def _set_session_combo_status(self, status: str) -> None:
        selected = self._current_session_id()
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("não especificar"), None)
        self.session_combo.addItem(status, "status")
        self.session_combo.setCurrentIndex(0)
        if selected is not None:
            self.session_combo.addItem(str(selected), selected)
            found = self.session_combo.findData(selected)
            if found >= 0:
                self.session_combo.setCurrentIndex(found)
        self.session_combo.blockSignals(False)

    def _schedule_session_refresh(self) -> None:
        if not self.session_interactive.isChecked() or not self.is_host_online:
            return
        self._session_refresh_timer.start()

    def _refresh_remote_sessions(self) -> None:
        host = normalize_host(self.host_edit.text())
        if (
            not self.session_interactive.isChecked()
            or not self.is_host_online
            or not host
            or not is_valid_host(host)
        ):
            return
        worker = self._session_worker
        if worker is not None and worker.isRunning():
            self._session_wanted = host
            return
        self._session_wanted = host
        self._set_session_combo_status(self.tr("Consultando…"))
        self.session_combo.setToolTip(self.tr("Consultando sessões do host…"))
        self._session_worker = _SessionListWorker(
            host,
            user=(self.user_edit.text() or "").strip(),
            password=self.pass_edit.text() or "",
            parent=self,
        )
        self._session_worker.result.connect(self._on_sessions_result)
        self._session_worker.finished.connect(self._on_session_worker_finished)
        self._session_worker.start()

    def _on_sessions_result(self, host: str, sessions, error: str) -> None:
        wanted = self._session_wanted
        if host.casefold() != (wanted or "").casefold():
            return
        current = normalize_host(self.host_edit.text())
        if host.casefold() != current.casefold():
            return
        if not self.session_interactive.isChecked():
            return
        selected = self._current_session_id()
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        self.session_combo.addItem(self.tr("não especificar"), None)
        for item in sessions or []:
            if not isinstance(item, RemoteSession):
                continue
            self.session_combo.addItem(item.label(), int(item.session_id))
        if error and not sessions:
            self.session_combo.addItem(self.tr("Falha ao consultar"), "status")
        index = 0
        if selected is not None:
            found = self.session_combo.findData(selected)
            if found >= 0:
                index = found
        self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)
        if error and not sessions:
            self.session_combo.setToolTip(self.tr(error))
            log = getattr(self, "log_output", None)
            if log is not None and hasattr(log, "append_log"):
                log.append_log(self.tr(f"[PSEXEC] Sessões: {error}"))
        else:
            self.session_combo.setToolTip(self.tr(TOOLTIPS["session_id"]))
        self.session_combo.currentIndexChanged.emit(self.session_combo.currentIndex())

    def _on_session_worker_finished(self) -> None:
        worker = self._session_worker
        finished_host = getattr(worker, "_host", "") if worker is not None else ""
        self._session_worker = None
        if worker is not None:
            worker.deleteLater()
        current = normalize_host(self.host_edit.text())
        if (
            self.session_interactive.isChecked()
            and self.is_host_online
            and current
            and is_valid_host(current)
            and current.casefold() != (finished_host or "").casefold()
        ):
            self._refresh_remote_sessions()

    def _update_affinity_validator(self, group_id: Optional[int]) -> None:
        gid = 0 if group_id is None else int(group_id)
        try:
            cpu_count = get_processor_count(gid)
        except Exception:
            cpu_count = self.current_max_cpu or 1
        self.current_max_cpu = cpu_count
        self.affinity_validator = AffinityValidator(cpu_count, self.affinity_edit)
        self.affinity_edit.setValidator(self.affinity_validator)
        if group_id is None:
            self.affinity_edit.setPlaceholderText(self.tr(f"1-{cpu_count} (ex: 1,2,3)"))
        else:
            self.affinity_edit.setPlaceholderText(
                self.tr(f"Grupo {group_id}: 1-{cpu_count} (ex: 1,2,3)")
            )

    @property
    def is_host_online(self) -> bool:
        return bool(self._host_online)

    def _host_action_buttons(self):
        return (
            self.hostapps_button,
            self.winget_button,
            self.psinfo_button,
            self.rustdesk_button,
        )

    def _update_host_action_buttons(self, online: bool) -> None:
        for btn in self._host_action_buttons():
            btn.setEnabled(online)

    def _set_host_status(self, state: str, text: str | None = None) -> None:
        color = _STATUS_COLORS.get(state, _STATUS_COLORS["idle"])
        self.host_status_dot.set_color(color)
        labels = {
            "idle": self.tr("Aguardando host"),
            "checking": self.tr("Verificando…"),
            "online": self.tr("Online"),
            "offline": self.tr("Offline"),
            "invalid": self.tr("Host inválido"),
        }
        caption = text if text is not None else labels.get(state, "")
        self.host_status_label.setText(caption)
        self.host_status_dot.setToolTip(caption)
        self.host_status_label.setToolTip(caption)

        online = state == "online"
        self._update_host_action_buttons(online)
        if self._host_online != online:
            self._host_online = online
            self.hostOnlineChanged.emit(online)
            if online and self.session_interactive.isChecked():
                self._schedule_session_refresh()
            elif not online:
                self._reset_session_combo(keep_enabled=self.session_interactive.isChecked())

    def _on_host_text_changed(self, _text: str = "") -> None:
        host = normalize_host(self.host_edit.text())
        self._host_status_wanted = host
        if not host:
            self._host_status_timer.stop()
            self._set_host_status("idle")
            return
        if not is_valid_host(host):
            self._host_status_timer.stop()
            self._set_host_status("invalid")
            return
        self._set_host_status("checking")
        self._host_status_timer.start()

    def _check_host_status(self) -> None:
        host = normalize_host(self.host_edit.text())
        self._host_status_wanted = host
        if not host:
            self._set_host_status("idle")
            return
        if not is_valid_host(host):
            self._set_host_status("invalid")
            return

        self._set_host_status("checking")
        worker = self._host_status_worker
        if worker is not None and worker.isRunning():
            return

        self._start_host_status_worker(host)

    def _start_host_status_worker(self, host: str) -> None:
        self._host_status_worker = _HostStatusWorker(host, self)
        self._host_status_worker.result.connect(self._on_host_status_result)
        self._host_status_worker.finished.connect(self._on_host_status_worker_finished)
        self._host_status_worker.start()

    def _on_host_status_result(self, host: str, online: bool) -> None:
        wanted = self._host_status_wanted
        if host.casefold() != (wanted or "").casefold():
            return
        current = normalize_host(self.host_edit.text())
        if host.casefold() != current.casefold():
            return
        self._set_host_status("online" if online else "offline")

    def _on_host_status_worker_finished(self) -> None:
        worker = self._host_status_worker
        finished_host = getattr(worker, "_host", "") if worker is not None else ""
        self._host_status_worker = None
        if worker is not None:
            worker.deleteLater()

        current = normalize_host(self.host_edit.text())
        if (
            current
            and is_valid_host(current)
            and current.casefold() != (finished_host or "").casefold()
        ):
            self._set_host_status("checking")
            self._start_host_status_worker(current)
