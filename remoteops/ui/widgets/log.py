import re
from typing import Iterable, Optional, Sequence

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
from remoteops.utils.redaction import redact_command_text

_SESSION_LABELS = {
    "idle": "Desconectado",
    "connecting": "Conectando",
    "running": "Executando",
    "session": "Sessão ativa",
    "exited": "Encerrado",
    "error": "Erro",
}

# Retenção visual: evita QTextEdit crescer sem limite em sessões longas.
_MAX_DOCUMENT_BLOCKS = 4000
_MAX_HISTORY = 80
_BATCH_FLUSH_MS = 16

# Prompt tipicamente sensível → próxima entrada não entra no histórico ↑/↓.
_SENSITIVE_PROMPT_RE = re.compile(
    r"(password|passwd|senha|passphrase|token|secret)\s*[:=]?\s*$",
    re.IGNORECASE,
)

# Barra de progresso estilo WinGet (substitui a linha anterior).
_PROGRESS_BAR_RE = re.compile(
    r"^[\s█▒]+[0-9.,]+ (KB|MB|GB) / [0-9.,]+ (KB|MB|GB)"
)


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

    def __init__(self, parent=None, *, title: str = "", icon: str = ""):
        label = title or "Console de Saída"
        glyph = icon or "\uE9F9"
        super().__init__(glyph, label, parent)
        self._title_label.setText(self.tr(label))
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
        self._follow_tail = True
        self._pending_sensitive_input = False
        self._redaction_secrets: list[str] = []
        self._batch: list[str] = []
        self._batch_timer = QTimer(self)
        self._batch_timer.setSingleShot(True)
        self._batch_timer.setInterval(_BATCH_FLUSH_MS)
        self._batch_timer.timeout.connect(self._flush_batch)
        self._trim_notice_shown = False

        self._clear_btn = QToolButton()
        self._clear_btn.setObjectName("cardDownload")
        self._clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._clear_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._clear_btn.setAutoRaise(True)
        self._clear_btn.setFixedSize(HEADER_BTN_SIZE, HEADER_BTN_SIZE)
        self._clear_btn.setFont(QFont("Segoe MDL2 Assets", 10))
        self._clear_btn.setText("\uE74D")  # Delete
        self._clear_btn.setToolTip(
            self.tr("Limpar este console (apenas visual; não encerra a sessão)")
        )
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
        bar = self.text_edit.verticalScrollBar()
        bar.valueChanged.connect(self._on_scroll_value_changed)
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
        self._schedule_console_size()

    def set_session_status(self, state: str) -> None:
        self._status = state if state in _SESSION_LABELS else "idle"
        self._status_label.setText(self.tr(_SESSION_LABELS[self._status]))
        self._status_label.setToolTip(self._status_label.text())

    def set_redaction_secrets(self, passwords: Optional[Iterable[str]]) -> None:
        """Define senhas conhecidas da operação atual (última barreira antes da UI)."""
        self._redaction_secrets = [p for p in (passwords or []) if p]

    def clear_redaction_secrets(self) -> None:
        """Remove senhas efêmeras ao encerrar a operação."""
        self._redaction_secrets = []

    def redaction_secrets(self) -> Sequence[str]:
        return tuple(self._redaction_secrets)

    def _sanitize_for_display(self, text: str) -> str:
        """Toda saída visível/copiada passa por aqui (flags ``-p`` + senhas conhecidas)."""
        return redact_command_text(
            text or "",
            passwords=self._redaction_secrets or None,
        )

    def set_interactive(self, active: bool) -> None:
        """Habilita/desabilita o campo de teclado ligado ao ConPTY."""
        was = self._interactive
        self._interactive = bool(active)
        self._input.setEnabled(self._interactive)
        self._input_wrap.setVisible(self._interactive)
        self._end_session_btn.setEnabled(self._interactive)
        if self._interactive:
            self._input.setFocus(Qt.FocusReason.OtherFocusReason)
            self._emit_console_size()
            if self._status in ("idle", "connecting", "running"):
                self.set_session_status("session")
        else:
            self._flush_batch()
            self._input.clear()
            self._draft = ""
            # Histórico local não persiste além da sessão interativa.
            if was:
                self._history.clear()
            self._history_index = 0
            self._pending_sensitive_input = False
            self.set_partial_line("")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._schedule_console_size()

    def _schedule_console_size(self) -> None:
        """Timer filho do widget — cancelado automaticamente na destruição."""
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._emit_console_size)
        timer.start(0)

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
        size = (cols, rows)
        if size == self._last_console_size:
            return
        self._last_console_size = size
        self.consoleResized.emit(cols, rows)

    def _on_scroll_value_changed(self, value: int) -> None:
        bar = self.text_edit.verticalScrollBar()
        self._follow_tail = value >= bar.maximum() - 4

    def _at_bottom(self) -> bool:
        bar = self.text_edit.verticalScrollBar()
        return bar.value() >= bar.maximum() - 4

    def _maybe_follow(self, *, force: bool = False) -> None:
        if force or self._follow_tail:
            self.text_edit.moveCursor(QTextCursor.MoveOperation.End)
            self.text_edit.ensureCursorVisible()
            self._follow_tail = True

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
        store = bool(text.strip()) and not self._pending_sensitive_input
        self._pending_sensitive_input = False
        if store:
            if not self._history or self._history[-1] != text:
                self._history.append(text)
            if len(self._history) > _MAX_HISTORY:
                self._history = self._history[-_MAX_HISTORY:]
        self._history_index = len(self._history)
        # Sem eco local: o CMD/ConPTY ecoa a linha (evita duplicar no console).
        self.inputSubmitted.emit(text)

    def _copy_visible(self) -> None:
        cursor = self.text_edit.textCursor()
        selected = cursor.selectedText().replace("\u2029", "\n")
        text = selected if selected.strip() else (self.text_edit.toPlainText() or "")
        if not text.strip():
            return
        # Reforço: clipboard nunca leva senhas conhecidas da operação.
        QApplication.clipboard().setText(self._sanitize_for_display(text))
        self._copy_btn.setToolTip(self.tr("Copiado!"))
        tip = self.tr("Copiar seleção (ou tudo, se nada estiver selecionado)")
        tip_timer = QTimer(self)
        tip_timer.setSingleShot(True)
        tip_timer.timeout.connect(lambda: self._copy_btn.setToolTip(tip))
        tip_timer.start(1500)

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
        self._flush_batch()
        text = self._sanitize_for_display(text if text is not None else "")
        # Heurística: próximo Enter após prompt de senha não vai ao histórico.
        probe = text.strip()
        if probe and _SENSITIVE_PROMPT_RE.search(probe):
            self._pending_sensitive_input = True
        keep_selection = self.text_edit.textCursor().hasSelection()
        follow = self._follow_tail and not keep_selection
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
            if not keep_selection:
                self.text_edit.setTextCursor(cursor)
            if follow:
                self.text_edit.ensureCursorVisible()
                self._follow_tail = True
        else:
            self._partial_anchor = None

    def append_log(self, text: str) -> None:
        text = self._sanitize_for_display(text if text is not None else "")
        # Progresso WinGet: aplica na hora (substitui linha).
        if _PROGRESS_BAR_RE.match(text):
            self._flush_batch()
            self._append_immediate(text, replace_progress=True)
            return
        self._batch.append(text)
        if not self._batch_timer.isActive():
            self._batch_timer.start()

    def _flush_batch(self) -> None:
        if not self._batch:
            return
        lines = self._batch
        self._batch = []
        for line in lines:
            self._append_immediate(line, replace_progress=False)

    def _append_immediate(self, text: str, *, replace_progress: bool) -> None:
        self._commit_partial()
        keep_selection = self.text_edit.textCursor().hasSelection()
        follow = (self._follow_tail or self._at_bottom()) and not keep_selection
        if replace_progress and _PROGRESS_BAR_RE.match(text or ""):
            cursor = self.text_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.select(QTextCursor.SelectionType.LineUnderCursor)
            cursor.removeSelectedText()
            cursor.deletePreviousChar()
            cursor.insertText(text)
            if not keep_selection:
                self.text_edit.setTextCursor(cursor)
            if follow:
                self.text_edit.ensureCursorVisible()
                self._follow_tail = True
            self._trim_document_if_needed()
            return
        self.text_edit.append(text if text is not None else "")
        if follow:
            self._maybe_follow(force=True)
        self._trim_document_if_needed()

    def _trim_document_if_needed(self) -> None:
        """Descarta blocos antigos sem corromper linha parcial nem a sessão."""
        if self._partial_anchor is not None:
            return
        doc = self.text_edit.document()
        excess = doc.blockCount() - _MAX_DOCUMENT_BLOCKS
        if excess <= 0:
            return
        cursor = QTextCursor(doc)
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        for _ in range(excess):
            cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
            cursor.movePosition(
                QTextCursor.MoveOperation.NextBlock,
                QTextCursor.MoveMode.KeepAnchor,
            )
            cursor.removeSelectedText()
        if not self._trim_notice_shown:
            self._trim_notice_shown = True
            notice = self.tr(
                "[Console] Histórico visual limitado às mensagens mais recentes."
            )
            end = self.text_edit.textCursor()
            end.movePosition(QTextCursor.MoveOperation.End)
            plain = self.text_edit.toPlainText()
            if plain and not plain.endswith("\n"):
                end.insertText("\n")
            end.insertText(notice + "\n")
        if self._follow_tail:
            self._maybe_follow(force=True)

    def clear_log(self):
        """Limpa somente este console; não afeta outros logs da aplicação."""
        self._batch_timer.stop()
        self._batch.clear()
        self._partial_anchor = None
        self._trim_notice_shown = False
        self.text_edit.clear()
        self._follow_tail = True
