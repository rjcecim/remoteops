from PyQt6.QtWidgets import (
    QWidget, QCheckBox, QComboBox, QLineEdit, QHBoxLayout, QSizePolicy
)
from remoteops.ui.widgets.card import (
    CardWidget, grid_in_card, add_row, make_card_stack,
)


class MsiTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        vbox = make_card_stack(self)

        # ── Card Comando MSI ──────────────────────────────────────────────────
        card_cmd = CardWidget("\uE8A5", self.tr("Comando MSI"))
        g1 = grid_in_card(card_cmd)
        row = 0

        self.action_combo = QComboBox()
        self.action_combo.addItems([
            self.tr("Nenhum"),
            "/i", "/x", "/a", "/jm", "/ju"
        ])
        self.action_combo.setCurrentIndex(0)
        self.action_tooltips = [
            self.tr("Não especificar ação"),
            self.tr("Instalar pacote MSI (default)"),
            self.tr("Desinstalar pacote MSI"),
            self.tr("Instalação administrativa (rede)"),
            self.tr("Instalação com cache local (usuário atual)"),
            self.tr("Instalação com cache local (todos usuários)")
        ]
        self.action_combo.currentIndexChanged.connect(self.on_action_changed)
        add_row(g1, row, self.tr("Comando:"), self.action_combo)
        row += 1

        self.interface_combo = QComboBox()
        self.interface_combo.addItems([
            self.tr("Nenhum"),
            "/quiet", "/passive", "/qn", "/qb", "/qr", "/qf"
        ])
        self.interface_combo.setCurrentIndex(0)
        self.interface_tooltips = [
            self.tr("Não especificar interface"),
            self.tr("Instala silenciosamente, sem interface gráfica"),
            self.tr("Instala com interface mínima (barra de progresso)"),
            self.tr("Sem interface gráfica (UI nenhuma)"),
            self.tr("Interface básica (UI mínima)"),
            self.tr("Interface reduzida (UI reduzida)"),
            self.tr("Interface completa (UI completa)")
        ]
        self.interface_combo.currentIndexChanged.connect(self.on_interface_changed)
        add_row(g1, row, self.tr("Modo:"), self.interface_combo)
        row += 1

        self.restart_combo = QComboBox()
        self.restart_combo.addItems([
            self.tr("Nenhum"),
            "/norestart", "/promptrestart", "/forcerestart"
        ])
        self.restart_combo.setCurrentIndex(0)
        self.restart_tooltips = [
            self.tr("Não especificar política de reinício"),
            self.tr("Não reiniciar após a instalação"),
            self.tr("Perguntar antes de reiniciar"),
            self.tr("Forçar reinício após a instalação")
        ]
        self.restart_combo.currentIndexChanged.connect(self.on_restart_changed)
        add_row(g1, row, self.tr("Política:"), self.restart_combo)

        vbox.addWidget(card_cmd)

        # ── Card Log ───────────────────────────────────────────────────────────
        card_log = CardWidget("\uE9F9", self.tr("Log"))
        g2 = grid_in_card(card_log)

        log_file_layout = QHBoxLayout()
        log_file_layout.setContentsMargins(0, 0, 0, 0)
        log_file_layout.setSpacing(5)
        self.log_checkbox = QCheckBox(self.tr("Habilitar log"))
        self.log_checkbox.setChecked(False)
        self.log_checkbox.setToolTip(self.tr("Habilita o log detalhado da instalação em arquivo"))
        self.log_file_edit = QLineEdit()
        self.log_file_edit.setPlaceholderText(self.tr("C:\\temp\\install.log"))
        self.log_file_edit.setText("")
        self.log_file_edit.setToolTip(self.tr("Caminho do arquivo de log detalhado (ex: C:\\temp\\install.log)"))
        log_file_layout.addWidget(self.log_checkbox)
        log_file_layout.addWidget(self.log_file_edit)
        log_container = QWidget()
        log_container.setLayout(log_file_layout)
        log_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        add_row(g2, 0, self.tr("Arquivo:"), log_container)

        vbox.addWidget(card_log)

        # ── Card Opções avançadas ──────────────────────────────────────────────
        card_opt = CardWidget("\uE115", self.tr("Opções avançadas"))
        g3 = grid_in_card(card_opt)

        self.repair_spin = QLineEdit()
        self.repair_spin.setPlaceholderText(self.tr("p|o|e|d|c|a|u|m|s|v"))
        self.repair_spin.setText("")
        self.repair_spin.setToolTip(self.tr("Parâmetros de reparo: p=arquivos, o=componentes, e=registro, d=arquivos de dados, c=arquivos de configuração, a=todos, u=usuário, m=machine, s=shortcuts, v=volumes"))
        add_row(g3, 0, self.tr("Opções:"), self.repair_spin)

        self.update_edit = QLineEdit()
        self.update_edit.setPlaceholderText(self.tr("PROPERTY=Value PROPERTY2=Value2"))
        self.update_edit.setText("")
        self.update_edit.setToolTip(self.tr("Propriedades do MSI (ex: ALLUSERS=1 REBOOT=ReallySuppress)"))
        add_row(g3, 1, self.tr("Lista:"), self.update_edit)

        vbox.addWidget(card_opt)

        self.update_action_tooltip()
        self.update_interface_tooltip()
        self.update_restart_tooltip()

    def on_action_changed(self, index):
        self.update_action_tooltip()

    def on_interface_changed(self, index):
        self.update_interface_tooltip()

    def on_restart_changed(self, index):
        self.update_restart_tooltip()

    def update_action_tooltip(self):
        index = self.action_combo.currentIndex()
        if 0 <= index < len(self.action_tooltips):
            self.action_combo.setToolTip(self.action_tooltips[index])

    def update_interface_tooltip(self):
        index = self.interface_combo.currentIndex()
        if 0 <= index < len(self.interface_tooltips):
            self.interface_combo.setToolTip(self.interface_tooltips[index])

    def update_restart_tooltip(self):
        index = self.restart_combo.currentIndex()
        if 0 <= index < len(self.restart_tooltips):
            self.restart_combo.setToolTip(self.restart_tooltips[index])

    def reset_to_defaults(self) -> None:
        self.action_combo.setCurrentIndex(0)
        self.interface_combo.setCurrentIndex(0)
        self.restart_combo.setCurrentIndex(0)
        self.log_checkbox.setChecked(False)
        self.log_file_edit.clear()
        self.repair_spin.clear()
        self.update_edit.clear()
