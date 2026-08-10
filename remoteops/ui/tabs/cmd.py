from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QCheckBox, QLineEdit, QSizePolicy
)
from remoteops.ui.widgets.card import (
    CardWidget, grid_in_card, add_row, add_row_full_width, make_card_stack,
)


class CmdTab(QWidget):
    formLayoutChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        vbox = make_card_stack(self)

        # ── Card Opções ───────────────────────────────────────────────────────
        card_opts = CardWidget("\uE115", self.tr("Opções"))  # Engrenagem = opções/switches
        card_opts.set_collapsible(True, collapsed=False)
        g1 = grid_in_card(card_opts)
        row = 0

        self.c_checkbox = QCheckBox(self.tr("/C (Executa comando e sai)"))
        self.c_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.c_checkbox)
        row += 1

        self.k_checkbox = QCheckBox(self.tr("/K (Executa comando e permanece)"))
        self.k_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.k_checkbox)
        row += 1

        self.q_checkbox = QCheckBox(self.tr("/Q (Desativa echo)"))
        self.q_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.q_checkbox)
        row += 1

        self.d_checkbox = QCheckBox(self.tr("/D (Desativa AutoRun)"))
        self.d_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.d_checkbox)
        row += 1

        self.s_checkbox = QCheckBox(self.tr("/S (Modifica tratamento de aspas)"))
        self.s_checkbox.setChecked(False)
        add_row_full_width(g1, row, self.s_checkbox)

        vbox.addWidget(card_opts)

        # ── Card Comando ───────────────────────────────────────────────────────
        card_cmd = CardWidget("\uE768", self.tr("Comando"))  # Play = executar comando
        card_cmd.set_collapsible(True, collapsed=False)
        g2 = grid_in_card(card_cmd)

        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText(self.tr("Comando ou script .bat"))
        add_row(g2, 0, self.tr("Comando:"), self.command_edit)

        vbox.addWidget(card_cmd)

        self._form_cards = (card_opts, card_cmd)
        for card in self._form_cards:
            card.collapsedChanged.connect(self._on_form_card_collapsed)

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        self.updateGeometry()
        if self.layout() is not None:
            self.layout().activate()
        self.formLayoutChanged.emit()

    def get_params(self):
        return {
            '/C': self.c_checkbox.isChecked(),
            '/K': self.k_checkbox.isChecked(),
            '/Q': self.q_checkbox.isChecked(),
            '/D': self.d_checkbox.isChecked(),
            '/S': self.s_checkbox.isChecked(),
            'Command': self.command_edit.text(),
        }

    def set_command_field_enabled(self, enabled: bool):
        self.command_edit.setEnabled(enabled)
