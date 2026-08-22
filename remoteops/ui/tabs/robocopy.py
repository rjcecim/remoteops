from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QWidget, QLineEdit, QCheckBox, QHBoxLayout, QSizePolicy
)
from remoteops.ui.widgets.card import (
    CardWidget, grid_in_card, add_row, make_card_stack, finish_card_stack,
)


class RobocopyTab(QWidget):
    formLayoutChanged = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        vbox = make_card_stack(self)

        # ── Card Destino ──────────────────────────────────────────────────────
        card_dest = CardWidget("\uE8B7", self.tr("Destino"))
        g1 = grid_in_card(card_dest)

        self.dest_edit = QLineEdit()
        self.dest_edit.setText("C:\\temp")
        self.dest_edit.setPlaceholderText(self.tr("Ex.: C:\\temp ou C:\\temp\\scripts"))
        self.dest_edit.setToolTip(
            self.tr(
                "Caminho completo no C: do host remoto. "
                "Ex.: C:\\temp ou C:\\temp\\scripts. "
                "C: é opcional (temp equivale a C:\\temp). Não use C$."
            )
        )
        add_row(g1, 0, self.tr("Pasta de destino:"), self.dest_edit)

        vbox.addWidget(card_dest)

        # ── Card Parâmetros ─────────────────────────────────────────────────────
        card_params = CardWidget("\uE115", self.tr("Parâmetros"))
        g2 = grid_in_card(card_params)

        switches_layout = QHBoxLayout()
        switches_layout.setContentsMargins(0, 0, 0, 0)
        switches_layout.setSpacing(5)
        self.switches = []
        self.switch_labels = [
            ("/NFL", self.tr("Não listar arquivos copiados")),
            ("/NDL", self.tr("Não listar diretórios")),
            ("/NJH", self.tr("Não mostrar cabeçalho do job")),
            ("/NJS", self.tr("Não mostrar resumo do job")),
            ("/nc", self.tr("Não mostrar classes de arquivo")),
            ("/ns", self.tr("Não mostrar tamanhos de arquivo")),
            ("/np", self.tr("Não mostrar progresso"))
        ]
        for flag, tip in self.switch_labels:
            cb = QCheckBox(flag)
            cb.setChecked(True)
            cb.setToolTip(tip)
            self.switches.append(cb)
            switches_layout.addWidget(cb)
        switches_layout.addStretch()
        switches_container = QWidget()
        switches_container.setLayout(switches_layout)
        switches_container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        add_row(g2, 0, self.tr("Parâmetros:"), switches_container)

        vbox.addWidget(card_params)
        finish_card_stack(vbox)

        self._form_cards = (card_dest, card_params)
        for card, on_reset in zip(
            self._form_cards,
            (self._reset_card_destino, self._reset_card_parametros),
        ):
            card.set_collapsible(True, collapsed=False)
            card.set_resettable(True, self.tr("Restaurar padrões deste card"))
            card.resetRequested.connect(on_reset)
            card.collapsedChanged.connect(self._on_form_card_collapsed)

    def _on_form_card_collapsed(self, _collapsed: bool = False) -> None:
        lay = self.layout()
        if lay is not None:
            lay.invalidate()
            lay.activate()
        self.updateGeometry()
        self.formLayoutChanged.emit()

    def get_params(self):
        switches = " ".join(cb.text() for cb in self.switches if cb.isChecked())
        return {
            'dest': self.dest_edit.text(),
            'switches': switches,
        }

    def _reset_card_destino(self) -> None:
        self.dest_edit.setText("C:\\temp")

    def _reset_card_parametros(self) -> None:
        for cb in self.switches:
            cb.setChecked(True)

    def reset_to_defaults(self) -> None:
        self._reset_card_destino()
        self._reset_card_parametros()
