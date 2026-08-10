from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QCheckBox, QComboBox, QLineEdit, QSizePolicy
)
from remoteops.ui.widgets.card import (
    CardWidget, grid_in_card, add_row, add_row_full_width, make_card_stack,
)


class PowerShellTab(QWidget):
    formLayoutChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        vbox = make_card_stack(self)

        # ── Card Opções ───────────────────────────────────────────────────────
        card_opts = CardWidget("\uE115", self.tr("Opções"))  # Engrenagem = opções de execução
        g1 = grid_in_card(card_opts)
        row = 0

        self.noprofile_checkbox = QCheckBox(self.tr("-NoProfile (Não carregar perfil)"))
        self.noprofile_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.noprofile_checkbox)
        row += 1

        self.noexit_checkbox = QCheckBox(self.tr("-NoExit (Não sair após executar)"))
        self.noexit_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.noexit_checkbox)
        row += 1

        self.execpol_combo = QComboBox()
        self.execpol_combo.addItems([
            self.tr("Nenhum"), "Bypass", "Unrestricted", "RemoteSigned", "AllSigned", "Restricted"
        ])
        self.execpol_combo.setCurrentIndex(0)
        add_row(g1, row, self.tr("-ExecutionPolicy:"), self.execpol_combo)
        row += 1

        self.winstyle_combo = QComboBox()
        self.winstyle_combo.addItems([
            self.tr("Nenhum"), "Normal", "Minimized", "Maximized", "Hidden"
        ])
        self.winstyle_combo.setCurrentIndex(0)
        add_row(g1, row, self.tr("-WindowStyle:"), self.winstyle_combo)

        vbox.addWidget(card_opts)

        # ── Card Comando ───────────────────────────────────────────────────────
        card_cmd = CardWidget("\uE768", self.tr("Comando"))  # Play = executar comando (igual ao card Comando da aba CMD)
        g2 = grid_in_card(card_cmd)

        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText(self.tr("Comando PowerShell (opcional)"))
        add_row(g2, 0, self.tr("-Command:"), self.command_edit)

        self.encoded_edit = QLineEdit()
        self.encoded_edit.setPlaceholderText(self.tr("Comando em Base64 (opcional)"))
        add_row(g2, 1, self.tr("-EncodedCommand:"), self.encoded_edit)

        vbox.addWidget(card_cmd)

        self._form_cards = (card_opts, card_cmd)
        for card in self._form_cards:
            card.set_collapsible(True, collapsed=False)
            card.collapsedChanged.connect(self._on_form_card_collapsed)

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    def set_command_fields_enabled(self, enabled: bool):
        self.command_edit.setEnabled(enabled)
        self.encoded_edit.setEnabled(enabled)

    def get_params(self):
        return {
            'NoProfile': self.noprofile_checkbox.isChecked(),
            'NoExit': self.noexit_checkbox.isChecked(),
            'ExecutionPolicy': self.execpol_combo.currentText() if self.execpol_combo.currentText() != self.tr("Nenhum") else "",
            'WindowStyle': self.winstyle_combo.currentText() if self.winstyle_combo.currentText() != self.tr("Nenhum") else "",
            'Command': self.command_edit.text(),
            'EncodedCommand': self.encoded_edit.text(),
        }
