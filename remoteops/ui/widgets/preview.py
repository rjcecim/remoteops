from PyQt6.QtWidgets import QPlainTextEdit, QSizePolicy, QApplication
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont

from remoteops.ui.style import FONT_MONO, SIZE_MONO
from remoteops.ui.widgets.card import CardWidget


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
        self._stop_btn.setToolTip(self.tr("Parar"))
        self.copyRequested.connect(self._copy_to_clipboard)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFont(QFont(FONT_MONO, SIZE_MONO))
        self.preview.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview.setMinimumHeight(56)
        self.content_layout.addWidget(self.preview, 1)

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
