from PyQt6.QtWidgets import QPlainTextEdit, QSizePolicy
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
