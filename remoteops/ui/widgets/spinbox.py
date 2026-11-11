"""QSpinBox / QTimeEdit com setas MDL2 compactas, alinhadas ao valor."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QSize, Qt, QTime
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QAbstractSpinBox, QSizePolicy, QSpinBox, QTimeEdit, QToolButton

from remoteops.ui.style import (
    COLOR_ACCENT,
    COLOR_HOVER,
    COLOR_PRESSED,
    COLOR_TEXT_MUTED,
)


# Empilhados, altura total ≈ a do texto (Segoe UI 10pt).
_BTN_W = 12
_BTN_H = 9
_GAP = 0
_MARGIN = 2
_FONT_PT = 7
# O QStyleSheetStyle deixa o editor com 1px quando ButtonSymbols é NoButtons.
# O texto é posicionado aqui, à esquerda dos chevrons.
_EDITOR_LEFT = 8
_EDITOR_RIGHT_PAD = 4


def _step_button(icon_char: str, tooltip: str, parent) -> QToolButton:
    btn = QToolButton(parent)
    btn.setObjectName("spinStep")
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    btn.setAutoRaise(True)
    btn.setAutoRepeat(True)
    btn.setAutoRepeatDelay(400)
    btn.setAutoRepeatInterval(60)
    btn.setFixedSize(_BTN_W, _BTN_H)
    btn.setFont(QFont("Segoe MDL2 Assets", _FONT_PT))
    btn.setText(icon_char)
    btn.setToolTip(tooltip)
    btn.setStyleSheet(f"""
        QToolButton#spinStep {{
            border: none;
            background: transparent;
            color: {COLOR_ACCENT};
            padding: 0;
            min-width: {_BTN_W}px;
            max-width: {_BTN_W}px;
            min-height: {_BTN_H}px;
            max-height: {_BTN_H}px;
        }}
        QToolButton#spinStep:hover {{
            background: {COLOR_HOVER};
            border-radius: 2px;
        }}
        QToolButton#spinStep:pressed {{
            background: {COLOR_PRESSED};
            border-radius: 2px;
        }}
        QToolButton#spinStep:disabled {{
            color: {COLOR_TEXT_MUTED};
            background: transparent;
        }}
    """)
    return btn


class _StepButtonsMixin:
    """Setas MDL2 empilhadas à direita (compartilhado por spin e horário)."""

    def _init_step_buttons(self) -> None:
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            "QSpinBox::up-button, QSpinBox::down-button,"
            " QTimeEdit::up-button, QTimeEdit::down-button"
            " { width: 0; height: 0; border: none; }"
        )
        self._up = _step_button("\uE70E", "Aumentar", self)
        self._down = _step_button("\uE70D", "Diminuir", self)
        self._up.clicked.connect(self.stepUp)
        self._down.clicked.connect(self.stepDown)
        self._apply_text_margin()
        self._sync_step_buttons()

    def _buttons_span(self) -> int:
        return _BTN_W + _MARGIN * 2

    def _editor_extra_width(self) -> int:
        """Largura extra além do texto: margem esquerda, folga e chevrons."""
        return _EDITOR_LEFT + _EDITOR_RIGHT_PAD + self._buttons_span() + 4

    def _apply_text_margin(self) -> None:
        edit = self.lineEdit()
        if edit is None:
            return
        edit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        edit.setTextMargins(2, 0, _EDITOR_RIGHT_PAD, 0)

    def _place_editor(self) -> None:
        """Devolve largura ao QLineEdit interno.

        Com stylesheet global e ``NoButtons``, o Qt calcula
        ``SC_SpinBoxEditField`` com largura 1 e o valor fica invisível.
        """
        edit = self.lineEdit()
        if edit is None or self.width() <= 0 or self.height() <= 0:
            return
        left = _EDITOR_LEFT
        top = 1
        width = max(1, self.width() - left - self._buttons_span())
        height = max(1, self.height() - 2)
        current = edit.geometry()
        if (
            current.x() == left
            and current.y() == top
            and current.width() == width
            and current.height() == height
        ):
            return
        edit.setGeometry(left, top, width, height)

    def _layout_step_buttons(self) -> None:
        if not hasattr(self, "_up"):
            return
        x = self.width() - _MARGIN - _BTN_W
        stack_h = _BTN_H * 2 + _GAP
        y = max(0, (self.height() - stack_h) // 2)
        self._up.setGeometry(x, y, _BTN_W, _BTN_H)
        self._down.setGeometry(x, y + _BTN_H + _GAP, _BTN_W, _BTN_H)
        self._up.raise_()
        self._down.raise_()

    def _sync_step_buttons(self, *_args) -> None:
        flags = self.stepEnabled()
        self._up.setEnabled(bool(flags & QAbstractSpinBox.StepEnabledFlag.StepUpEnabled))
        self._down.setEnabled(
            bool(flags & QAbstractSpinBox.StepEnabledFlag.StepDownEnabled)
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_editor()
        self._layout_step_buttons()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._apply_text_margin()
        self._place_editor()
        self._layout_step_buttons()
        self._sync_step_buttons()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if not hasattr(self, "_up"):
            return
        if event.type() in (
            QEvent.Type.StyleChange,
            QEvent.Type.FontChange,
            QEvent.Type.LanguageChange,
        ):
            self._apply_text_margin()
            self._place_editor()
            self._layout_step_buttons()


class StepSpinBox(_StepButtonsMixin, QSpinBox):
    """Spin compacto: valor colado nos chevrons empilhados à direita."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_step_buttons()
        self.valueChanged.connect(self._sync_step_buttons)

    def _content_width(self) -> int:
        fm = self.fontMetrics()
        samples = (
            f"{self.prefix()}{self.minimum()}{self.suffix()}",
            f"{self.prefix()}{self.maximum()}{self.suffix()}",
        )
        text_w = max(fm.horizontalAdvance(s) for s in samples)
        return text_w + self._editor_extra_width()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._content_width(), super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def setRange(self, min_val: int, max_val: int) -> None:  # noqa: N802
        super().setRange(min_val, max_val)
        self._sync_step_buttons()
        self.updateGeometry()

    def setMinimum(self, min_val: int) -> None:  # noqa: N802
        super().setMinimum(min_val)
        self._sync_step_buttons()
        self.updateGeometry()

    def setMaximum(self, max_val: int) -> None:  # noqa: N802
        super().setMaximum(max_val)
        self._sync_step_buttons()
        self.updateGeometry()

    def setSuffix(self, suffix: str) -> None:  # noqa: N802
        super().setSuffix(suffix)
        self.updateGeometry()


class StepTimeEdit(_StepButtonsMixin, QTimeEdit):
    """Horário HH:mm com o mesmo visual compacto do ``StepSpinBox``."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDisplayFormat("HH:mm")
        self.setTime(QTime(18, 0))
        self.setWrapping(True)
        self._init_step_buttons()
        self.timeChanged.connect(self._sync_step_buttons)

    def _content_width(self) -> int:
        fm = self.fontMetrics()
        text_w = fm.horizontalAdvance("23:59")
        return text_w + self._editor_extra_width()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._content_width(), super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()
