from PyQt6.QtWidgets import (
    QTextEdit,
    QSizePolicy,
    QToolButton,
    QLineEdit,
    QHBoxLayout,
    QWidget,
    QLabel,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor, QKeyEvent
import re

from remoteops.ui.style import FONT_MONO, SIZE_MONO, INPUT_HEIGHT
from remoteops.ui.widgets.card import CardWidget


class _InteractiveInput(QLineEdit):
    """Campo de entrada do console; Ctrl+C envia interrupt à sessão."""

    interruptRequested = pyqtSignal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() == Qt.Key.Key_C
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        ):
            self.interruptRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class LogOutputWidget(CardWidget):
    """Card expansível com o console de saída (+ entrada interativa ConPTY)."""

    inputSubmitted = pyqtSignal(str)
    interruptRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("\uE9F9", "Console de Saída", parent)
        self._title_label.setText(self.tr("Console de Saída"))
        self.set_layout_stretch(1)
        self.set_expanding(True)
        self.set_collapsible(True, collapsed=False)
        self._partial_anchor: int | None = None
        self._interactive = False

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

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 4, 0, 0)
        input_row.setSpacing(6)
        prompt = QLabel(">")
        prompt.setFont(QFont(FONT_MONO, SIZE_MONO))
        self._input = _InteractiveInput()
        self._input.setFont(QFont(FONT_MONO, SIZE_MONO))
        self._input.setFixedHeight(INPUT_HEIGHT)
        self._input.setPlaceholderText(
            self.tr("Sessão interativa: digite e Enter (Ctrl+C interrompe)")
        )
        self._input.returnPressed.connect(self._on_return)
        self._input.interruptRequested.connect(self.interruptRequested.emit)
        input_row.addWidget(prompt, 0)
        input_row.addWidget(self._input, 1)
        wrap = QWidget()
        wrap.setLayout(input_row)
        self._input_wrap = wrap
        self.content_layout.addWidget(wrap, 0)
        self.set_interactive(False)

    def set_interactive(self, active: bool) -> None:
        """Habilita/desabilita o campo de teclado ligado ao ConPTY."""
        self._interactive = bool(active)
        self._input.setEnabled(self._interactive)
        self._input_wrap.setVisible(self._interactive)
        if self._interactive:
            self._input.setFocus(Qt.FocusReason.OtherFocusReason)
        else:
            self._input.clear()
            self.set_partial_line("")

    def _on_return(self) -> None:
        if not self._interactive:
            return
        text = self._input.text()
        self._input.clear()
        self.inputSubmitted.emit(text)

    def _commit_partial(self) -> None:
        if self._partial_anchor is None:
            return
        cursor = self.text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        doc = self.text_edit.toPlainText()
        if doc and not doc.endswith("\n"):
            cursor.insertText("\n")
        self._partial_anchor = None

    def set_partial_line(self, text: str) -> None:
        """Atualiza a linha incompleta (ex.: prompt ``C:\\>`` sem \\n)."""
        cursor = self.text_edit.textCursor()
        if self._partial_anchor is not None:
            cursor.setPosition(self._partial_anchor)
            cursor.movePosition(
                QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor
            )
            cursor.removeSelectedText()
        else:
            cursor.movePosition(QTextCursor.MoveOperation.End)
            self._partial_anchor = cursor.position()
        if text:
            cursor.insertText(text)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.ensureCursorVisible()
        else:
            self._partial_anchor = None

    def append_log(self, text: str):
        self._commit_partial()
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
        self._partial_anchor = None
        self.text_edit.clear()
