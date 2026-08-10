from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QGridLayout, QLineEdit, QComboBox, QSpinBox,
    QCheckBox, QHBoxLayout, QLabel,
    QSizePolicy, QToolButton
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from remoteops.utils.api import get_processor_groups, get_processor_count
from remoteops.utils.ping import is_valid_host, normalize_host, ping_host
from remoteops.ui.style import (
    CARD_GRID_VERTICAL_SPACING,
    INPUT_HEIGHT,
    SIZE_UI_SMALL,
    make_icon_button,
)
from remoteops.utils.validator import AffinityValidator
from remoteops.ui.widgets.card import CardWidget, make_card_stack
from remoteops.ui.widgets.flow import FlowLayout


from remoteops.ui.widgets.status_dot import STATUS_COLORS as _STATUS_COLORS
from remoteops.ui.widgets.status_dot import StatusDot as _StatusDot


class _HostStatusWorker(QThread):
    """Ping em background; emite o host consultado e se está online."""

    result = pyqtSignal(str, bool)

    def __init__(self, host: str, parent=None):
        super().__init__(parent)
        self._host = host

    def run(self) -> None:
        online, _ = ping_host(self._host)
        self.result.emit(self._host, online)


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("fieldLabel")
    lbl.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
    # Mantém alinhado com as outras abas (padrão antigo ~120)
    lbl.setMinimumWidth(120)
    palette = lbl.palette()
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
    openPsInfoRequested = pyqtSignal()
    openRustDeskRequested = pyqtSignal()
    formLayoutChanged = pyqtSignal()

    def __init__(self, parent=None, log_output=None):
        super().__init__(parent)
        self.log_output = log_output
        self._host_status_worker: Optional[_HostStatusWorker] = None
        self._host_status_wanted = ""
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
        self.psinfo_button = make_icon_button("\uE946", self.tr("Abrir PsInfo (inventário)"))
        self.psinfo_button.clicked.connect(self.openPsInfoRequested.emit)
        # \uE8B7 (Copy) já usado em Robocopy; aqui usamos \uE774 (Link) como ação de conexão
        self.rustdesk_button = make_icon_button("\uE774", self.tr("Conectar via RustDesk"))
        self.rustdesk_button.clicked.connect(self.openRustDeskRequested.emit)
        host_row.addWidget(host_clear_container)
        host_row.addWidget(self.hostapps_button)
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
        self._set_host_status("idle")
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
        self.user_edit.setToolTip(self.tr(r"Usuário no formato DOMAIN\user"))
        _add_row(g2, 0, self.tr("Usuário"), user_container)

        pass_container, self.pass_edit = _line_edit_with_clear_icon(password=True)
        self.pass_edit.setPlaceholderText(self.tr("Senha"))
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pass_edit.setToolTip(self.tr("Senha do usuário"))
        _add_row(g2, 1, self.tr("Senha"), pass_container)

        vbox.addWidget(card2)

        # ── Card 3 — Privilégios e Sessão ─────────────────────────────────────
        card3 = CardWidget("\uE8D4", self.tr("Privilégios e Sessão"))
        g3 = _grid_in_card(card3)

        # Elevação
        elev_row = QHBoxLayout()
        elev_row.setSpacing(10)
        elev_row.setContentsMargins(0, 0, 0, 0)
        self.flag_h = QCheckBox("-h  " + self.tr("Elevado"))
        self.flag_h.setToolTip(self.tr("Executar com privilégios elevados"))
        self.flag_s = QCheckBox("-s  SYSTEM")
        self.flag_s.setToolTip(self.tr("Executar como System"))
        self.flag_l = QCheckBox("-l  " + self.tr("Limitado"))
        self.flag_l.setToolTip(self.tr("Executar com privilégios limitados"))
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
        self.session_interactive.setToolTip(
            self.tr("Torna o processo interativo na sessão especificada")
        )
        self.session_id_spin = QSpinBox()
        self.session_id_spin.setRange(0, 2147483647)
        self.session_id_spin.setValue(0)
        self.session_id_spin.setToolTip(
            self.tr("ID da sessão Windows (0 = console)")
        )
        self.session_id_spin.setEnabled(False)
        self.session_id_spin.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        session_id_label = QLabel(self.tr("ID da sessão"))
        session_id_label.setStyleSheet("color: palette(windowText);")
        self.session_interactive.stateChanged.connect(self.on_session_interactive_changed)
        session_row.addWidget(self.session_interactive)
        session_row.addWidget(session_id_label)
        session_row.addWidget(self.session_id_spin)
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
        self.priority_combo.currentIndexChanged.connect(self.on_priority_changed)
        _add_row(g4, 0, self.tr("Prioridade"), self.priority_combo)

        # Grupo CPU
        self.group_combo = QComboBox()
        self.group_combo.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.group_combo.addItem(self.tr("Nenhum"), None)
        self.processor_groups = get_processor_groups()
        for group_id in self.processor_groups:
            self.group_combo.addItem(str(group_id), group_id)
        self.group_combo.setCurrentIndex(0)
        self.group_combo.currentIndexChanged.connect(self.on_group_changed)
        _add_row(g4, 1, self.tr("Grupo CPU"), self.group_combo)

        # Afinidade CPU
        self.affinity_edit = QLineEdit()
        self.affinity_edit.setEnabled(False)
        self.affinity_edit.setPlaceholderText(self.tr("Campo desabilitado"))
        self.affinity_edit.setToolTip(
            self.tr("Selecione um grupo de processador para habilitar")
        )
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
        self.flag_d.setToolTip(self.tr("Não aguardar o processo terminar"))
        self.flag_e = QCheckBox("-e")
        self.flag_e.setToolTip(self.tr("Não carregar o perfil do usuário"))
        self.flag_c = QCheckBox("-c")
        self.flag_c.setToolTip(self.tr("Copiar o arquivo especificado para o sistema remoto"))
        self.flag_f = QCheckBox("-f")
        self.flag_f.setToolTip(self.tr("Copiar o arquivo apenas se for mais novo"))
        self.flag_v = QCheckBox("-v")
        self.flag_v.setToolTip(self.tr("Modo verbose"))
        self.flag_accepteula = QCheckBox("-accepteula")
        self.flag_accepteula.setToolTip(self.tr("Aceitar automaticamente o EULA"))
        self.flag_nobanner = QCheckBox("-nobanner")
        self.flag_nobanner.setToolTip(self.tr("Não exibir banner"))
        for cb in [
            self.flag_d, self.flag_e, self.flag_c, self.flag_f,
            self.flag_v, self.flag_accepteula, self.flag_nobanner,
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
        self.extra_args.setPlaceholderText(self.tr("Argumentos adicionais para o arquivo"))
        self.extra_args.setToolTip(self.tr("Argumentos extras para passar ao arquivo executado"))
        _add_row(g5, 2, self.tr("Args extras"), extra_args_container)

        vbox.addWidget(card5)

        # Tooltips de prioridade
        self._priority_tooltips = [
            self.tr("Prioridade padrão"),
            self.tr("Baixa prioridade"),
            self.tr("Abaixo do normal"),
            self.tr("Acima do normal"),
            self.tr("Alta prioridade"),
            self.tr("Tempo real"),
            self.tr("Em segundo plano"),
        ]
        self.update_priority_tooltip()
        self.update_affinity_for_group()

        # Collapsible só depois do conteúdo no layout — senão o padrão
        # minimizado (Autenticação / Desempenho) nasce com altura errada.
        self._form_cards = (card1, card2, card3, card4, card5)
        _collapsed_default = (False, True, False, True, False)
        for card, start_collapsed in zip(self._form_cards, _collapsed_default):
            card.set_collapsible(True, collapsed=start_collapsed)
            card.collapsedChanged.connect(self._on_form_card_collapsed)

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    # ── slots ─────────────────────────────────────────────────────────────────

    def on_session_interactive_changed(self, state):
        self.session_id_spin.setEnabled(state == 2)

    def on_priority_changed(self, _index):
        self.update_priority_tooltip()

    def on_group_changed(self, _index):
        self.update_affinity_for_group()

    def update_affinity_for_group(self):
        if self.group_combo.currentIndex() == 0:
            self.affinity_edit.setEnabled(False)
            self.affinity_edit.clear()
            self.affinity_edit.setPlaceholderText(self.tr("Campo desabilitado"))
            self.affinity_edit.setToolTip(
                self.tr("Selecione um grupo de processador para habilitar")
            )
            return

        group_id = self.group_combo.currentData()
        if group_id is None:
            return
        try:
            cpu_count = get_processor_count(group_id)
            self.current_max_cpu = cpu_count
            self.affinity_validator = AffinityValidator(cpu_count, self.affinity_edit)
            self.affinity_edit.setValidator(self.affinity_validator)
            self.affinity_edit.setEnabled(True)
            self.affinity_edit.setPlaceholderText(f"1-{cpu_count} (ex: 1,2,3)")
            self.affinity_edit.setToolTip(
                f"CPUs do grupo {group_id} (1-{cpu_count}) separadas por vírgula"
            )
        except Exception as exc:
            print(f"Erro ao obter CPUs do grupo {group_id}: {exc}")
            self.affinity_edit.setEnabled(True)
            self.affinity_edit.setPlaceholderText("1-8 (ex: 1,2,3)")
            self.affinity_edit.setToolTip(
                f"CPUs do grupo {group_id} separadas por vírgula"
            )

    def update_priority_tooltip(self):
        idx = self.priority_combo.currentIndex()
        if 0 <= idx < len(self._priority_tooltips):
            self.priority_combo.setToolTip(self._priority_tooltips[idx])

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
