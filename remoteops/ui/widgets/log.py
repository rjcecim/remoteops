import re

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QKeyEvent, QResizeEvent, QTextCursor
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QWidget,
)

from remoteops.ui.style import FONT_MONO, HEADER_BTN_SIZE, INPUT_HEIGHT, SIZE_MONO, SIZE_UI_SMALL
from remoteops.ui.widgets.card import CardWidget

_SESSION_LABELS = {
    "idle": "Desconectado",
    "connecting": "Conectando",
    "running": "Executando",
    "session": "Sessão ativa",
    "exited": "Encerrado",
    "error": "Erro",
}


class _InteractiveInput(QLineEdit):
    """Campo de entrada do console; Ctrl+C interrompe; ↑/↓ navegam o histórico local."""

    interruptRequested = pyqtSignal()
    historyStep = pyqtSignal(int)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if (
            event.key() == Qt.Key.Key_C
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        ):
            self.interruptRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up and not event.modifiers():
            self.historyStep.emit(-1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Down and not event.modifiers():
            self.historyStep.emit(1)
            event.accept()
            return
        super().keyPressEvent(event)


class LogOutputWidget(CardWidget):
    """Card expansível com o console de saída (+ entrada interativa ConPTY)."""

    inputSubmitted = pyqtSignal(str)
    interruptRequested = pyqtSignal()
    sessionExitRequested = pyqtSignal()
    consoleResized = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__("\uE9F9", "Console de Saída", parent)
        self._title_label.setText(self.tr("Console de Saída"))
        self.set_layout_stretch(1)
        self.set_expanding(True)
        self.set_collapsible(True, collapsed=False)
        self.set_copyable(True)
        self.copyRequested.connect(self._copy_visible)
        self._copy_btn.setToolTip(
            self.tr("Copiar seleção (ou tudo, se nada estiver selecionado)")
        )
        self._partial_anchor: int | None = None
        self._interactive = False
        self._history: list[str] = []
        self._history_index = 0
        self._draft = ""
        self._status = "idle"
        self._last_console_size = (120, 30)

        self._clear_btn = QToolButton()
        self._clear_btn.setObjectName("cardDownload")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._clear_btn.setAutoRaise(True)
        self._clear_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._clear_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._clear_btn.setText("\uE74D")  # Delete
        self._clear_btn.setToolTip(self.tr("Limpar este console (apenas na tela)"))
        # Instância local: não propaga para outros LogOutputWidget.
        self._clear_btn.clicked.connect(self.clear_log)

        self._status_label = QLabel()
        self._status_label.setObjectName("consoleSessionStatus")
        self._status_label.setStyleSheet(
            f"QLabel#consoleSessionStatus {{ color: palette(mid); font-size: {SIZE_UI_SMALL}pt; }}"
        )

        header = self._header_widget.layout()
        if header is not None:
            header.removeWidget(self._copy_btn)
            idx = header.indexOf(self._toggle_btn)
            if idx < 0:
                idx = header.count()
            # Título à esquerda; à direita: status + copiar + limpar + recolher
            header.insertWidget(idx, self._status_label)
            header.insertWidget(idx + 1, self._copy_btn)
            header.insertWidget(idx + 2, self._clear_btn)

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
            self.tr("Sessão interativa: Enter envia · ↑/↓ histórico · Ctrl+C interrompe")
        )
        self._input.returnPressed.connect(self._on_return)
        self._input.interruptRequested.connect(self.interruptRequested.emit)
        self._input.historyStep.connect(self._on_history_step)
        self._end_session_btn = QToolButton()
        self._end_session_btn.setObjectName("cardDownload")
        self._end_session_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._end_session_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._end_session_btn.setAutoRaise(True)
        self._end_session_btn.setFixedSize(INPUT_HEIGHT, INPUT_HEIGHT)
        self._end_session_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._end_session_btn.setText("\uE711")  # Cancel
        self._end_session_btn.setToolTip(
            self.tr(
                "Encerrar sessão (envia exit). "
                "Segundo clique em Parar encerra o processo local."
            )
        )
        self._end_session_btn.clicked.connect(self.sessionExitRequested.emit)
        input_row.addWidget(prompt, 0)
        input_row.addWidget(self._input, 1)
        input_row.addWidget(self._end_session_btn, 0)
        wrap = QWidget()
        wrap.setLayout(input_row)
        self._input_wrap = wrap
        self.content_layout.addWidget(wrap, 0)
        self.set_interactive(False)
        self.set_session_status("idle")
        QTimer.singleShot(0, self._emit_console_size)

    def set_session_status(self, state: str) -> None:
        self._status = state if state in _SESSION_LABELS else "idle"
        self._status_label.setText(self.tr(_SESSION_LABELS[self._status]))
        self._status_label.setToolTip(self._status_label.text())

    def set_interactive(self, active: bool) -> None:
        """Habilita/desabilita o campo de teclado ligado ao ConPTY."""
        self._interactive = bool(active)
        self._input.setEnabled(self._interactive)
        self._input_wrap.setVisible(self._interactive)
        self._end_session_btn.setEnabled(self._interactive)
        if self._interactive:
            self._input.setFocus(Qt.FocusReason.OtherFocusReason)
            self._emit_console_size()
        else:
            self._input.clear()
            self._draft = ""
            self._history_index = len(self._history)
            self.set_partial_line("")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._emit_console_size)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._emit_console_size()

    def _emit_console_size(self) -> None:
        # Oculto: não redimensionar o ConPTY (a sessão continua em segundo plano).
        if not self.isVisible():
            return
        fm = self.text_edit.fontMetrics()
        cw = max(1, fm.horizontalAdvance("M"))
        ch = max(1, fm.lineSpacing())
        view = self.text_edit.viewport()
        cols = max(20, int(view.width() / cw))
        rows = max(5, int(view.height() / ch))
        self._last_console_size = (cols, rows)
        self.consoleResized.emit(cols, rows)

    def _on_history_step(self, step: int) -> None:
        if not self._history:
            return
        if self._history_index == len(self._history):
            self._draft = self._input.text()
        nxt = self._history_index + int(step)
        if nxt < 0 or nxt > len(self._history):
            return
        self._history_index = nxt
        if self._history_index == len(self._history):
            self._input.setText(self._draft)
        else:
            self._input.setText(self._history[self._history_index])
        self._input.setCursorPosition(len(self._input.text()))

    def _on_return(self) -> None:
        if not self._interactive:
            return
        text = self._input.text()
        self._input.clear()
        self._draft = ""
        if text.strip():
            if not self._history or self._history[-1] != text:
                self._history.append(text)
            if len(self._history) > 200:
                self._history = self._history[-200:]
        self._history_index = len(self._history)
        # Sem eco local: o CMD/ConPTY ecoa a linha (evita duplicar no console).
        self.inputSubmitted.emit(text)

    def _copy_visible(self) -> None:
        cursor = self.text_edit.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")
        text = selected if selected.strip() else (self.text_edit.toPlainText() or "")
        if not text.strip():
            return
        QApplication.clipboard().setText(text)
        self._copy_btn.setToolTip(self.tr("Copiado!"))
        QTimer.singleShot(
            1500,
            lambda: self._copy_btn.setToolTip(
                self.tr("Copiar seleção (ou tudo, se nada estiver selecionado)")
            ),
        )

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
            doc = self.text_edit.toPlainText()
            if doc and not doc.endswith("\n"):
                cursor.insertText("\n")
            self._partial_anchor = cursor.position()
        if text:
            cursor.insertText(text)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.ensureCursorVisible()
        else:
            self._partial_anchor = None

    def append_log(self, text: str):
        self._commit_partial()
        animation_lines = {"-", "\\", "|", "/"}
        if text.strip() in animation_lines:
            return
        progress_bar_pattern = re.compile(
            r"^[\s█▒]+[0-9.,]+ (KB|MB|GB) / [0-9.,]+ (KB|MB|GB)"
        )
        if progress_bar_pattern.match(text):
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deletePreviousChar()
            cursor.insertText(text)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.ensureCursorVisible()
            return
        self.text_edit.append(text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.End)

    def clear_log(self):
        """Limpa somente este console; não afeta outros logs da aplicação."""
        self._partial_anchor = None
        self.text_edit.clear()
