from PyQt6.QtWidgets import QTextEdit, QSizePolicy, QToolButton
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextCursor
import re

from remoteops.ui.style import FONT_MONO, SIZE_MONO
from remoteops.ui.widgets.card import CardWidget


class LogOutputWidget(CardWidget):
    """Card expansível com o console de saída."""

    def __init__(self, parent=None):
        super().__init__("\uE9F9", "Console de Saída", parent)
        self._title_label.setText(self.tr("Console de Saída"))
        self.set_layout_stretch(1)
        self.set_expanding(True)
        self.set_collapsible(True, collapsed=False)

        # Limpa só a tela (QTextEdit); não apaga o arquivo de histórico.
        self._clear_btn = QToolButton()
        self._clear_btn.setObjectName("cardDownload")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._clear_btn.setAutoRaise(True)
        self._clear_btn.setFixedSize(22, 22)
        self._clear_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._clear_btn.setText("\uE74D")  # Delete
        self._clear_btn.setToolTip(self.tr("Limpar log (apenas na tela)"))
        self._clear_btn.clicked.connect(self.clear_log)
        header = self._header_widget.layout()
        if header is not None:
            idx = header.indexOf(self._toggle_btn)
            if idx >= 0:
                header.insertWidget(idx, self._clear_btn)
            else:
                header.addWidget(self._clear_btn)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont(FONT_MONO, SIZE_MONO))
        self.text_edit.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.text_edit.setMinimumHeight(56)
        self.content_layout.addWidget(self.text_edit, 1)

    def append_log(self, text: str):
        # Filtra linhas de animação (ex: '-', '\\', '|', '/') que aparecem sozinhas ou com espaços
        animation_lines = {'-', '\\', '|', '/'}
        if text.strip() in animation_lines:
            return
        # Detecta barra de progresso (ex: linhas com blocos e tamanho)
        progress_bar_pattern = re.compile(r'^[\s█▒]+[0-9.,]+ (KB|MB|GB) / [0-9.,]+ (KB|MB|GB)')
        if progress_bar_pattern.match(text):
            # Atualiza a última linha se for barra de progresso
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deletePreviousChar()  # Remove o \n anterior
            cursor.insertText(text)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.ensureCursorVisible()
            return
        self.text_edit.append(text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.End)

    def clear_log(self):
        self.text_edit.clear()
