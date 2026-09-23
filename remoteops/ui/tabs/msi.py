"""Aba MSI — parâmetros do msiexec para o fluxo individual e o lote."""

from __future__ import annotations

from typing import Callable, Optional

from PyQt6 import sip
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QWidget

from remoteops.services.batch_msi import preview_msiexec_command
from remoteops.ui.style import SIZE_UI_SMALL
from remoteops.ui.widgets.card import (
    CardWidget,
    add_row,
    bind_card_stack,
    grid_in_card,
    make_card_stack,
)
from remoteops.ui.widgets.combobox import FluentComboBox


class MsiTab(QWidget):
    formLayoutChanged = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        msi_provider: Optional[Callable[[], str]] = None,
        robocopy_params_provider: Optional[Callable[[], dict]] = None,
    ):
        super().__init__(parent)
        self._msi_provider = msi_provider or (lambda: "")
        self._robocopy_params_provider = robocopy_params_provider or (lambda: {})
        self._msi_path = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        vbox = make_card_stack(self)

        card_cmd = CardWidget("\uE8A5", self.tr("Comando MSI"))
        g1 = grid_in_card(card_cmd)
        row = 0

        self.file_lbl = QLabel(self.tr("Nenhum arquivo .msi selecionado"))
        self.file_lbl.setObjectName("msiSelectedFile")
        self.file_lbl.setWordWrap(True)
        self.file_lbl.setStyleSheet(
            f"QLabel#msiSelectedFile {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        add_row(g1, row, self.tr("Arquivo:"), self.file_lbl)
        row += 1

        self.action_combo = FluentComboBox()
        self.action_combo.addItems([self.tr("Nenhum"), "/i", "/x", "/a", "/jm", "/ju"])
        self.action_combo.setCurrentIndex(1)
        self.action_tooltips = [
            self.tr("Não especificar ação"),
            self.tr("Instalar pacote MSI (default)"),
            self.tr("Desinstalar pacote MSI"),
            self.tr("Instalação administrativa (rede)"),
            self.tr("Instalação com cache local (usuário atual)"),
            self.tr("Instalação com cache local (todos usuários)"),
        ]
        self.action_combo.currentIndexChanged.connect(self.on_action_changed)
        add_row(g1, row, self.tr("Comando:"), self.action_combo)
        row += 1

        self.interface_combo = FluentComboBox()
        self.interface_combo.addItems(
            [self.tr("Nenhum"), "/quiet", "/passive", "/qn", "/qb", "/qr", "/qf"]
        )
        self.interface_combo.setCurrentIndex(3)
        self.interface_tooltips = [
            self.tr("Não especificar interface"),
            self.tr("Instala silenciosamente, sem interface gráfica"),
            self.tr("Instala com interface mínima (barra de progresso)"),
            self.tr("Sem interface gráfica (UI nenhuma)"),
            self.tr("Interface básica (UI mínima)"),
            self.tr("Interface reduzida (UI reduzida)"),
            self.tr("Interface completa (UI completa)"),
        ]
        self.interface_combo.currentIndexChanged.connect(self.on_interface_changed)
        add_row(g1, row, self.tr("Modo:"), self.interface_combo)
        row += 1

        self.restart_combo = FluentComboBox()
        self.restart_combo.addItems(
            [self.tr("Nenhum"), "/norestart", "/promptrestart", "/forcerestart"]
        )
        self.restart_combo.setCurrentIndex(1)
        self.restart_tooltips = [
            self.tr("Não especificar política de reinício"),
            self.tr("Não reiniciar após a instalação"),
            self.tr("Perguntar antes de reiniciar"),
            self.tr("Forçar reinício após a instalação"),
        ]
        self.restart_combo.currentIndexChanged.connect(self.on_restart_changed)
        add_row(g1, row, self.tr("Política:"), self.restart_combo)
        row += 1

        self.preview_lbl = QLabel("")
        self.preview_lbl.setObjectName("msiCommandPreview")
        self.preview_lbl.setWordWrap(True)
        self.preview_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.preview_lbl.setStyleSheet(
            f"QLabel#msiCommandPreview {{ color: palette(windowText); font-size: {SIZE_UI_SMALL}pt; }}"
        )
        add_row(g1, row, self.tr("Prévia:"), self.preview_lbl)
        vbox.addWidget(card_cmd)

        card_log = CardWidget("\uE9F9", self.tr("Log"))
        g2 = grid_in_card(card_log)
        log_file_layout = QHBoxLayout()
        log_file_layout.setContentsMargins(0, 0, 0, 0)
        log_file_layout.setSpacing(5)
        self.log_checkbox = QCheckBox(self.tr("Habilitar log"))
        self.log_checkbox.setChecked(False)
        self.log_checkbox.setToolTip(
            self.tr("Habilita o log detalhado da instalação em arquivo")
        )
        self.log_file_edit = QLineEdit()
        self.log_file_edit.setPlaceholderText(self.tr("C:\\temp\\install.log"))
        self.log_file_edit.setText("")
        self.log_file_edit.setToolTip(
            self.tr("Caminho do arquivo de log detalhado (ex: C:\\temp\\install.log)")
        )
        log_file_layout.addWidget(self.log_checkbox)
        log_file_layout.addWidget(self.log_file_edit)
        log_container = QWidget()
        log_container.setLayout(log_file_layout)
        log_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        add_row(g2, 0, self.tr("Arquivo:"), log_container)
        vbox.addWidget(card_log)

        card_opt = CardWidget("\uE115", self.tr("Opções avançadas"))
        g3 = grid_in_card(card_opt)
        self.repair_spin = QLineEdit()
        self.repair_spin.setPlaceholderText(self.tr("p|o|e|d|c|a|u|m|s|v"))
        self.repair_spin.setText("")
        self.repair_spin.setToolTip(
            self.tr(
                "Parâmetros de reparo: p=arquivos, o=componentes, e=registro, "
                "d=arquivos de dados, c=arquivos de configuração, a=todos, "
                "u=usuário, m=machine, s=shortcuts, v=volumes"
            )
        )
        add_row(g3, 0, self.tr("Opções:"), self.repair_spin)
        self.update_edit = QLineEdit()
        self.update_edit.setPlaceholderText(self.tr("PROPERTY=Value PROPERTY2=Value2"))
        self.update_edit.setText("")
        self.update_edit.setToolTip(
            self.tr("Propriedades do MSI (ex: ALLUSERS=1 REBOOT=ReallySuppress)")
        )
        add_row(g3, 1, self.tr("Lista:"), self.update_edit)
        vbox.addWidget(card_opt)

        self._form_cards = (card_cmd, card_log, card_opt)
        for card, on_reset in zip(
            self._form_cards,
            (self._reset_card_comando, self._reset_card_log, self._reset_card_opcoes),
        ):
            card.set_collapsible(True, collapsed=False)
            card.set_resettable(True, self.tr("Restaurar padrões deste card"))
            card.resetRequested.connect(on_reset)
            card.collapsedChanged.connect(self._on_form_card_collapsed)
        bind_card_stack(vbox, self._form_cards)

        for widget in (self.action_combo, self.interface_combo, self.restart_combo):
            widget.currentTextChanged.connect(self._refresh_preview)
        self.log_checkbox.stateChanged.connect(self._refresh_preview)
        self.log_file_edit.textChanged.connect(self._refresh_preview)
        self.repair_spin.textChanged.connect(self._refresh_preview)
        self.update_edit.textChanged.connect(self._refresh_preview)

        self.update_action_tooltip()
        self.update_interface_tooltip()
        self.update_restart_tooltip()
        self._refresh_preview()

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

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

    def _reset_card_comando(self) -> None:
        self.action_combo.setCurrentIndex(1)
        self.interface_combo.setCurrentIndex(3)
        self.restart_combo.setCurrentIndex(1)
        self.update_action_tooltip()
        self.update_interface_tooltip()
        self.update_restart_tooltip()

    def _reset_card_log(self) -> None:
        self.log_checkbox.setChecked(False)
        self.log_file_edit.clear()

    def _reset_card_opcoes(self) -> None:
        self.repair_spin.clear()
        self.update_edit.clear()

    def reset_to_defaults(self) -> None:
        self._reset_card_comando()
        self._reset_card_log()
        self._reset_card_opcoes()
        self._refresh_preview()

    def get_params(self) -> dict:
        return {
            "enable": True,
            "action": self.action_combo.currentText(),
            "interface": self.interface_combo.currentText(),
            "restart": self.restart_combo.currentText(),
            "log": self.log_checkbox.isChecked(),
            "log_file": self.log_file_edit.text(),
            "repair": self.repair_spin.text(),
            "update": self.update_edit.text(),
        }

    def current_preview_command(self) -> str:
        path = self._current_msi()
        if not path:
            return "# msiexec [opções] <arquivo.msi>"
        try:
            robocopy = self._robocopy_params_provider() or {}
        except Exception:
            robocopy = {}
        return preview_msiexec_command(
            msi_path=path,
            msi_params=self.get_params(),
            robocopy_params=robocopy,
        )

    def _refresh_preview(self, *_args) -> None:
        if sip.isdeleted(self):
            return
        self.preview_lbl.setText(self.current_preview_command())

    def set_msi_path(self, path: str) -> None:
        self._msi_path = (path or "").strip()
        if self._msi_path:
            self.file_lbl.setText(self._msi_path)
        else:
            self.file_lbl.setText(self.tr("Nenhum arquivo .msi selecionado"))
        self._refresh_preview()

    def _current_msi(self) -> str:
        try:
            path = self._msi_provider() or self._msi_path or ""
        except Exception:
            path = self._msi_path or ""
        return str(path).strip()
