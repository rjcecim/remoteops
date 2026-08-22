from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QCheckBox, QPlainTextEdit, QSizePolicy

from remoteops.ui.style import FONT_MONO, SIZE_MONO
from remoteops.ui.widgets.card import CardWidget, add_row_full_width, grid_in_card


class CommandPreviewWidget(CardWidget):
    """Card expansível com a pré-visualização do comando montado."""

    def __init__(self, parent=None):
        super().__init__("\uE756", "Pré-visualização do comando", parent)
        self._title_label.setText(self.tr("Pré-visualização do comando"))
        self.set_layout_stretch(1)
        self.set_expanding(True)
        self.set_collapsible(True, collapsed=False)
        self.set_copyable(True)
        self.set_runnable(True)
        self._run_btn.setToolTip(self.tr("Executar"))
        self._stop_btn.setToolTip(
            self.tr(
                "Encerrar sessão (envia exit). "
                "Segundo clique encerra o processo local."
            )
        )
        self.copyRequested.connect(self._copy_to_clipboard)

        # Mesmo padrão visual do checkbox "Usar faixa de IP" (Origem dos hosts).
        g = grid_in_card(self)
        self.external_cmd_check = QCheckBox(self.tr("Executar como comando externo"))
        self.external_cmd_check.setChecked(False)
        self.external_cmd_check.setToolTip(
            self.tr(
                "Desmarcado: executa via ConPTY e envia a saída ao Console de Saída. "
                "Marcado: abre uma janela de console externa (sem ConPTY nesta execução)."
            )
        )
        add_row_full_width(g, 0, self.external_cmd_check)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont(FONT_MONO, SIZE_MONO))
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview.setMinimumHeight(56)
        self.content_layout.addWidget(self.preview, 1)

    def is_external_command(self) -> bool:
        """Fonte de verdade do modo de execução (ConPTY vs console externo)."""
        return bool(self.external_cmd_check.isChecked())

    def set_external_command(self, enabled: bool) -> None:
        self.external_cmd_check.setChecked(bool(enabled))

    def set_command(self, command: str):
        self.preview.setPlainText(command)

    def get_command(self):
        return self.preview.toPlainText()

    def _copy_to_clipboard(self) -> None:
        text = (self.get_command() or "").strip()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self._copy_btn.setToolTip(self.tr("Copiado!"))
        QTimer.singleShot(
            1500,
            lambda: self._copy_btn.setToolTip(
                self.tr("Copiar para a área de transferência")
            ),
        )
